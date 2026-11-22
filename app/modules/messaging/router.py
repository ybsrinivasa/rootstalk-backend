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

from fastapi import APIRouter, Depends, HTTPException, Query
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
    Campaign, CampaignQuestion, CampaignRecipient, CampaignResponse,
    CampaignResponseAnswer, CampaignSendAudit,
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

    # Minimum viable draft
    missing = [k for k in ("name", "campaign_type", "recipient_role", "title", "body")
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
        title=body.title.strip(),
        body=body.body,
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

    now = datetime.now(timezone.utc)
    # Scheduling deferred — reject future send_at for now.
    if body.send_at is not None and body.send_at > now:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "scheduling_not_yet_available",
                "message": "Scheduled sends are not yet supported. Leave send_at empty to send now.",
            },
        )

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
    if c.status in (CampaignStatus.ACTIVE.value, CampaignStatus.SCHEDULED.value):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "not_archivable",
                "message": "Active and scheduled campaigns cannot be archived.",
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
