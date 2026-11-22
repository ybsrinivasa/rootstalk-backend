"""Messaging — Client Portal endpoints.

Phase 2A (2026-11-22): CRUD on drafts + audience preview + filter
options. Send flow, FCM dispatch, response capture, analytics, CSV
export all land in Phase 2B.

Role gates (reused from clients/router.py::_assert_ca_or_rm):
- CA or CLIENT_RM of this client: full write access.
- SA email: full access everywhere.
- CM with EDIT rights on this client: full access (project
  convention — CM inherits every in-client role).
- REPORT_USER: view only (Phase 2B; not reachable here yet since
  these endpoints are mostly write).

Feature-flag gate: every endpoint asserts
`client_has_feature(db, client_id, "messaging")` before any work.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.clients.models import Client
from app.modules.platform.models import User
from app.modules.feature_access.models import FEATURE_MESSAGING
from app.modules.feature_access.service import client_has_feature
from app.modules.messaging.models import (
    Campaign, CampaignAttachment, CampaignQuestion, CampaignRecipient,
    CampaignResponse, CampaignResponseAnswer, CampaignSendAudit,
    CampaignStatus, CampaignType, RecipientRole,
    QuestionType, SUBSCRIPTION_STATUS_FILTER_VALUES,
)
from app.modules.messaging.service import (
    audience_count_with_exclusions,
    compute_audience_user_ids,
)


router = APIRouter(tags=["Messaging"])


# ── Role + feature gates ─────────────────────────────────────────────────

async def _assert_messaging_access(
    db: AsyncSession, user: User, client_id: str,
) -> None:
    """CA / CLIENT_RM / SA / CM-EDIT may use Messaging. Also checks the
    Feature Access flag — if the client doesn't have the messaging
    add-on, respond 402-Payment-Required-equivalent as 403 with a
    stable error code so the CP can render a nice empty state."""
    # Delegate role check to the Farmer Support gate — same role set.
    from app.modules.clients.router import _assert_ca_or_rm
    await _assert_ca_or_rm(db, user, client_id)

    if not await client_has_feature(db, client_id, FEATURE_MESSAGING):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "feature_not_enabled",
                "feature": FEATURE_MESSAGING,
                "message": (
                    "The Messaging Module is not enabled for this company. "
                    "Contact your Neytiri representative to activate it."
                ),
            },
        )


def _assert_client_exists(client: Optional[Client]) -> None:
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")


def _assert_valid_campaign_type(value: str) -> None:
    valid = {t.value for t in CampaignType}
    if value not in valid:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_campaign_type",
                "message": f"campaign_type must be one of {sorted(valid)}.",
            },
        )


def _assert_valid_recipient_role(value: str) -> None:
    valid = {r.value for r in RecipientRole}
    if value not in valid:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_recipient_role",
                "message": f"recipient_role must be one of {sorted(valid)}.",
            },
        )


def _normalise_audience_filters(raw: Optional[dict]) -> dict:
    """Keep only the known filter keys; drop empties; validate
    subscription_status vocabulary. Unknown keys are silently
    dropped so the frontend can't smuggle extra conditions in."""
    allowed = {
        "state_cosh_id", "district_cosh_id", "crop_cosh_id",
        "subscription_status", "promoter_user_id",
    }
    out: dict = {}
    for k, v in (raw or {}).items():
        if k not in allowed:
            continue
        if v is None or v == "":
            continue
        out[k] = v
    sub = out.get("subscription_status")
    if sub is not None and sub not in SUBSCRIPTION_STATUS_FILTER_VALUES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_subscription_status",
                "message": (
                    f"subscription_status must be one of "
                    f"{sorted(SUBSCRIPTION_STATUS_FILTER_VALUES)}."
                ),
            },
        )
    return out


# ── Body schemas ────────────────────────────────────────────────────────

class DraftIn(BaseModel):
    """Create or update a draft. All fields optional on update except
    those that are already set — the endpoint patches rather than
    replaces so the composer can save-as-you-go."""
    name: Optional[str] = Field(None, max_length=200)
    campaign_type: Optional[str] = None
    recipient_role: Optional[str] = None
    description: Optional[str] = None
    title: Optional[str] = Field(None, max_length=200)
    body: Optional[str] = None
    language_code: Optional[str] = Field(None, max_length=10)
    audience_filters: Optional[dict] = None
    rating_scale_type: Optional[str] = None


class AudiencePreviewIn(BaseModel):
    recipient_role: str
    audience_filters: dict = Field(default_factory=dict)


# ── Shapers ─────────────────────────────────────────────────────────────

def _shape_campaign(c: Campaign) -> dict:
    return {
        "id": c.id,
        "client_id": c.client_id,
        "name": c.name,
        "campaign_type": c.campaign_type,
        "recipient_role": c.recipient_role,
        "description": c.description,
        "title": c.title,
        "body": c.body,
        "language_code": c.language_code,
        "audience_filters": c.audience_filters or {},
        "rating_scale_type": c.rating_scale_type,
        "status": c.status,
        "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None,
        "sent_at": c.sent_at.isoformat() if c.sent_at else None,
        "expires_at": c.expires_at.isoformat() if c.expires_at else None,
        "cancelled_at": c.cancelled_at.isoformat() if c.cancelled_at else None,
        "archived_at": c.archived_at.isoformat() if c.archived_at else None,
        "recipient_count": c.recipient_count,
        "pushed_count": c.pushed_count,
        "opened_count": c.opened_count,
        "responded_count": c.responded_count,
        "created_by_user_id": c.created_by_user_id,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


# ── List ─────────────────────────────────────────────────────────────────

@router.get("/client/{cid}/messaging/campaigns")
async def list_campaigns(
    cid: str,
    status: Optional[str] = Query(
        None,
        description="Filter by status: DRAFT / SCHEDULED / ACTIVE / "
                    "EXPIRED / CANCELLED / ARCHIVED. Omit for all except "
                    "ARCHIVED (default list hides archived).",
    ),
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all campaigns for this client, newest first. Archived
    hidden by default per spec §3.10 — default list shows Active,
    Scheduled, Draft + last 90 days of terminated."""
    await _assert_messaging_access(db, current_user, cid)

    stmt = select(Campaign).where(Campaign.client_id == cid)
    if status:
        valid = {s.value for s in CampaignStatus}
        if status not in valid:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_status", "message": f"status must be one of {sorted(valid)}."},
            )
        stmt = stmt.where(Campaign.status == status)
    elif not include_archived:
        stmt = stmt.where(Campaign.status != CampaignStatus.ARCHIVED.value)

    stmt = stmt.order_by(Campaign.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return {"campaigns": [_shape_campaign(c) for c in rows]}


# ── Get one ─────────────────────────────────────────────────────────────

@router.get("/client/{cid}/messaging/campaigns/{campaign_id}")
async def get_campaign(
    cid: str, campaign_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full campaign shape including questions (for SURVEY and
    FEEDBACK follow-ups). Attachment list ships in Phase 2D."""
    await _assert_messaging_access(db, current_user, cid)

    c = (await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id,
            Campaign.client_id == cid,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    q_rows = (await db.execute(
        select(CampaignQuestion)
        .where(CampaignQuestion.campaign_id == campaign_id)
        .order_by(CampaignQuestion.sort_order)
    )).scalars().all()
    questions = [
        {
            "id": q.id,
            "sort_order": q.sort_order,
            "question_type": q.question_type,
            "label": q.label,
            "help_text": q.help_text,
            "is_mandatory": q.is_mandatory,
            "choice_options": q.choice_options,
            "number_unit": q.number_unit,
            "rating_scale_type": q.rating_scale_type,
        }
        for q in q_rows
    ]
    return {"campaign": _shape_campaign(c), "questions": questions}


# ── Create draft ────────────────────────────────────────────────────────

@router.post("/client/{cid}/messaging/campaigns")
async def create_draft(
    cid: str,
    body: DraftIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new DRAFT campaign.

    Mandatory on create: name, campaign_type, recipient_role, title,
    body. Audience filters, questions, attachments, and schedule are
    all filled in by subsequent PUT / dedicated endpoints as the
    composer walks forward.
    """
    await _assert_messaging_access(db, current_user, cid)

    client = (await db.execute(
        select(Client).where(Client.id == cid)
    )).scalar_one_or_none()
    _assert_client_exists(client)

    # Minimum viable draft — only the three structural fields are
    # required at CREATE. title + body are work-in-progress fields
    # that the composer fills on the Content step; they're validated
    # non-empty at SEND time (see send_campaign). Lets the Basics
    # step save the draft before the CA writes a word of content.
    missing = [k for k in ("name", "campaign_type", "recipient_role")
               if getattr(body, k) is None or (isinstance(getattr(body, k), str) and not getattr(body, k).strip())]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "missing_fields",
                "message": f"Required to create a draft: {missing}",
            },
        )
    _assert_valid_campaign_type(body.campaign_type)
    _assert_valid_recipient_role(body.recipient_role)

    now = datetime.now(timezone.utc)
    c = Campaign(
        client_id=cid,
        name=body.name.strip(),
        campaign_type=body.campaign_type,
        recipient_role=body.recipient_role,
        description=body.description,
        title=(body.title or "").strip(),
        body=body.body or "",
        language_code=body.language_code,
        audience_filters=_normalise_audience_filters(body.audience_filters),
        rating_scale_type=body.rating_scale_type,
        status=CampaignStatus.DRAFT.value,
        created_by_user_id=current_user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return {"campaign": _shape_campaign(c)}


# ── Update draft ────────────────────────────────────────────────────────

@router.patch("/client/{cid}/messaging/campaigns/{campaign_id}")
async def update_draft(
    cid: str, campaign_id: str,
    body: DraftIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Patch a DRAFT. Sent (ACTIVE/EXPIRED) campaigns are immutable —
    duplicate to change content."""
    await _assert_messaging_access(db, current_user, cid)

    c = (await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id,
            Campaign.client_id == cid,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if c.status != CampaignStatus.DRAFT.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "not_a_draft",
                "message": "Only DRAFT campaigns can be edited. Duplicate to change content.",
            },
        )

    if body.campaign_type is not None:
        _assert_valid_campaign_type(body.campaign_type)
        c.campaign_type = body.campaign_type
    if body.recipient_role is not None:
        _assert_valid_recipient_role(body.recipient_role)
        c.recipient_role = body.recipient_role
    if body.name is not None:
        c.name = body.name.strip()
    if body.description is not None:
        c.description = body.description
    if body.title is not None:
        c.title = body.title.strip()
    if body.body is not None:
        c.body = body.body
    if body.language_code is not None:
        c.language_code = body.language_code
    if body.audience_filters is not None:
        c.audience_filters = _normalise_audience_filters(body.audience_filters)
    if body.rating_scale_type is not None:
        c.rating_scale_type = body.rating_scale_type
    c.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(c)
    return {"campaign": _shape_campaign(c)}


# ── Delete draft ────────────────────────────────────────────────────────

@router.delete("/client/{cid}/messaging/campaigns/{campaign_id}")
async def delete_draft(
    cid: str, campaign_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hard-delete a DRAFT. Non-drafts cannot be deleted — cancel
    (if SCHEDULED) or archive (otherwise)."""
    await _assert_messaging_access(db, current_user, cid)

    c = (await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id,
            Campaign.client_id == cid,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if c.status != CampaignStatus.DRAFT.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "not_a_draft",
                "message": "Only DRAFT campaigns can be hard-deleted. Use archive or cancel for other states.",
            },
        )
    await db.delete(c)
    await db.commit()
    return {"deleted": True}


# ── Audience preview ────────────────────────────────────────────────────

@router.post("/client/{cid}/messaging/audience-preview")
async def audience_preview(
    cid: str,
    body: AudiencePreviewIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return `{matched, excluded_missing_district}` for the current
    filter set. Used by the composer's Audience step for the live
    count and by the Preview step's "excluded due to missing data"
    breakdown.

    `matched` = actual recipients the campaign would deliver to.
    `excluded_missing_district` = count of matched users with no
    district on file; informational only (they still receive).
    """
    await _assert_messaging_access(db, current_user, cid)
    _assert_valid_recipient_role(body.recipient_role)
    filters = _normalise_audience_filters(body.audience_filters)
    return await audience_count_with_exclusions(
        db, cid, body.recipient_role, filters,
    )


# ── Filter options ──────────────────────────────────────────────────────

@router.get("/client/{cid}/messaging/filter-options")
async def filter_options(
    cid: str,
    recipient_role: str = Query(
        ..., description="FARMER | DEALER | FACILITATOR — scopes the "
                         "options to recipients of that role.",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Options for the audience chip dropdowns. Returns cosh_id +
    display name per option, sorted by name.

    `recipient_role=FARMER` returns the full set (states, districts,
    crops, promoters, subscription_status values).
    DEALER / FACILITATOR return only location (states + districts) —
    crops / subscription_status / promoter don't apply.
    """
    await _assert_messaging_access(db, current_user, cid)
    _assert_valid_recipient_role(recipient_role)

    # Pull the set of recipients first (so options are limited to
    # what actually exists in this audience — avoids showing states
    # with zero farmers, for example).
    user_ids = await compute_audience_user_ids(
        db, cid, recipient_role, filters={},
    )
    if not user_ids:
        return {
            "states": [],
            "districts": [],
            "crops": [] if recipient_role == RecipientRole.FARMER.value else None,
            "promoters": [] if recipient_role == RecipientRole.FARMER.value else None,
            "subscription_statuses": sorted(SUBSCRIPTION_STATUS_FILTER_VALUES) if recipient_role == RecipientRole.FARMER.value else None,
        }

    # Collect cosh_ids seen
    state_rows = (await db.execute(
        select(User.state_cosh_id).where(
            User.id.in_(user_ids),
            User.state_cosh_id.isnot(None),
        ).distinct()
    )).scalars().all()
    district_rows = (await db.execute(
        select(User.district_cosh_id).where(
            User.id.in_(user_ids),
            User.district_cosh_id.isnot(None),
        ).distinct()
    )).scalars().all()

    # Resolve names from Cosh core
    from app.modules.sync.models import CoshCoreItem
    cosh_ids = set(state_rows) | set(district_rows)
    name_by_id: dict[str, str] = {}
    if cosh_ids:
        cores = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(cosh_ids))
        )).all()
        for cid_, trans in cores:
            name_by_id[cid_] = (trans or {}).get("en") or cid_

    def _pack(ids: list[str]) -> list[dict]:
        return sorted(
            [{"id": i, "name": name_by_id.get(i, i)} for i in ids],
            key=lambda o: o["name"].casefold(),
        )

    out: dict = {
        "states": _pack(list(set(state_rows))),
        "districts": _pack(list(set(district_rows))),
    }

    if recipient_role == RecipientRole.FARMER.value:
        # Crops from subscriptions
        from app.modules.subscriptions.models import Subscription
        from app.modules.advisory.models import Package
        crop_rows = (await db.execute(
            select(Package.crop_cosh_id)
            .join(Subscription, Subscription.package_id == Package.id)
            .where(
                Subscription.client_id == cid,
                Subscription.farmer_user_id.in_(user_ids),
                Subscription.deleted_at.is_(None),
                Package.crop_cosh_id.isnot(None),
            ).distinct()
        )).scalars().all()
        crop_ids = set(crop_rows)
        if crop_ids:
            cores = (await db.execute(
                select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
                .where(CoshCoreItem.cosh_id.in_(crop_ids))
            )).all()
            for cid_, trans in cores:
                name_by_id[cid_] = (trans or {}).get("en") or cid_
        out["crops"] = _pack(list(crop_ids))

        # Promoters currently attached to any sub of this client
        from app.modules.platform.models import User as _U
        promoter_rows = (await db.execute(
            select(_U.id, _U.name).distinct()
            .join(Subscription, Subscription.promoter_user_id == _U.id)
            .where(
                Subscription.client_id == cid,
                Subscription.deleted_at.is_(None),
                Subscription.promoter_user_id.isnot(None),
            )
        )).all()
        out["promoters"] = sorted(
            [{"id": uid, "name": name or uid} for uid, name in promoter_rows],
            key=lambda o: o["name"].casefold(),
        )

        out["subscription_statuses"] = sorted(SUBSCRIPTION_STATUS_FILTER_VALUES)
    else:
        out["crops"] = None
        out["promoters"] = None
        out["subscription_statuses"] = None

    return out


# ── Feature-flag-aware access check (public for CP to probe) ────────────

@router.get("/client/{cid}/messaging/access")
async def messaging_access(
    cid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns `{enabled: bool, expires_at: str|null, days_remaining: int|null}`
    so the CP can decide whether to render the Messaging nav item,
    the Expiring Soon banner, and so on. Does NOT 403 when feature
    is off — just returns `enabled=false`. Role gate still applies
    (CA/RM/CM-EDIT/SA only)."""
    # Role gate only (no feature gate — we're reporting the state).
    from app.modules.clients.router import _assert_ca_or_rm
    await _assert_ca_or_rm(db, current_user, cid)

    from app.modules.feature_access.service import latest_feature_row, row_is_active
    latest = await latest_feature_row(db, cid, FEATURE_MESSAGING)
    if latest is None or not row_is_active(latest):
        return {"enabled": False, "expires_at": None, "days_remaining": None}
    now = datetime.now(timezone.utc)
    remaining = int((latest.access_until - now).total_seconds() // 86400)
    return {
        "enabled": True,
        "expires_at": latest.access_until.isoformat(),
        "days_remaining": remaining,
    }


# ── Question CRUD ────────────────────────────────────────────────────────

class QuestionIn(BaseModel):
    question_type: str
    label: str = Field(..., max_length=500)
    help_text: Optional[str] = Field(None, max_length=500)
    is_mandatory: bool = False
    choice_options: Optional[list[str]] = None
    number_unit: Optional[str] = Field(None, max_length=50)
    rating_scale_type: Optional[str] = None


class QuestionReorderIn(BaseModel):
    """[{id, sort_order}, ...] — complete list in intended order. Any
    question ids not in the payload are left untouched."""
    ordering: list[dict]


def _assert_question_type(value: str) -> None:
    valid = {q.value for q in QuestionType}
    if value not in valid:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_question_type",
                "message": f"question_type must be one of {sorted(valid)}.",
            },
        )


def _validate_question_shape(q: QuestionIn) -> None:
    """Enforce per-type field requirements. Keeps the router honest so
    the composer can rely on the shape."""
    _assert_question_type(q.question_type)
    if q.question_type in (QuestionType.SINGLE_CHOICE.value, QuestionType.MULTIPLE_CHOICE.value):
        if not q.choice_options or len(q.choice_options) < 2:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "choice_options_required",
                    "message": "Single/Multiple choice questions need at least 2 options.",
                },
            )
        if len(q.choice_options) > 10:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "too_many_choice_options",
                    "message": "A choice question can have at most 10 options.",
                },
            )
        # 2026-11-22 audit — MULTIPLE_CHOICE answers are stored as
        # comma-joined strings in CampaignResponseAnswer.value_choice;
        # the aggregate tally in campaign_analytics splits on comma.
        # An option label containing a comma would be split into
        # phantom options and corrupt the tally. Reject at validation
        # so CAs can't trip the bug. The commas-in-options workaround
        # is "use a dash or semicolon".
        for opt in q.choice_options:
            if "," in opt:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "choice_option_contains_comma",
                        "message": (
                            f"Option labels can't contain commas (storage format "
                            f"ambiguity). Rephrase: '{opt}' — try using a dash "
                            f"or semicolon instead."
                        ),
                    },
                )
    if q.question_type == QuestionType.NUMBER.value and not q.number_unit:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "number_unit_required",
                "message": "Number questions need a unit (quintals, kg, Rs., …).",
            },
        )
    if q.question_type == QuestionType.RATING_SCALE.value:
        rs = (q.rating_scale_type or "STAR_5").upper()
        if rs not in ("STAR_5", "NPS_10"):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_rating_scale",
                    "message": "rating_scale_type must be STAR_5 or NPS_10.",
                },
            )


async def _get_draft_or_422(
    db: AsyncSession, cid: str, campaign_id: str,
) -> Campaign:
    c = (await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id,
            Campaign.client_id == cid,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if c.status != CampaignStatus.DRAFT.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "not_a_draft",
                "message": "Questions can only be edited on DRAFT campaigns.",
            },
        )
    return c


def _assert_campaign_has_questions(c: Campaign) -> None:
    """ANNOUNCEMENT + INTEREST_CAPTURE don't use the question table."""
    if c.campaign_type == CampaignType.ANNOUNCEMENT.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "campaign_type_has_no_questions",
                "message": "Announcement campaigns don't collect responses. Change campaign_type to SURVEY or FEEDBACK.",
            },
        )
    if c.campaign_type == CampaignType.INTEREST_CAPTURE.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "campaign_type_has_no_questions",
                "message": "Interest Capture uses a built-in Yes/No/Maybe — no extra questions.",
            },
        )


def _shape_question(q: CampaignQuestion) -> dict:
    return {
        "id": q.id,
        "sort_order": q.sort_order,
        "question_type": q.question_type,
        "label": q.label,
        "help_text": q.help_text,
        "is_mandatory": q.is_mandatory,
        "choice_options": q.choice_options,
        "number_unit": q.number_unit,
        "rating_scale_type": q.rating_scale_type,
    }


@router.post("/client/{cid}/messaging/campaigns/{campaign_id}/questions")
async def add_question(
    cid: str, campaign_id: str, body: QuestionIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Append a new question to a DRAFT SURVEY/FEEDBACK campaign.

    v1 cap: 20 questions per survey (per spec §4.4.1). Enforced here
    so the composer doesn't need to pre-count."""
    await _assert_messaging_access(db, current_user, cid)
    c = await _get_draft_or_422(db, cid, campaign_id)
    _assert_campaign_has_questions(c)
    _validate_question_shape(body)

    count = len((await db.execute(
        select(CampaignQuestion.id).where(
            CampaignQuestion.campaign_id == campaign_id,
        )
    )).scalars().all())
    if count >= 20:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "too_many_questions",
                "message": "A survey can have at most 20 questions.",
            },
        )

    q = CampaignQuestion(
        campaign_id=campaign_id,
        sort_order=count,  # append to end
        question_type=body.question_type,
        label=body.label.strip(),
        help_text=body.help_text,
        is_mandatory=bool(body.is_mandatory),
        choice_options=body.choice_options,
        number_unit=body.number_unit,
        rating_scale_type=body.rating_scale_type,
    )
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return _shape_question(q)


@router.patch("/client/{cid}/messaging/campaigns/{campaign_id}/questions/{qid}")
async def update_question(
    cid: str, campaign_id: str, qid: str, body: QuestionIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_messaging_access(db, current_user, cid)
    c = await _get_draft_or_422(db, cid, campaign_id)
    _assert_campaign_has_questions(c)
    _validate_question_shape(body)

    q = (await db.execute(
        select(CampaignQuestion).where(
            CampaignQuestion.id == qid,
            CampaignQuestion.campaign_id == campaign_id,
        )
    )).scalar_one_or_none()
    if q is None:
        raise HTTPException(status_code=404, detail="Question not found")

    q.question_type = body.question_type
    q.label = body.label.strip()
    q.help_text = body.help_text
    q.is_mandatory = bool(body.is_mandatory)
    q.choice_options = body.choice_options
    q.number_unit = body.number_unit
    q.rating_scale_type = body.rating_scale_type
    await db.commit()
    await db.refresh(q)
    return _shape_question(q)


@router.delete("/client/{cid}/messaging/campaigns/{campaign_id}/questions/{qid}")
async def delete_question(
    cid: str, campaign_id: str, qid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_messaging_access(db, current_user, cid)
    c = await _get_draft_or_422(db, cid, campaign_id)
    q = (await db.execute(
        select(CampaignQuestion).where(
            CampaignQuestion.id == qid,
            CampaignQuestion.campaign_id == campaign_id,
        )
    )).scalar_one_or_none()
    if q is None:
        raise HTTPException(status_code=404, detail="Question not found")
    await db.delete(q)
    await db.commit()
    return {"deleted": True}


@router.put("/client/{cid}/messaging/campaigns/{campaign_id}/questions/reorder")
async def reorder_questions(
    cid: str, campaign_id: str, body: QuestionReorderIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Apply a new ordering. Body: `{ordering: [{id, sort_order}, ...]}`.
    Any question id not in the payload is left untouched (useful when
    the composer only shuffles a subset)."""
    await _assert_messaging_access(db, current_user, cid)
    _ = await _get_draft_or_422(db, cid, campaign_id)

    by_id = {r["id"]: int(r["sort_order"]) for r in body.ordering if "id" in r and "sort_order" in r}
    if not by_id:
        return {"updated": 0}

    rows = (await db.execute(
        select(CampaignQuestion).where(
            CampaignQuestion.campaign_id == campaign_id,
            CampaignQuestion.id.in_(list(by_id.keys())),
        )
    )).scalars().all()
    for q in rows:
        q.sort_order = by_id[q.id]
    await db.commit()
    return {"updated": len(rows)}


# ── Send flow ────────────────────────────────────────────────────────────

def _default_expires_at(
    now: datetime, days: int = 1,
) -> datetime:
    """Spec §3.1 — minimum 1 day. If a CA doesn't specify, default to
    1 day from send so the campaign stays reachable overnight."""
    from datetime import timedelta
    return now + timedelta(days=max(1, days))


class SendIn(BaseModel):
    # When None, defaults to send-now (not Scheduled).
    send_at: Optional[datetime] = None
    # When None, defaults to send_at + 7 days (sensible middle ground
    # between the 1-day floor and the 180-day cap). Caller can
    # override — the preview step surfaces this.
    expires_at: Optional[datetime] = None
    # True → test-send to a small list of test_user_ids (1-5 recipients).
    # Doesn't affect campaign status; writes CampaignRecipient rows
    # with is_test=True which are excluded from analytics.
    is_test: bool = False
    test_user_ids: Optional[list[str]] = None


async def _snapshot_recipients(
    db: AsyncSession, campaign: Campaign,
    user_ids: list[str], is_test: bool = False,
) -> int:
    """Materialise `user_ids` into CampaignRecipient rows. Idempotent:
    users already in campaign_recipients for this campaign are skipped
    (so a re-run on a test-send doesn't duplicate rows).

    Returns the number of NEW rows written.
    """
    if not user_ids:
        return 0

    # Skip existing
    existing = (await db.execute(
        select(CampaignRecipient.user_id).where(
            CampaignRecipient.campaign_id == campaign.id,
            CampaignRecipient.user_id.in_(user_ids),
        )
    )).scalars().all()
    existing_set = set(existing)
    new_ids = [u for u in user_ids if u not in existing_set]
    if not new_ids:
        return 0

    # Hydrate denorm fields from User
    users = (await db.execute(
        select(User).where(User.id.in_(new_ids))
    )).scalars().all()
    for u in users:
        db.add(CampaignRecipient(
            campaign_id=campaign.id,
            user_id=u.id,
            recipient_name=(u.name or "")[:200],
            recipient_phone=(u.phone or "")[:20],
            recipient_language_code=u.language_code,
            recipient_state_cosh_id=u.state_cosh_id,
            recipient_district_cosh_id=u.district_cosh_id,
            is_test=is_test,
        ))
    await db.flush()
    return len(users)


async def _write_send_audit(
    db: AsyncSession, campaign: Campaign, user: User,
    recipient_count: int, is_test: bool,
) -> None:
    """One row per send event — append-only. Mirrors the
    report_csv_downloads pattern."""
    db.add(CampaignSendAudit(
        campaign_id=campaign.id,
        client_id=campaign.client_id,
        sender_user_id=user.id,
        campaign_name=campaign.name[:200],
        campaign_type=campaign.campaign_type,
        recipient_role=campaign.recipient_role,
        recipient_count=recipient_count,
        is_test=is_test,
    ))
    await db.flush()


@router.post("/client/{cid}/messaging/campaigns/{campaign_id}/send")
async def send_campaign(
    cid: str, campaign_id: str, body: SendIn = SendIn(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send (or schedule) a DRAFT campaign.

    - `is_test=True` + `test_user_ids=[...]`: writes test recipients
      (is_test=True), fires dispatch for just those, campaign stays in
      DRAFT so the CA can edit + do the real send afterwards.
    - `send_at` in the future: campaign moves to SCHEDULED; a daily
      beat tick (feature_access_expiry style) flips it to ACTIVE when
      the time comes. For v1 the scheduler is a stub — real scheduling
      lands in a later round; for now `send_at` must be None or past.
    - `expires_at` defaults to send_at + 7 days; floor is send_at + 1 day.
    """
    from datetime import timedelta
    await _assert_messaging_access(db, current_user, cid)
    c = (await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id, Campaign.client_id == cid,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Test-send path — doesn't touch status, doesn't bump counters.
    if body.is_test:
        if not body.test_user_ids or not (1 <= len(body.test_user_ids) <= 5):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "test_user_ids_required",
                    "message": "Test send requires 1-5 user ids in test_user_ids.",
                },
            )
        snapped = await _snapshot_recipients(
            db, c, body.test_user_ids, is_test=True,
        )
        await _write_send_audit(db, c, current_user, snapped, is_test=True)
        await db.commit()
        # Fire the Celery dispatch for this test batch.
        from app.tasks.campaign_dispatch import dispatch_campaign
        dispatch_campaign.delay(c.id, test_only=True)
        return {"sent": True, "recipient_count": snapped, "is_test": True}

    # Real send path — campaign must be DRAFT.
    if c.status != CampaignStatus.DRAFT.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "not_a_draft",
                "message": f"Only DRAFT campaigns can be sent. Current status: {c.status}.",
            },
        )

    # Content check — title + body are optional at CREATE (so the
    # composer can save-as-you-go from Basics before any content is
    # typed) but mandatory at SEND. Returns a stable error code so the
    # composer can flag which field is missing.
    content_missing = []
    if not c.title or not c.title.strip():
        content_missing.append("title")
    if not c.body or not c.body.strip():
        content_missing.append("body")
    if content_missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "campaign_content_incomplete",
                "message": (
                    f"Fill in the content first: {content_missing}. "
                    "Go back to the Content step."
                ),
                "missing": content_missing,
            },
        )

    now = datetime.now(timezone.utc)

    # Scheduling path (2026-11-22 Phase 2E) — send_at in the future
    # flips to SCHEDULED; the campaign_schedule beat task activates
    # it at the right time. Audience is NOT snapshotted at this
    # point — snapshot runs at activation so the recipient list
    # reflects reality at the send moment.
    if body.send_at is not None and body.send_at > now:
        # Reject sends scheduled > 180 days out (matches expiry cap
        # semantics).
        from datetime import timedelta as _td
        if body.send_at > now + _td(days=180):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "send_at_too_far",
                    "message": "send_at must be at most 180 days from now.",
                },
            )
        # Compute + validate expires_at relative to send_at, not now.
        scheduled_expires = body.expires_at or (body.send_at + timedelta(days=7))
        if scheduled_expires < body.send_at + timedelta(days=1):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "expires_too_soon",
                    "message": "expires_at must be at least 1 day after send_at.",
                },
            )
        if scheduled_expires > body.send_at + timedelta(days=180):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "expires_too_far",
                    "message": "expires_at must be at most 180 days after send_at.",
                },
            )
        c.status = CampaignStatus.SCHEDULED.value
        c.scheduled_at = body.send_at
        c.expires_at = scheduled_expires
        c.updated_at = now
        await db.commit()
        return {
            "sent": False,
            "scheduled": True,
            "scheduled_at": body.send_at.isoformat(),
            "expires_at": scheduled_expires.isoformat(),
        }

    # Compute expiry — floor at now + 1 day per spec §3.1.
    expires = body.expires_at or (now + timedelta(days=7))
    min_expires = now + timedelta(days=1)
    if expires < min_expires:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "expires_too_soon",
                "message": "expires_at must be at least 1 day after send.",
            },
        )
    # Hard cap 180 days per spec.
    max_expires = now + timedelta(days=180)
    if expires > max_expires:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "expires_too_far",
                "message": "expires_at must be at most 180 days after send.",
            },
        )

    # Snapshot the audience.
    user_ids = await compute_audience_user_ids(
        db, cid, c.recipient_role, c.audience_filters or {},
    )
    if not user_ids:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "empty_audience",
                "message": "No recipients match the current filters.",
            },
        )
    snapped = await _snapshot_recipients(db, c, user_ids, is_test=False)

    # Flip status, stamp counters.
    c.status = CampaignStatus.ACTIVE.value
    c.sent_at = now
    c.expires_at = expires
    c.recipient_count = snapped
    c.updated_at = now

    await _write_send_audit(db, c, current_user, snapped, is_test=False)
    await db.commit()

    # Fire the Celery dispatch.
    from app.tasks.campaign_dispatch import dispatch_campaign
    dispatch_campaign.delay(c.id, test_only=False)

    return {
        "sent": True,
        "recipient_count": snapped,
        "sent_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }


# ── Lifecycle actions ───────────────────────────────────────────────────

@router.post("/client/{cid}/messaging/campaigns/{campaign_id}/cancel")
async def cancel_campaign(
    cid: str, campaign_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cancel a SCHEDULED campaign before its send time. ACTIVE
    campaigns cannot be cancelled — they're already delivering."""
    await _assert_messaging_access(db, current_user, cid)
    c = (await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id, Campaign.client_id == cid,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if c.status != CampaignStatus.SCHEDULED.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "not_cancellable",
                "message": f"Only SCHEDULED campaigns can be cancelled. Current status: {c.status}.",
            },
        )
    now = datetime.now(timezone.utc)
    c.status = CampaignStatus.CANCELLED.value
    c.cancelled_at = now
    c.updated_at = now
    await db.commit()
    return _shape_campaign(c)


@router.post("/client/{cid}/messaging/campaigns/{campaign_id}/archive")
async def archive_campaign(
    cid: str, campaign_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Archive a terminated campaign. ACTIVE campaigns can't be
    archived — wait for expiry or cancel-then-archive."""
    await _assert_messaging_access(db, current_user, cid)
    c = (await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id, Campaign.client_id == cid,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if c.status in (
        CampaignStatus.ACTIVE.value,
        CampaignStatus.SCHEDULED.value,
        CampaignStatus.DRAFT.value,
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "not_archivable",
                "message": (
                    "Only terminated campaigns (Expired or Cancelled) can be "
                    "archived. Drafts should be discarded instead; active and "
                    "scheduled campaigns must run their course or be cancelled first."
                ),
            },
        )
    now = datetime.now(timezone.utc)
    c.status = CampaignStatus.ARCHIVED.value
    c.archived_at = now
    c.updated_at = now
    await db.commit()
    return _shape_campaign(c)


# ── CA analytics summary ────────────────────────────────────────────────

@router.get("/client/{cid}/messaging/campaigns/{campaign_id}/analytics")
async def campaign_analytics(
    cid: str, campaign_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delivery summary + per-question aggregates (SURVEY / FEEDBACK /
    INTEREST_CAPTURE). Individual-response drill-down + CSV export
    ship in Phase 2D."""
    await _assert_messaging_access(db, current_user, cid)
    c = (await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id, Campaign.client_id == cid,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    from sqlalchemy import func

    # Delivery counts — exclude test recipients.
    total = (await db.execute(
        select(func.count(CampaignRecipient.id)).where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.is_test.is_(False),
        )
    )).scalar_one()
    pushed = (await db.execute(
        select(func.count(CampaignRecipient.id)).where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.is_test.is_(False),
            CampaignRecipient.pushed_at.isnot(None),
        )
    )).scalar_one()
    opened = (await db.execute(
        select(func.count(CampaignRecipient.id)).where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.is_test.is_(False),
            CampaignRecipient.opened_at.isnot(None),
        )
    )).scalar_one()
    responded = (await db.execute(
        select(func.count(CampaignRecipient.id)).where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.is_test.is_(False),
            CampaignRecipient.responded_at.isnot(None),
        )
    )).scalar_one()

    # INTEREST_CAPTURE — Yes/No/Maybe tally.
    interest = None
    if c.campaign_type == CampaignType.INTEREST_CAPTURE.value:
        interest_rows = (await db.execute(
            select(CampaignResponse.interest_answer, func.count(CampaignResponse.id))
            .where(CampaignResponse.campaign_id == campaign_id)
            .group_by(CampaignResponse.interest_answer)
        )).all()
        interest = {ans or "UNKNOWN": int(n) for ans, n in interest_rows}

    # FEEDBACK — rating histogram.
    feedback = None
    if c.campaign_type == CampaignType.FEEDBACK.value:
        rating_rows = (await db.execute(
            select(CampaignResponse.rating, func.count(CampaignResponse.id))
            .where(CampaignResponse.campaign_id == campaign_id)
            .group_by(CampaignResponse.rating)
        )).all()
        feedback = {str(r or "0"): int(n) for r, n in rating_rows}

    # SURVEY / FEEDBACK follow-ups — per-question aggregates.
    questions = (await db.execute(
        select(CampaignQuestion)
        .where(CampaignQuestion.campaign_id == campaign_id)
        .order_by(CampaignQuestion.sort_order)
    )).scalars().all()
    question_aggs = []
    for q in questions:
        agg: dict = {
            "question_id": q.id,
            "label": q.label,
            "question_type": q.question_type,
            "response_count": 0,
        }
        base = select(CampaignResponseAnswer).where(
            CampaignResponseAnswer.question_id == q.id,
        )
        all_rows = (await db.execute(base)).scalars().all()
        agg["response_count"] = len(all_rows)
        if q.question_type in (QuestionType.SINGLE_CHOICE.value, QuestionType.MULTIPLE_CHOICE.value):
            tally: dict[str, int] = {}
            for r in all_rows:
                raw = r.value_choice or ""
                if q.question_type == QuestionType.MULTIPLE_CHOICE.value:
                    # Stored as comma-joined; split.
                    for opt in [o.strip() for o in raw.split(",") if o.strip()]:
                        tally[opt] = tally.get(opt, 0) + 1
                else:
                    tally[raw] = tally.get(raw, 0) + 1
            agg["choice_tally"] = tally
        elif q.question_type == QuestionType.YES_NO.value:
            yes = sum(1 for r in all_rows if r.value_boolean is True)
            no = sum(1 for r in all_rows if r.value_boolean is False)
            agg["yes_no"] = {"YES": yes, "NO": no}
        elif q.question_type in (QuestionType.NUMBER.value, QuestionType.RATING_SCALE.value):
            nums = [r.value_number for r in all_rows if r.value_number is not None]
            if nums:
                agg["stats"] = {
                    "min": min(nums), "max": max(nums),
                    "mean": sum(nums) / len(nums), "count": len(nums),
                }
        # Short/paragraph/date/photo — no aggregation; just count.
        question_aggs.append(agg)

    return {
        "campaign": _shape_campaign(c),
        "delivery": {
            "recipient_count": total,
            "pushed": pushed,
            "opened": opened,
            "responded": responded,
        },
        "interest_capture": interest,
        "feedback_rating_histogram": feedback,
        "questions": question_aggs,
    }


# ── Individual response drill-down ──────────────────────────────────────

def _mask_phone(phone: str | None) -> str:
    """Last-4-only masking — same convention as the Queries report
    (shipped 2026-11-21). CA has Farmer Support for the full phone."""
    if not phone:
        return ""
    if len(phone) <= 4:
        return phone
    return "•" * (len(phone) - 4) + phone[-4:]


def _display_answer(
    answer: CampaignResponseAnswer,
    question: CampaignQuestion | None,
) -> str:
    """Flatten the polymorphic answer columns into one human-readable
    string. Used by both the JSON drill-down and the CSV export so
    the two views agree."""
    if answer.value_text is not None:
        return str(answer.value_text)
    if answer.value_number is not None:
        # Keep ints clean, decimals to 2 places max.
        n = answer.value_number
        if n == int(n):
            return str(int(n))
        return f"{n:.2f}".rstrip("0").rstrip(".")
    if answer.value_choice is not None:
        # MULTIPLE_CHOICE stored comma-joined; render as-is.
        return str(answer.value_choice)
    if answer.value_boolean is not None:
        return "Yes" if answer.value_boolean else "No"
    if answer.value_date is not None:
        return answer.value_date.isoformat()
    if answer.value_media_path is not None:
        # Media URL sent in a follow-up Phase 2E step; for now surface
        # the stored path so CAs know a photo was uploaded.
        return f"[photo: {answer.value_media_path}]"
    return ""


@router.get("/client/{cid}/messaging/campaigns/{campaign_id}/responses")
async def list_responses(
    cid: str, campaign_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Paginated per-recipient response list for the drill-down table
    on the campaign-detail page. Excludes test recipients from the
    count + rows so analytics stays clean."""
    from sqlalchemy import func
    await _assert_messaging_access(db, current_user, cid)
    c = (await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id, Campaign.client_id == cid,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Base: responses JOIN recipients to get denorm fields + skip test.
    total = (await db.execute(
        select(func.count(CampaignResponse.id))
        .join(CampaignRecipient, CampaignRecipient.id == CampaignResponse.recipient_id)
        .where(
            CampaignResponse.campaign_id == campaign_id,
            CampaignRecipient.is_test.is_(False),
        )
    )).scalar_one()

    offset = (page - 1) * page_size
    rows = (await db.execute(
        select(CampaignResponse, CampaignRecipient)
        .join(CampaignRecipient, CampaignRecipient.id == CampaignResponse.recipient_id)
        .where(
            CampaignResponse.campaign_id == campaign_id,
            CampaignRecipient.is_test.is_(False),
        )
        .order_by(CampaignResponse.submitted_at.desc())
        .offset(offset).limit(page_size)
    )).all()
    if not rows:
        return {
            "rows": [], "total": total, "page": page, "page_size": page_size,
        }

    # Hydrate answers in bulk — one query across all displayed responses.
    response_ids = [r.id for r, _ in rows]
    ans_rows = (await db.execute(
        select(CampaignResponseAnswer).where(
            CampaignResponseAnswer.response_id.in_(response_ids),
        )
    )).scalars().all()
    answers_by_response: dict[str, list[CampaignResponseAnswer]] = {}
    for a in ans_rows:
        answers_by_response.setdefault(a.response_id, []).append(a)

    # Hydrate question labels (for display + CSV header).
    q_rows = (await db.execute(
        select(CampaignQuestion)
        .where(CampaignQuestion.campaign_id == campaign_id)
        .order_by(CampaignQuestion.sort_order)
    )).scalars().all()
    q_by_id = {q.id: q for q in q_rows}

    # Hydrate state/district cosh → English for display.
    from app.modules.sync.models import CoshCoreItem
    cosh_ids = set()
    for _, r in rows:
        if r.recipient_state_cosh_id: cosh_ids.add(r.recipient_state_cosh_id)
        if r.recipient_district_cosh_id: cosh_ids.add(r.recipient_district_cosh_id)
    name_by_cosh: dict[str, str] = {}
    if cosh_ids:
        cores = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(cosh_ids))
        )).all()
        for cid_, trans in cores:
            name_by_cosh[cid_] = (trans or {}).get("en") or cid_

    shaped = []
    for resp, recipient in rows:
        ans_list = sorted(
            answers_by_response.get(resp.id, []),
            key=lambda a: q_by_id.get(a.question_id).sort_order if q_by_id.get(a.question_id) else 0,
        )
        shaped.append({
            "response_id": resp.id,
            "recipient_name": recipient.recipient_name,
            "recipient_phone_masked": _mask_phone(recipient.recipient_phone),
            "recipient_state": name_by_cosh.get(recipient.recipient_state_cosh_id or "", ""),
            "recipient_district": name_by_cosh.get(recipient.recipient_district_cosh_id or "", ""),
            "submitted_at": resp.submitted_at.isoformat() if resp.submitted_at else None,
            "interest_answer": resp.interest_answer,
            "rating": resp.rating,
            "answers": [
                {
                    "question_id": a.question_id,
                    "question_label": q_by_id.get(a.question_id).label if q_by_id.get(a.question_id) else "(removed)",
                    "question_type": q_by_id.get(a.question_id).question_type if q_by_id.get(a.question_id) else None,
                    "display_value": _display_answer(a, q_by_id.get(a.question_id)),
                }
                for a in ans_list
            ],
        })
    return {
        "rows": shaped, "total": total, "page": page, "page_size": page_size,
        "questions": [
            {"id": q.id, "label": q.label, "question_type": q.question_type, "sort_order": q.sort_order}
            for q in q_rows
        ],
    }


# ── CSV export (CA-only) ────────────────────────────────────────────────

@router.get("/client/{cid}/messaging/campaigns/{campaign_id}/export.csv")
async def export_responses_csv(
    cid: str, campaign_id: str,
    request: Request = None,  # noqa — FastAPI injects when declared
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CSV export of all responses for this campaign. CA-only (same
    gate as other CSV exports). Writes a row to `report_csv_downloads`
    with `report_name="messaging_v1"` so the SA Portal CSV audit
    viewer (shipped 2026-11-21) surfaces this download alongside
    every other audited CSV."""
    from app.modules.clients.router import _assert_ca_admin_or_cm_for_client
    import csv, io
    from fastapi.responses import StreamingResponse
    from datetime import datetime

    # Role: CA / CM-EDIT / SA only (not CLIENT_RM, not REPORT_USER —
    # mirror the CSV convention set in the Queries report).
    await _assert_ca_admin_or_cm_for_client(db, current_user.id, cid)
    # Feature flag.
    if not await client_has_feature(db, cid, FEATURE_MESSAGING):
        raise HTTPException(
            status_code=403,
            detail={"code": "feature_not_enabled", "message": "Messaging not enabled."},
        )

    c = (await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id, Campaign.client_id == cid,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Pull everything (no pagination on export).
    rows = (await db.execute(
        select(CampaignResponse, CampaignRecipient)
        .join(CampaignRecipient, CampaignRecipient.id == CampaignResponse.recipient_id)
        .where(
            CampaignResponse.campaign_id == campaign_id,
            CampaignRecipient.is_test.is_(False),
        )
        .order_by(CampaignResponse.submitted_at.desc())
    )).all()

    q_rows = (await db.execute(
        select(CampaignQuestion)
        .where(CampaignQuestion.campaign_id == campaign_id)
        .order_by(CampaignQuestion.sort_order)
    )).scalars().all()
    q_by_id = {q.id: q for q in q_rows}

    ans_by_response: dict[str, dict[str, CampaignResponseAnswer]] = {}
    if rows:
        response_ids = [r.id for r, _ in rows]
        ans_rows = (await db.execute(
            select(CampaignResponseAnswer).where(
                CampaignResponseAnswer.response_id.in_(response_ids),
            )
        )).scalars().all()
        for a in ans_rows:
            ans_by_response.setdefault(a.response_id, {})[a.question_id] = a

    # Hydrate cosh id → name for state/district cols.
    from app.modules.sync.models import CoshCoreItem
    cosh_ids = set()
    for _, r in rows:
        if r.recipient_state_cosh_id: cosh_ids.add(r.recipient_state_cosh_id)
        if r.recipient_district_cosh_id: cosh_ids.add(r.recipient_district_cosh_id)
    name_by_cosh: dict[str, str] = {}
    if cosh_ids:
        cores = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(cosh_ids))
        )).all()
        for cid_, trans in cores:
            name_by_cosh[cid_] = (trans or {}).get("en") or cid_

    # Build CSV in-memory. UTF-8 BOM + Excel sep hint (same conventions
    # we set in the Queries CSV helper).
    buf = io.StringIO()
    buf.write("sep=,\r\n")
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    header = [
        "Recipient Name", "Phone (masked)", "State", "District",
        "Submitted At",
    ]
    if c.campaign_type == CampaignType.INTEREST_CAPTURE.value:
        header.append("Interest Answer")
    if c.campaign_type == CampaignType.FEEDBACK.value:
        header.append("Rating")
    for q in q_rows:
        header.append(q.label)
    writer.writerow(header)

    for resp, recipient in rows:
        row = [
            recipient.recipient_name,
            _mask_phone(recipient.recipient_phone),
            name_by_cosh.get(recipient.recipient_state_cosh_id or "", ""),
            name_by_cosh.get(recipient.recipient_district_cosh_id or "", ""),
            resp.submitted_at.isoformat() if resp.submitted_at else "",
        ]
        if c.campaign_type == CampaignType.INTEREST_CAPTURE.value:
            row.append(resp.interest_answer or "")
        if c.campaign_type == CampaignType.FEEDBACK.value:
            row.append(str(resp.rating) if resp.rating is not None else "")
        for q in q_rows:
            a = ans_by_response.get(resp.id, {}).get(q.id)
            row.append(_display_answer(a, q) if a else "")
        writer.writerow(row)

    body = "﻿" + buf.getvalue()  # UTF-8 BOM prefix

    # Audit row — same append-only table as Queries CSV, so the SA
    # Portal CSV audit viewer surfaces this download automatically
    # with `report_name="messaging_v1"`.
    from app.modules.reports.audit import record_csv_download
    await record_csv_download(
        db,
        user=current_user,
        report_name="messaging_v1",
        client_id=cid,
        filters={
            "campaign_id": campaign_id,
            "campaign_name": c.name,
            "campaign_type": c.campaign_type,
            "recipient_role": c.recipient_role,
        },
        row_count=len(rows),
        request=request,
    )

    stamp = datetime.utcnow().strftime("%Y%m%d")
    filename = f"{cid[:8]}-{c.name[:30].replace(' ', '-')}-{stamp}.csv"
    resp = StreamingResponse(iter([body]), media_type="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


# ── Phone-based test-send (convenience wrapper) ─────────────────────────

class TestSendByPhoneIn(BaseModel):
    """Resolve each phone number to a user_id, then delegate to the
    regular send flow with is_test=true + test_user_ids. Keeps CAs
    from having to look up internal user_ids."""
    phones: list[str]


@router.post("/client/{cid}/messaging/campaigns/{campaign_id}/test-send")
async def test_send_by_phone(
    cid: str, campaign_id: str, body: TestSendByPhoneIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Test-send to 1-5 recipients identified by phone number.
    Returns `{resolved: [...], unresolved: [...]}` plus the result of
    the underlying send."""
    await _assert_messaging_access(db, current_user, cid)
    phones = [p.strip() for p in (body.phones or []) if p.strip()]
    if not phones:
        raise HTTPException(
            status_code=422,
            detail={"code": "no_phones", "message": "Enter at least one phone number."},
        )
    if len(phones) > 5:
        raise HTTPException(
            status_code=422,
            detail={"code": "too_many_phones", "message": "Test-send accepts at most 5 recipients."},
        )

    # Resolve phones → user_ids. Scoped to users who are associated
    # with THIS client: either a portal member (ClientUser), a
    # dealer/facilitator (ClientPromoter), or a farmer with a
    # subscription on this client. Prevents a CA from accidentally —
    # or deliberately — sending a test to a random user elsewhere
    # on the platform (2026-11-22 audit finding).
    from app.modules.clients.models import ClientUser, ClientPromoter
    from app.modules.subscriptions.models import Subscription
    from sqlalchemy import or_
    found = (await db.execute(
        select(User.id, User.phone)
        .where(
            User.phone.in_(phones),
            or_(
                select(ClientUser.id).where(
                    ClientUser.client_id == cid,
                    ClientUser.user_id == User.id,
                ).exists(),
                select(ClientPromoter.id).where(
                    ClientPromoter.client_id == cid,
                    ClientPromoter.user_id == User.id,
                    ClientPromoter.status == "ACTIVE",
                ).exists(),
                select(Subscription.id).where(
                    Subscription.client_id == cid,
                    Subscription.farmer_user_id == User.id,
                    Subscription.deleted_at.is_(None),
                ).exists(),
            ),
        )
    )).all()
    by_phone = {ph: uid for uid, ph in found}
    resolved_ids = [by_phone[p] for p in phones if p in by_phone]
    unresolved = [p for p in phones if p not in by_phone]
    if not resolved_ids:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_matching_phones",
                "message": "None of those phone numbers match a RootsTalk account.",
                "unresolved": unresolved,
            },
        )

    # Delegate to the main send flow.
    c = (await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id, Campaign.client_id == cid,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    snapped = await _snapshot_recipients(
        db, c, resolved_ids, is_test=True,
    )
    await _write_send_audit(db, c, current_user, snapped, is_test=True)
    await db.commit()
    from app.tasks.campaign_dispatch import dispatch_campaign
    dispatch_campaign.delay(c.id, test_only=True)

    return {
        "sent": True,
        "resolved_count": len(resolved_ids),
        "unresolved": unresolved,
        "is_test": True,
    }


# ── Attachments (Phase 2E, 2026-11-22) ──────────────────────────────────

# Per spec §3.3 — cap 3 attachments × 10 MB each per campaign.
# Allow-list: images (JPEG/PNG/WebP/GIF), video (MP4/WebM/QuickTime),
# PDFs. Audio deliberately out — campaigns aren't voice-note channels.
MESSAGING_ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024
MESSAGING_ATTACHMENT_MAX_COUNT = 3
MESSAGING_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MESSAGING_VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime"}
MESSAGING_PDF_TYPES = {"application/pdf"}
MESSAGING_ATTACHMENT_TYPES = (
    MESSAGING_IMAGE_TYPES | MESSAGING_VIDEO_TYPES | MESSAGING_PDF_TYPES
)


def _kind_for_mime(mime: str) -> str:
    base = (mime or "").split(";", 1)[0].strip()
    if base in MESSAGING_IMAGE_TYPES: return "IMAGE"
    if base in MESSAGING_VIDEO_TYPES: return "VIDEO"
    if base in MESSAGING_PDF_TYPES:   return "PDF"
    return "IMAGE"  # unreachable — validator rejects first


@router.post("/client/{cid}/messaging/campaigns/{campaign_id}/attachments")
async def add_attachment(
    cid: str, campaign_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload + attach a file to a DRAFT campaign. Delegates to the
    shared /media/upload S3 path (same pattern as logo uploads).
    Returns the created CampaignAttachment shape."""
    from app.modules.media.router import upload_to_s3

    await _assert_messaging_access(db, current_user, cid)
    c = await _get_draft_or_422(db, cid, campaign_id)

    count = len((await db.execute(
        select(CampaignAttachment.id).where(
            CampaignAttachment.campaign_id == campaign_id,
        )
    )).scalars().all())
    if count >= MESSAGING_ATTACHMENT_MAX_COUNT:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "too_many_attachments",
                "message": f"A campaign can have at most {MESSAGING_ATTACHMENT_MAX_COUNT} attachments.",
            },
        )

    # upload_to_s3 handles type + size validation.
    uploaded = await upload_to_s3(
        file,
        folder="messaging",
        allowed_types=MESSAGING_ATTACHMENT_TYPES,
        max_size_bytes=MESSAGING_ATTACHMENT_MAX_BYTES,
    )

    base_ct = (file.content_type or "").split(";", 1)[0].strip()
    row = CampaignAttachment(
        campaign_id=campaign_id,
        kind=_kind_for_mime(base_ct),
        storage_path=uploaded["url"],
        original_filename=(file.filename or "attachment")[:300],
        size_bytes=file.size or 0,
        mime_type=base_ct[:100],
        sort_order=count,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "id": row.id,
        "kind": row.kind,
        "url": row.storage_path,
        "original_filename": row.original_filename,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
        "sort_order": row.sort_order,
    }


@router.delete("/client/{cid}/messaging/campaigns/{campaign_id}/attachments/{attachment_id}")
async def remove_attachment(
    cid: str, campaign_id: str, attachment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove an attachment from a DRAFT. The S3 object itself is NOT
    deleted (attachments are small + lifecycle policies on the bucket
    handle cleanup if ever needed) — this just drops the row."""
    await _assert_messaging_access(db, current_user, cid)
    _ = await _get_draft_or_422(db, cid, campaign_id)
    row = (await db.execute(
        select(CampaignAttachment).where(
            CampaignAttachment.id == attachment_id,
            CampaignAttachment.campaign_id == campaign_id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": True}


@router.get("/client/{cid}/messaging/campaigns/{campaign_id}/attachments")
async def list_attachments(
    cid: str, campaign_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List attachments on a campaign (for the composer to show current
    state). CA-side read — gated like everything else."""
    await _assert_messaging_access(db, current_user, cid)
    rows = (await db.execute(
        select(CampaignAttachment)
        .where(CampaignAttachment.campaign_id == campaign_id)
        .order_by(CampaignAttachment.sort_order)
    )).scalars().all()
    return {
        "attachments": [
            {
                "id": r.id, "kind": r.kind, "url": r.storage_path,
                "original_filename": r.original_filename,
                "mime_type": r.mime_type, "size_bytes": r.size_bytes,
                "sort_order": r.sort_order,
            }
            for r in rows
        ],
    }
