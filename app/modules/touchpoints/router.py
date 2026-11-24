"""Subscription Touchpoints — Client Portal endpoints.

Phase T-B (2026-11-24): CRUD on drafts + lifecycle (activate / pause /
resume / archive) + assignment management + question CRUD + attachment
upload. Firing engine, PWA inbox integration, and analytics ship in
phases T-C → T-F.

Role gate: CA / CLIENT_RM / SA / CM-EDIT (reused from clients/router.py
::_assert_ca_or_rm — same set that gates Messaging).

Feature-flag gate: every CA endpoint asserts
`client_has_feature(db, cid, "touchpoints")`. The toggle already
exists in the SA Portal Feature Access panel from Phase 1.

Content editing rule: lock-on-first-open per spec §4.2. Once any
recipient has opened a message fired from this touchpoint,
`touchpoints.content_locked_at` is stamped and the composer PATCH
rejects further edits to title/body/language_code/rating_scale_type/
questions/attachments. Metadata (name, description, per_firing_expiry_days)
stays editable. Add/pause/resume/remove of assignments is always
allowed — it's distribution, not content.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.advisory.models import Package, PackageStatus, Timeline
from app.modules.clients.models import Client
from app.modules.feature_access.models import FEATURE_TOUCHPOINTS
from app.modules.feature_access.service import client_has_feature
from app.modules.platform.models import User
from app.modules.touchpoints.models import (
    Touchpoint, TouchpointAssignment, TouchpointAssignmentStatus,
    TouchpointAttachment, TouchpointAttachmentKind, TouchpointContentType,
    TouchpointFireAt, TouchpointQuestion, TouchpointQuestionType,
    TouchpointRecipient, TouchpointResponse, TouchpointResponseAnswer,
    TouchpointStatus,
)


router = APIRouter(tags=["Touchpoints"])


# ── Role + feature gates ────────────────────────────────────────────────

async def _assert_touchpoints_access(
    db: AsyncSession, user: User, client_id: str,
) -> None:
    """CA / CLIENT_RM / SA / CM-EDIT may use Touchpoints.
    403 feature_not_enabled when the add-on isn't active for the
    client — same stable error code as Messaging so the CP can
    render the same empty state."""
    from app.modules.clients.router import _assert_ca_or_rm
    await _assert_ca_or_rm(db, user, client_id)

    if not await client_has_feature(db, client_id, FEATURE_TOUCHPOINTS):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "feature_not_enabled",
                "feature": FEATURE_TOUCHPOINTS,
                "message": (
                    "Subscription Touchpoints is not enabled for this "
                    "company. Contact your Neytiri representative to "
                    "activate it."
                ),
            },
        )


def _assert_valid_content_type(value: str) -> None:
    valid = {t.value for t in TouchpointContentType}
    if value not in valid:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_content_type",
                "message": f"content_type must be one of {sorted(valid)}.",
            },
        )


def _assert_valid_fire_at(value: str) -> None:
    valid = {t.value for t in TouchpointFireAt}
    if value not in valid:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_fire_at",
                "message": f"fire_at must be one of {sorted(valid)} "
                           "(BEGINNING / END).",
            },
        )


def _assert_valid_question_type(value: str) -> None:
    valid = {q.value for q in TouchpointQuestionType}
    if value not in valid:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_question_type",
                "message": f"question_type must be one of {sorted(valid)}.",
            },
        )


async def _get_touchpoint_or_404(
    db: AsyncSession, client_id: str, touchpoint_id: str,
) -> Touchpoint:
    t = (await db.execute(
        select(Touchpoint).where(
            Touchpoint.id == touchpoint_id,
            Touchpoint.client_id == client_id,
        )
    )).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Touchpoint not found")
    return t


def _assert_mutable_status(t: Touchpoint, allowed: set[str]) -> None:
    if t.status not in allowed:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_status_transition",
                "message": f"Touchpoint status is {t.status}; this action "
                           f"requires one of {sorted(allowed)}.",
            },
        )


def _assert_content_unlocked(t: Touchpoint) -> None:
    """Spec §4.2 — lock-on-first-open. Composer PATCH + question
    mutations + attachment uploads reject once any recipient has
    opened any firing of this touchpoint."""
    if t.content_locked_at is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "content_locked",
                "message": (
                    "At least one recipient has opened this message. "
                    "Content is now locked. Duplicate the touchpoint to "
                    "make further changes."
                ),
                "locked_at": t.content_locked_at.isoformat(),
            },
        )


# ── Body schemas ────────────────────────────────────────────────────────

class DraftIn(BaseModel):
    """Create or PATCH. On create, name + content_type are required;
    on PATCH every field is optional."""
    name: Optional[str] = Field(None, max_length=200)
    content_type: Optional[str] = None
    description: Optional[str] = None
    title: Optional[str] = Field(None, max_length=200)
    body: Optional[str] = None
    language_code: Optional[str] = Field(None, max_length=10)
    rating_scale_type: Optional[str] = None
    per_firing_expiry_days: Optional[int] = Field(None, ge=1, le=180)


class AssignmentIn(BaseModel):
    package_id: str
    timeline_id: str
    fire_at: str  # BEGINNING | END


class QuestionIn(BaseModel):
    question_type: str
    label: str = Field(..., max_length=500)
    help_text: Optional[str] = Field(None, max_length=500)
    is_mandatory: bool = False
    choice_options: Optional[list[str]] = None
    number_unit: Optional[str] = Field(None, max_length=50)
    rating_scale_type: Optional[str] = None


class QuestionReorderIn(BaseModel):
    ordering: list[dict]


def _validate_question_shape(q: QuestionIn) -> None:
    """Per-type validation (mirror of messaging). Rejects commas in
    SINGLE/MULTIPLE_CHOICE option labels because the response answer
    storage uses comma-joined strings (see TouchpointResponseAnswer
    .value_choice)."""
    _assert_valid_question_type(q.question_type)
    if q.question_type in (
        TouchpointQuestionType.SINGLE_CHOICE.value,
        TouchpointQuestionType.MULTIPLE_CHOICE.value,
    ):
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
        for opt in q.choice_options:
            if "," in opt:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "choice_option_contains_comma",
                        "message": (
                            f"Option labels can't contain commas (storage "
                            f"format ambiguity). Rephrase '{opt}' — use "
                            f"a dash or semicolon instead."
                        ),
                    },
                )
    if q.question_type == TouchpointQuestionType.NUMBER.value and not q.number_unit:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "number_unit_required",
                "message": "Number questions need a unit (quintals, kg, Rs., …).",
            },
        )
    if q.question_type == TouchpointQuestionType.RATING_SCALE.value:
        rs = (q.rating_scale_type or "STAR_5").upper()
        if rs not in ("STAR_5", "NPS_10"):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_rating_scale",
                    "message": "rating_scale_type must be STAR_5 or NPS_10.",
                },
            )


# ── Shapers ─────────────────────────────────────────────────────────────

def _shape_touchpoint(t: Touchpoint) -> dict:
    return {
        "id": t.id,
        "client_id": t.client_id,
        "name": t.name,
        "content_type": t.content_type,
        "description": t.description,
        "title": t.title,
        "body": t.body,
        "language_code": t.language_code,
        "rating_scale_type": t.rating_scale_type,
        "per_firing_expiry_days": t.per_firing_expiry_days,
        "status": t.status,
        "activated_at": t.activated_at.isoformat() if t.activated_at else None,
        "paused_at": t.paused_at.isoformat() if t.paused_at else None,
        "archived_at": t.archived_at.isoformat() if t.archived_at else None,
        "content_locked_at": t.content_locked_at.isoformat() if t.content_locked_at else None,
        "created_by_user_id": t.created_by_user_id,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _shape_assignment(a: TouchpointAssignment, package_name: str = "", timeline_name: str = "", crop_cosh_id: str = "", crop_name: str = "") -> dict:
    return {
        "id": a.id,
        "touchpoint_id": a.touchpoint_id,
        "package_id": a.package_id,
        "package_name": package_name,
        "timeline_id": a.timeline_id,
        "timeline_name": timeline_name,
        "crop_cosh_id": crop_cosh_id,
        "crop_name": crop_name,
        "fire_at": a.fire_at,
        "status": a.status,
        "activated_at": a.activated_at.isoformat() if a.activated_at else None,
        "paused_at": a.paused_at.isoformat() if a.paused_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _shape_question(q: TouchpointQuestion) -> dict:
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


# ── List + detail ───────────────────────────────────────────────────────

@router.get("/client/{cid}/touchpoints")
async def list_touchpoints(
    cid: str,
    status: Optional[str] = Query(
        None, description="DRAFT | ACTIVE | PAUSED | ARCHIVED. Omit to "
                          "see all except ARCHIVED.",
    ),
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_touchpoints_access(db, current_user, cid)

    stmt = select(Touchpoint).where(Touchpoint.client_id == cid)
    if status:
        valid = {s.value for s in TouchpointStatus}
        if status not in valid:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_status", "message": f"status must be one of {sorted(valid)}."},
            )
        stmt = stmt.where(Touchpoint.status == status)
    elif not include_archived:
        stmt = stmt.where(Touchpoint.status != TouchpointStatus.ARCHIVED.value)
    stmt = stmt.order_by(Touchpoint.updated_at.desc())
    rows = (await db.execute(stmt)).scalars().all()

    # Counters per touchpoint — shape with assignment count + total
    # fires for the list view. Cheap aggregate.
    if rows:
        ids = [t.id for t in rows]
        asg_counts = dict((await db.execute(
            select(TouchpointAssignment.touchpoint_id, func.count(TouchpointAssignment.id))
            .where(TouchpointAssignment.touchpoint_id.in_(ids))
            .group_by(TouchpointAssignment.touchpoint_id)
        )).all())
        fire_counts = dict((await db.execute(
            select(TouchpointRecipient.touchpoint_id, func.count(TouchpointRecipient.id))
            .where(TouchpointRecipient.touchpoint_id.in_(ids))
            .group_by(TouchpointRecipient.touchpoint_id)
        )).all())
    else:
        asg_counts, fire_counts = {}, {}

    out = []
    for t in rows:
        shaped = _shape_touchpoint(t)
        shaped["assignment_count"] = int(asg_counts.get(t.id, 0))
        shaped["total_fires"] = int(fire_counts.get(t.id, 0))
        out.append(shaped)
    return {"touchpoints": out}


# ── Composer picker: Crop → Package → Timeline cascade ─────────────────
#
# These static-path routes MUST be registered BEFORE
# `GET /client/{cid}/touchpoints/{tid}` — otherwise FastAPI's first-
# match ordering routes GET .../touchpoints/crops into the {tid}
# handler as tid="crops" and 404s with "Touchpoint not found" (bug
# caught on staging 2026-11-24 while the Triggers step was loading).
# Feature-flag probe (/access) sits here for the same reason.

@router.get("/client/{cid}/touchpoints/crops")
async def list_crops_with_active_packages(
    cid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crops that have ≥1 ACTIVE package under this client.
    First step of the composer's PoP-assignment cascade."""
    await _assert_touchpoints_access(db, current_user, cid)
    crop_ids = (await db.execute(
        select(Package.crop_cosh_id).where(
            Package.client_id == cid,
            Package.status == PackageStatus.ACTIVE.value,
            Package.crop_cosh_id.isnot(None),
        ).distinct()
    )).scalars().all()
    if not crop_ids:
        return {"crops": []}
    from app.modules.sync.models import CoshCoreItem
    cores = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
        .where(CoshCoreItem.cosh_id.in_(crop_ids))
    )).all()
    name_by_id = {cid_: ((trans or {}).get("en") or cid_) for cid_, trans in cores}
    crops = [
        {"cosh_id": c, "name": name_by_id.get(c, c)} for c in crop_ids
    ]
    crops.sort(key=lambda o: o["name"].casefold())
    return {"crops": crops}


@router.get("/client/{cid}/touchpoints/packages")
async def list_packages_for_crop(
    cid: str,
    crop_cosh_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """ACTIVE packages for a given crop under this client.
    Second step of the composer cascade."""
    await _assert_touchpoints_access(db, current_user, cid)
    rows = (await db.execute(
        select(Package.id, Package.name).where(
            Package.client_id == cid,
            Package.status == PackageStatus.ACTIVE.value,
            Package.crop_cosh_id == crop_cosh_id,
        ).order_by(Package.name)
    )).all()
    return {
        "packages": [{"id": pid, "name": name or ""} for pid, name in rows],
    }


@router.get("/client/{cid}/touchpoints/timelines")
async def list_timelines_for_package(
    cid: str,
    package_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Timelines in a package (ordered by their sort position in
    the PoP). Third step of the composer cascade."""
    await _assert_touchpoints_access(db, current_user, cid)
    pkg = (await db.execute(
        select(Package.id).where(
            Package.id == package_id,
            Package.client_id == cid,
        )
    )).scalar_one_or_none()
    if pkg is None:
        raise HTTPException(status_code=404, detail="Package not found")
    # ACTIVE timelines only — INACTIVE timelines are excluded from
    # farmer advisory (Batch 28 per Timeline.status docstring), so
    # hosting a touchpoint on an INACTIVE timeline would be a
    # firing-against-nothing situation. Order by display_order.
    rows = (await db.execute(
        select(Timeline.id, Timeline.name)
        .where(
            Timeline.package_id == package_id,
            Timeline.status == "ACTIVE",
        )
        .order_by(Timeline.display_order.asc(), Timeline.name.asc())
    )).all()
    return {
        "timelines": [{"id": tid, "name": name or ""} for tid, name in rows],
    }


@router.get("/client/{cid}/touchpoints/access")
async def touchpoints_access(
    cid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns {enabled, expires_at, days_remaining} so the CP can
    decide whether to render the nav + Expiring Soon banner. Does
    NOT 403 when feature is off — reports state."""
    from app.modules.clients.router import _assert_ca_or_rm
    await _assert_ca_or_rm(db, current_user, cid)
    from app.modules.feature_access.service import latest_feature_row, row_is_active
    latest = await latest_feature_row(db, cid, FEATURE_TOUCHPOINTS)
    if latest is None or not row_is_active(latest):
        return {"enabled": False, "expires_at": None, "days_remaining": None}
    now = datetime.now(timezone.utc)
    remaining = int((latest.access_until - now).total_seconds() // 86400)
    return {
        "enabled": True,
        "expires_at": latest.access_until.isoformat(),
        "days_remaining": remaining,
    }


@router.get("/client/{cid}/touchpoints/{tid}")
async def get_touchpoint(
    cid: str, tid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Detail view: touchpoint + assignments (hydrated with package +
    timeline + crop names) + questions + attachments."""
    await _assert_touchpoints_access(db, current_user, cid)
    t = await _get_touchpoint_or_404(db, cid, tid)

    # Assignments — join to packages + timelines to hydrate names.
    asg_rows = (await db.execute(
        select(TouchpointAssignment, Package, Timeline)
        .join(Package, Package.id == TouchpointAssignment.package_id)
        .join(Timeline, Timeline.id == TouchpointAssignment.timeline_id)
        .where(TouchpointAssignment.touchpoint_id == tid)
        .order_by(TouchpointAssignment.created_at.asc())
    )).all()
    # Resolve crop names via Cosh.
    from app.modules.sync.models import CoshCoreItem
    crop_ids = {p.crop_cosh_id for _, p, _ in asg_rows if p.crop_cosh_id}
    crop_name_by_id: dict[str, str] = {}
    if crop_ids:
        cores = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(crop_ids))
        )).all()
        for cid_, trans in cores:
            crop_name_by_id[cid_] = (trans or {}).get("en") or cid_
    assignments = [
        _shape_assignment(
            a,
            package_name=p.name or "",
            timeline_name=tl.name or "",
            crop_cosh_id=p.crop_cosh_id or "",
            crop_name=crop_name_by_id.get(p.crop_cosh_id or "", ""),
        )
        for a, p, tl in asg_rows
    ]

    q_rows = (await db.execute(
        select(TouchpointQuestion)
        .where(TouchpointQuestion.touchpoint_id == tid)
        .order_by(TouchpointQuestion.sort_order)
    )).scalars().all()
    att_rows = (await db.execute(
        select(TouchpointAttachment)
        .where(TouchpointAttachment.touchpoint_id == tid)
        .order_by(TouchpointAttachment.sort_order)
    )).scalars().all()

    return {
        "touchpoint": _shape_touchpoint(t),
        "assignments": assignments,
        "questions": [_shape_question(q) for q in q_rows],
        "attachments": [
            {
                "id": a.id, "kind": a.kind, "url": a.storage_path,
                "original_filename": a.original_filename,
                "mime_type": a.mime_type, "size_bytes": a.size_bytes,
                "sort_order": a.sort_order,
            }
            for a in att_rows
        ],
    }


# ── Create draft ────────────────────────────────────────────────────────

@router.post("/client/{cid}/touchpoints")
async def create_draft(
    cid: str, body: DraftIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a DRAFT touchpoint. Only `name` + `content_type` are
    required at create; title/body stay optional until SEND-time
    (here: ACTIVATE-time), matching the Messaging convention set
    after the 2026-11-22 field-test fix."""
    await _assert_touchpoints_access(db, current_user, cid)
    client = (await db.execute(
        select(Client).where(Client.id == cid)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    missing = [k for k in ("name", "content_type")
               if getattr(body, k) is None or (isinstance(getattr(body, k), str) and not getattr(body, k).strip())]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "missing_fields",
                "message": f"Required to create a draft: {missing}",
            },
        )
    _assert_valid_content_type(body.content_type)

    now = datetime.now(timezone.utc)
    t = Touchpoint(
        client_id=cid,
        name=body.name.strip(),
        content_type=body.content_type,
        description=body.description,
        title=(body.title or "").strip(),
        body=body.body or "",
        language_code=body.language_code,
        rating_scale_type=body.rating_scale_type,
        per_firing_expiry_days=body.per_firing_expiry_days or 7,
        status=TouchpointStatus.DRAFT.value,
        created_by_user_id=current_user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"touchpoint": _shape_touchpoint(t)}


# ── Update (PATCH) ──────────────────────────────────────────────────────

# Content fields — editing these requires content_unlocked.
_CONTENT_FIELDS = {"title", "body", "language_code", "rating_scale_type"}
# Metadata fields — always editable.
_METADATA_FIELDS = {"name", "description", "per_firing_expiry_days"}


@router.patch("/client/{cid}/touchpoints/{tid}")
async def update_draft(
    cid: str, tid: str, body: DraftIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """PATCH semantics. Metadata (name, description,
    per_firing_expiry_days) is always editable regardless of status.
    Content (title, body, language_code, rating_scale_type) is
    editable while content_locked_at is NULL — once any recipient
    opens any firing, content locks per spec §4.2.
    Content_type is immutable after draft (changing between types
    would invalidate questions / rating_scale / etc).
    """
    await _assert_touchpoints_access(db, current_user, cid)
    t = await _get_touchpoint_or_404(db, cid, tid)
    _assert_mutable_status(t, {
        TouchpointStatus.DRAFT.value,
        TouchpointStatus.ACTIVE.value,
        TouchpointStatus.PAUSED.value,
    })

    content_edits = [f for f in _CONTENT_FIELDS if getattr(body, f, None) is not None]
    if content_edits:
        _assert_content_unlocked(t)

    if body.content_type is not None and body.content_type != t.content_type:
        if t.status != TouchpointStatus.DRAFT.value:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "content_type_immutable_after_draft",
                    "message": "content_type can only be changed while the touchpoint is a DRAFT.",
                },
            )
        _assert_valid_content_type(body.content_type)
        t.content_type = body.content_type

    if body.name is not None: t.name = body.name.strip()
    if body.description is not None: t.description = body.description
    if body.title is not None: t.title = body.title.strip()
    if body.body is not None: t.body = body.body
    if body.language_code is not None: t.language_code = body.language_code
    if body.rating_scale_type is not None: t.rating_scale_type = body.rating_scale_type
    if body.per_firing_expiry_days is not None:
        t.per_firing_expiry_days = body.per_firing_expiry_days
    t.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(t)
    return {"touchpoint": _shape_touchpoint(t)}


# ── Delete (DRAFT hard-delete only) ─────────────────────────────────────

@router.delete("/client/{cid}/touchpoints/{tid}")
async def delete_draft(
    cid: str, tid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hard-delete a DRAFT. ACTIVE/PAUSED → use archive instead."""
    await _assert_touchpoints_access(db, current_user, cid)
    t = await _get_touchpoint_or_404(db, cid, tid)
    if t.status != TouchpointStatus.DRAFT.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "not_a_draft",
                "message": "Only DRAFT touchpoints can be hard-deleted. Use archive otherwise.",
            },
        )
    await db.delete(t)
    await db.commit()
    return {"deleted": True}


# ── Lifecycle: activate / pause / resume / archive ─────────────────────

@router.post("/client/{cid}/touchpoints/{tid}/activate")
async def activate_touchpoint(
    cid: str, tid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """DRAFT or PAUSED → ACTIVE. Requires:
      - title + body non-empty (content complete)
      - ≥1 assignment exists AND at least one is ACTIVE
      - for SURVEY: ≥1 question exists; FEEDBACK can have 0 follow-ups.
    """
    await _assert_touchpoints_access(db, current_user, cid)
    t = await _get_touchpoint_or_404(db, cid, tid)
    _assert_mutable_status(t, {
        TouchpointStatus.DRAFT.value,
        TouchpointStatus.PAUSED.value,
    })

    # Content completeness
    missing = []
    if not t.title or not t.title.strip(): missing.append("title")
    if not t.body or not t.body.strip(): missing.append("body")
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "touchpoint_content_incomplete",
                "message": f"Fill in the content first: {missing}. Go back to the Content step.",
                "missing": missing,
            },
        )

    # SURVEY needs ≥1 question
    if t.content_type == TouchpointContentType.SURVEY.value:
        q_count = (await db.execute(
            select(func.count(TouchpointQuestion.id))
            .where(TouchpointQuestion.touchpoint_id == tid)
        )).scalar_one()
        if q_count == 0:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "survey_needs_questions",
                    "message": "A SURVEY touchpoint needs at least 1 question.",
                },
            )

    # ≥1 assignment exists AND ≥1 is ACTIVE
    active_asg = (await db.execute(
        select(func.count(TouchpointAssignment.id))
        .where(
            TouchpointAssignment.touchpoint_id == tid,
            TouchpointAssignment.status == TouchpointAssignmentStatus.ACTIVE.value,
        )
    )).scalar_one()
    if active_asg == 0:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_active_assignments",
                "message": (
                    "Add at least one PoP assignment (Crop → Package → "
                    "Timeline → Beginning/End) with ACTIVE status before "
                    "activating the touchpoint."
                ),
            },
        )

    now = datetime.now(timezone.utc)
    t.status = TouchpointStatus.ACTIVE.value
    if t.activated_at is None:
        t.activated_at = now
    t.paused_at = None
    t.updated_at = now
    await db.commit()
    await db.refresh(t)
    return {"touchpoint": _shape_touchpoint(t)}


@router.post("/client/{cid}/touchpoints/{tid}/pause")
async def pause_touchpoint(
    cid: str, tid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """ACTIVE → PAUSED (globally). Stops firing on ALL assignments
    until resumed. Doesn't affect already-fired recipients —
    responses still flow."""
    await _assert_touchpoints_access(db, current_user, cid)
    t = await _get_touchpoint_or_404(db, cid, tid)
    _assert_mutable_status(t, {TouchpointStatus.ACTIVE.value})
    now = datetime.now(timezone.utc)
    t.status = TouchpointStatus.PAUSED.value
    t.paused_at = now
    t.updated_at = now
    await db.commit()
    await db.refresh(t)
    return {"touchpoint": _shape_touchpoint(t)}


@router.post("/client/{cid}/touchpoints/{tid}/archive")
async def archive_touchpoint(
    cid: str, tid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """DRAFT / PAUSED → ARCHIVED. ACTIVE touchpoints must be paused
    first (same safety as Messaging — can't archive while firing)."""
    await _assert_touchpoints_access(db, current_user, cid)
    t = await _get_touchpoint_or_404(db, cid, tid)
    if t.status == TouchpointStatus.ACTIVE.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "pause_before_archive",
                "message": "Pause the touchpoint first — active touchpoints are still firing.",
            },
        )
    if t.status == TouchpointStatus.ARCHIVED.value:
        raise HTTPException(
            status_code=422,
            detail={"code": "already_archived"},
        )
    now = datetime.now(timezone.utc)
    t.status = TouchpointStatus.ARCHIVED.value
    t.archived_at = now
    t.updated_at = now
    await db.commit()
    await db.refresh(t)
    return {"touchpoint": _shape_touchpoint(t)}


# ── Assignments ─────────────────────────────────────────────────────────

@router.post("/client/{cid}/touchpoints/{tid}/assignments")
async def add_assignment(
    cid: str, tid: str, body: AssignmentIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a (package × timeline × fire_at) assignment to a touchpoint.
    Validates:
      - package belongs to this client AND is ACTIVE
      - timeline belongs to this package
      - fire_at is BEGINNING / END
      - no duplicate assignment with the same (package, timeline, fire_at)
    Assignments can be added regardless of touchpoint status
    (distribution ≠ content; always editable).
    """
    await _assert_touchpoints_access(db, current_user, cid)
    t = await _get_touchpoint_or_404(db, cid, tid)
    _assert_valid_fire_at(body.fire_at)

    # Package must belong to this client + be ACTIVE.
    pkg = (await db.execute(
        select(Package).where(
            Package.id == body.package_id,
            Package.client_id == cid,
        )
    )).scalar_one_or_none()
    if pkg is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "package_not_found", "message": "Package not found on this client."},
        )
    if pkg.status != PackageStatus.ACTIVE.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "package_not_active",
                "message": "Only ACTIVE packages can host touchpoint assignments.",
            },
        )

    # Timeline must belong to the package AND be ACTIVE (INACTIVE
    # timelines are excluded from farmer advisory so would never fire).
    tl = (await db.execute(
        select(Timeline).where(
            Timeline.id == body.timeline_id,
            Timeline.package_id == body.package_id,
        )
    )).scalar_one_or_none()
    if tl is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "timeline_not_found", "message": "Timeline not found in this package."},
        )
    if tl.status != "ACTIVE":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "timeline_not_active",
                "message": "Only ACTIVE timelines can host touchpoint assignments.",
            },
        )

    # Dupe check (UNIQUE constraint would catch it, but a clean 422
    # beats a 500 wrapped integrity error).
    dupe = (await db.execute(
        select(TouchpointAssignment.id).where(
            TouchpointAssignment.touchpoint_id == tid,
            TouchpointAssignment.package_id == body.package_id,
            TouchpointAssignment.timeline_id == body.timeline_id,
            TouchpointAssignment.fire_at == body.fire_at,
        )
    )).scalar_one_or_none()
    if dupe is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "assignment_already_exists",
                "message": "This touchpoint is already assigned to that package + timeline + end.",
            },
        )

    a = TouchpointAssignment(
        touchpoint_id=tid,
        package_id=body.package_id,
        timeline_id=body.timeline_id,
        fire_at=body.fire_at,
        status=TouchpointAssignmentStatus.ACTIVE.value,
        created_by_user_id=current_user.id,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)

    # Resolve crop name for the shape.
    from app.modules.sync.models import CoshCoreItem
    crop_name = ""
    if pkg.crop_cosh_id:
        core = (await db.execute(
            select(CoshCoreItem.translations).where(CoshCoreItem.cosh_id == pkg.crop_cosh_id)
        )).scalar_one_or_none()
        if core:
            crop_name = (core or {}).get("en") or ""

    return _shape_assignment(
        a,
        package_name=pkg.name or "",
        timeline_name=tl.name or "",
        crop_cosh_id=pkg.crop_cosh_id or "",
        crop_name=crop_name,
    )


@router.delete("/client/{cid}/touchpoints/{tid}/assignments/{aid}")
async def remove_assignment(
    cid: str, tid: str, aid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove an assignment. Past firings stay; the assignment stops
    being considered by future beat sweeps."""
    await _assert_touchpoints_access(db, current_user, cid)
    _ = await _get_touchpoint_or_404(db, cid, tid)
    a = (await db.execute(
        select(TouchpointAssignment).where(
            TouchpointAssignment.id == aid,
            TouchpointAssignment.touchpoint_id == tid,
        )
    )).scalar_one_or_none()
    if a is None:
        raise HTTPException(status_code=404, detail="Assignment not found")
    await db.delete(a)
    await db.commit()
    return {"deleted": True}


@router.post("/client/{cid}/touchpoints/{tid}/assignments/{aid}/pause")
async def pause_assignment(
    cid: str, tid: str, aid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pause one specific assignment. Other assignments on the same
    touchpoint keep firing."""
    await _assert_touchpoints_access(db, current_user, cid)
    _ = await _get_touchpoint_or_404(db, cid, tid)
    a = (await db.execute(
        select(TouchpointAssignment).where(
            TouchpointAssignment.id == aid,
            TouchpointAssignment.touchpoint_id == tid,
        )
    )).scalar_one_or_none()
    if a is None:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if a.status != TouchpointAssignmentStatus.ACTIVE.value:
        raise HTTPException(
            status_code=422,
            detail={"code": "not_active", "message": "Assignment is not active."},
        )
    a.status = TouchpointAssignmentStatus.PAUSED.value
    a.paused_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(a)
    return _shape_assignment(a)


@router.post("/client/{cid}/touchpoints/{tid}/assignments/{aid}/resume")
async def resume_assignment(
    cid: str, tid: str, aid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Resume a paused assignment. Future beat sweeps pick it up
    again. Resets activated_at to NOW so non-retroactive gating
    applies freshly from the resume moment."""
    await _assert_touchpoints_access(db, current_user, cid)
    _ = await _get_touchpoint_or_404(db, cid, tid)
    a = (await db.execute(
        select(TouchpointAssignment).where(
            TouchpointAssignment.id == aid,
            TouchpointAssignment.touchpoint_id == tid,
        )
    )).scalar_one_or_none()
    if a is None:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if a.status != TouchpointAssignmentStatus.PAUSED.value:
        raise HTTPException(
            status_code=422,
            detail={"code": "not_paused", "message": "Assignment is not paused."},
        )
    now = datetime.now(timezone.utc)
    a.status = TouchpointAssignmentStatus.ACTIVE.value
    a.activated_at = now  # reset — non-retroactive from resume
    a.paused_at = None
    await db.commit()
    await db.refresh(a)
    return _shape_assignment(a)


# ── Questions ───────────────────────────────────────────────────────────

async def _get_mutable_touchpoint_for_questions(
    db: AsyncSession, cid: str, tid: str,
) -> Touchpoint:
    """Shared gate for question CRUD: touchpoint exists on this
    client, content isn't locked yet, and the content type accepts
    questions (SURVEY / FEEDBACK-follow-ups)."""
    t = await _get_touchpoint_or_404(db, cid, tid)
    _assert_content_unlocked(t)
    if t.content_type == TouchpointContentType.ANNOUNCEMENT.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "content_type_has_no_questions",
                "message": "Announcements don't collect responses.",
            },
        )
    if t.content_type == TouchpointContentType.INTEREST_CAPTURE.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "content_type_has_no_questions",
                "message": "Interest Capture uses a built-in Yes/No/Maybe — no extra questions.",
            },
        )
    return t


@router.post("/client/{cid}/touchpoints/{tid}/questions")
async def add_question(
    cid: str, tid: str, body: QuestionIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Append question. Caps: 20 for SURVEY, 3 for FEEDBACK follow-ups."""
    await _assert_touchpoints_access(db, current_user, cid)
    t = await _get_mutable_touchpoint_for_questions(db, cid, tid)
    _validate_question_shape(body)

    cap = 3 if t.content_type == TouchpointContentType.FEEDBACK.value else 20
    count = len((await db.execute(
        select(TouchpointQuestion.id).where(TouchpointQuestion.touchpoint_id == tid)
    )).scalars().all())
    if count >= cap:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "too_many_questions",
                "message": f"A {t.content_type.lower()} can have at most {cap} question{'s' if cap > 1 else ''}.",
            },
        )

    q = TouchpointQuestion(
        touchpoint_id=tid,
        sort_order=count,
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


@router.patch("/client/{cid}/touchpoints/{tid}/questions/{qid}")
async def update_question(
    cid: str, tid: str, qid: str, body: QuestionIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_touchpoints_access(db, current_user, cid)
    _ = await _get_mutable_touchpoint_for_questions(db, cid, tid)
    _validate_question_shape(body)
    q = (await db.execute(
        select(TouchpointQuestion).where(
            TouchpointQuestion.id == qid,
            TouchpointQuestion.touchpoint_id == tid,
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


@router.delete("/client/{cid}/touchpoints/{tid}/questions/{qid}")
async def delete_question(
    cid: str, tid: str, qid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_touchpoints_access(db, current_user, cid)
    _ = await _get_mutable_touchpoint_for_questions(db, cid, tid)
    q = (await db.execute(
        select(TouchpointQuestion).where(
            TouchpointQuestion.id == qid,
            TouchpointQuestion.touchpoint_id == tid,
        )
    )).scalar_one_or_none()
    if q is None:
        raise HTTPException(status_code=404, detail="Question not found")
    await db.delete(q)
    await db.commit()
    return {"deleted": True}


@router.put("/client/{cid}/touchpoints/{tid}/questions/reorder")
async def reorder_questions(
    cid: str, tid: str, body: QuestionReorderIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_touchpoints_access(db, current_user, cid)
    _ = await _get_mutable_touchpoint_for_questions(db, cid, tid)
    by_id = {r["id"]: int(r["sort_order"]) for r in body.ordering if "id" in r and "sort_order" in r}
    if not by_id:
        return {"updated": 0}
    rows = (await db.execute(
        select(TouchpointQuestion).where(
            TouchpointQuestion.touchpoint_id == tid,
            TouchpointQuestion.id.in_(list(by_id.keys())),
        )
    )).scalars().all()
    for q in rows:
        q.sort_order = by_id[q.id]
    await db.commit()
    return {"updated": len(rows)}


# ── Attachments ─────────────────────────────────────────────────────────

MAX_ATTACHMENTS = 3
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime"}
PDF_TYPES = {"application/pdf"}
ALLOWED_ATTACHMENT_TYPES = IMAGE_TYPES | VIDEO_TYPES | PDF_TYPES


def _kind_for_mime(mime: str) -> str:
    base = (mime or "").split(";", 1)[0].strip()
    if base in IMAGE_TYPES: return TouchpointAttachmentKind.IMAGE.value
    if base in VIDEO_TYPES: return TouchpointAttachmentKind.VIDEO.value
    if base in PDF_TYPES: return TouchpointAttachmentKind.PDF.value
    return TouchpointAttachmentKind.IMAGE.value  # unreachable — validator rejects


@router.get("/client/{cid}/touchpoints/{tid}/attachments")
async def list_attachments(
    cid: str, tid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_touchpoints_access(db, current_user, cid)
    _ = await _get_touchpoint_or_404(db, cid, tid)
    rows = (await db.execute(
        select(TouchpointAttachment)
        .where(TouchpointAttachment.touchpoint_id == tid)
        .order_by(TouchpointAttachment.sort_order)
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
        ]
    }


@router.post("/client/{cid}/touchpoints/{tid}/attachments")
async def add_attachment(
    cid: str, tid: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload + attach. Cap 3 × 10 MB. S3 public URL (same pattern
    as Messaging attachments; storage_path IS the URL)."""
    from app.modules.media.router import upload_to_s3

    await _assert_touchpoints_access(db, current_user, cid)
    t = await _get_touchpoint_or_404(db, cid, tid)
    _assert_content_unlocked(t)

    count = len((await db.execute(
        select(TouchpointAttachment.id).where(TouchpointAttachment.touchpoint_id == tid)
    )).scalars().all())
    if count >= MAX_ATTACHMENTS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "too_many_attachments",
                "message": f"A touchpoint can have at most {MAX_ATTACHMENTS} attachments.",
            },
        )

    uploaded = await upload_to_s3(
        file, folder="touchpoints",
        allowed_types=ALLOWED_ATTACHMENT_TYPES,
        max_size_bytes=MAX_ATTACHMENT_BYTES,
    )
    base_ct = (file.content_type or "").split(";", 1)[0].strip()
    row = TouchpointAttachment(
        touchpoint_id=tid,
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
        "id": row.id, "kind": row.kind, "url": row.storage_path,
        "original_filename": row.original_filename,
        "mime_type": row.mime_type, "size_bytes": row.size_bytes,
        "sort_order": row.sort_order,
    }


@router.delete("/client/{cid}/touchpoints/{tid}/attachments/{aid}")
async def remove_attachment(
    cid: str, tid: str, aid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_touchpoints_access(db, current_user, cid)
    t = await _get_touchpoint_or_404(db, cid, tid)
    _assert_content_unlocked(t)
    row = (await db.execute(
        select(TouchpointAttachment).where(
            TouchpointAttachment.id == aid,
            TouchpointAttachment.touchpoint_id == tid,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": True}


# ── Analytics ───────────────────────────────────────────────────────────

def _mask_phone(phone: Optional[str]) -> str:
    """Last-4-only masking — same convention as Messaging + Queries."""
    if not phone:
        return ""
    if len(phone) <= 4:
        return phone
    return "•" * (len(phone) - 4) + phone[-4:]


def _display_answer(answer: TouchpointResponseAnswer) -> str:
    """Flatten the polymorphic answer columns into one human-readable
    string. Mirror of messaging._display_answer."""
    if answer.value_text is not None:
        return str(answer.value_text)
    if answer.value_number is not None:
        n = answer.value_number
        if n == int(n):
            return str(int(n))
        return f"{n:.2f}".rstrip("0").rstrip(".")
    if answer.value_choice is not None:
        return str(answer.value_choice)
    if answer.value_boolean is not None:
        return "Yes" if answer.value_boolean else "No"
    if answer.value_date is not None:
        return answer.value_date.isoformat()
    if answer.value_media_path is not None:
        return f"[photo: {answer.value_media_path}]"
    return ""


@router.get("/client/{cid}/touchpoints/{tid}/analytics")
async def touchpoint_analytics(
    cid: str, tid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delivery summary + per-question aggregates. No `is_test`
    filtering — touchpoints have no test-send path; every recipient
    row is a real firing."""
    await _assert_touchpoints_access(db, current_user, cid)
    t = await _get_touchpoint_or_404(db, cid, tid)

    total = (await db.execute(
        select(func.count(TouchpointRecipient.id)).where(
            TouchpointRecipient.touchpoint_id == tid,
        )
    )).scalar_one()
    pushed = (await db.execute(
        select(func.count(TouchpointRecipient.id)).where(
            TouchpointRecipient.touchpoint_id == tid,
            TouchpointRecipient.pushed_at.isnot(None),
        )
    )).scalar_one()
    opened = (await db.execute(
        select(func.count(TouchpointRecipient.id)).where(
            TouchpointRecipient.touchpoint_id == tid,
            TouchpointRecipient.opened_at.isnot(None),
        )
    )).scalar_one()
    responded = (await db.execute(
        select(func.count(TouchpointRecipient.id)).where(
            TouchpointRecipient.touchpoint_id == tid,
            TouchpointRecipient.responded_at.isnot(None),
        )
    )).scalar_one()

    interest = None
    if t.content_type == TouchpointContentType.INTEREST_CAPTURE.value:
        interest_rows = (await db.execute(
            select(TouchpointResponse.interest_answer, func.count(TouchpointResponse.id))
            .where(TouchpointResponse.touchpoint_id == tid)
            .group_by(TouchpointResponse.interest_answer)
        )).all()
        interest = {ans or "UNKNOWN": int(n) for ans, n in interest_rows}

    feedback = None
    if t.content_type == TouchpointContentType.FEEDBACK.value:
        rating_rows = (await db.execute(
            select(TouchpointResponse.rating, func.count(TouchpointResponse.id))
            .where(TouchpointResponse.touchpoint_id == tid)
            .group_by(TouchpointResponse.rating)
        )).all()
        feedback = {str(r or "0"): int(n) for r, n in rating_rows}

    questions = (await db.execute(
        select(TouchpointQuestion)
        .where(TouchpointQuestion.touchpoint_id == tid)
        .order_by(TouchpointQuestion.sort_order)
    )).scalars().all()
    question_aggs: list[dict[str, Any]] = []
    for q in questions:
        agg: dict[str, Any] = {
            "question_id": q.id,
            "label": q.label,
            "question_type": q.question_type,
            "response_count": 0,
        }
        all_rows = (await db.execute(
            select(TouchpointResponseAnswer).where(
                TouchpointResponseAnswer.question_id == q.id,
            )
        )).scalars().all()
        agg["response_count"] = len(all_rows)
        if q.question_type in (
            TouchpointQuestionType.SINGLE_CHOICE.value,
            TouchpointQuestionType.MULTIPLE_CHOICE.value,
        ):
            tally: dict[str, int] = {}
            for r in all_rows:
                raw = r.value_choice or ""
                if q.question_type == TouchpointQuestionType.MULTIPLE_CHOICE.value:
                    for opt in [o.strip() for o in raw.split(",") if o.strip()]:
                        tally[opt] = tally.get(opt, 0) + 1
                else:
                    tally[raw] = tally.get(raw, 0) + 1
            agg["choice_tally"] = tally
        elif q.question_type == TouchpointQuestionType.YES_NO.value:
            yes = sum(1 for r in all_rows if r.value_boolean is True)
            no = sum(1 for r in all_rows if r.value_boolean is False)
            agg["yes_no"] = {"YES": yes, "NO": no}
        elif q.question_type in (
            TouchpointQuestionType.NUMBER.value,
            TouchpointQuestionType.RATING_SCALE.value,
        ):
            nums = [r.value_number for r in all_rows if r.value_number is not None]
            if nums:
                agg["stats"] = {
                    "min": min(nums), "max": max(nums),
                    "mean": sum(nums) / len(nums), "count": len(nums),
                }
        question_aggs.append(agg)

    return {
        "touchpoint": _shape_touchpoint(t),
        "delivery": {
            "recipient_count": int(total),
            "pushed": int(pushed),
            "opened": int(opened),
            "responded": int(responded),
        },
        "interest_capture": interest,
        "feedback_rating_histogram": feedback,
        "questions": question_aggs,
    }


# ── Individual response drill-down ──────────────────────────────────────

@router.get("/client/{cid}/touchpoints/{tid}/responses")
async def list_responses(
    cid: str, tid: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Paginated per-recipient response list for the drill-down table
    on the touchpoint-detail page."""
    await _assert_touchpoints_access(db, current_user, cid)
    t = await _get_touchpoint_or_404(db, cid, tid)

    total = (await db.execute(
        select(func.count(TouchpointResponse.id))
        .join(TouchpointRecipient, TouchpointRecipient.id == TouchpointResponse.recipient_id)
        .where(TouchpointResponse.touchpoint_id == tid)
    )).scalar_one()

    offset = (page - 1) * page_size
    rows = (await db.execute(
        select(TouchpointResponse, TouchpointRecipient)
        .join(TouchpointRecipient, TouchpointRecipient.id == TouchpointResponse.recipient_id)
        .where(TouchpointResponse.touchpoint_id == tid)
        .order_by(TouchpointResponse.submitted_at.desc())
        .offset(offset).limit(page_size)
    )).all()
    if not rows:
        return {
            "rows": [], "total": total, "page": page, "page_size": page_size,
            "questions": [],
        }

    response_ids = [r.id for r, _ in rows]
    ans_rows = (await db.execute(
        select(TouchpointResponseAnswer).where(
            TouchpointResponseAnswer.response_id.in_(response_ids),
        )
    )).scalars().all()
    answers_by_response: dict[str, list[TouchpointResponseAnswer]] = {}
    for a in ans_rows:
        answers_by_response.setdefault(a.response_id, []).append(a)

    q_rows = (await db.execute(
        select(TouchpointQuestion)
        .where(TouchpointQuestion.touchpoint_id == tid)
        .order_by(TouchpointQuestion.sort_order)
    )).scalars().all()
    q_by_id = {q.id: q for q in q_rows}

    # Hydrate state/district cosh → English for display.
    from app.modules.sync.models import CoshCoreItem
    cosh_ids: set[str] = set()
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
            # Subscription context is part of the Touchpoint audit —
            # surface it in the drill-down so a CA can see WHICH
            # milestone fired for each respondent.
            "timeline_name": recipient.timeline_name or "",
            "fire_at": recipient.fire_at,
            "submitted_at": resp.submitted_at.isoformat() if resp.submitted_at else None,
            "interest_answer": resp.interest_answer,
            "rating": resp.rating,
            "answers": [
                {
                    "question_id": a.question_id,
                    "question_label": q_by_id.get(a.question_id).label if q_by_id.get(a.question_id) else "(removed)",
                    "question_type": q_by_id.get(a.question_id).question_type if q_by_id.get(a.question_id) else None,
                    "display_value": _display_answer(a),
                }
                for a in ans_list
            ],
        })
    return {
        "rows": shaped, "total": int(total), "page": page, "page_size": page_size,
        "questions": [
            {"id": q.id, "label": q.label, "question_type": q.question_type, "sort_order": q.sort_order}
            for q in q_rows
        ],
    }


# ── CSV export ──────────────────────────────────────────────────────────

@router.get("/client/{cid}/touchpoints/{tid}/export.csv")
async def export_responses_csv(
    cid: str, tid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CSV export of all responses for this touchpoint. CA / CM-EDIT /
    SA only. Writes a row to `report_csv_downloads` with
    `report_name="touchpoint_responses"` so the SA Portal CSV audit
    viewer surfaces this download alongside Messaging + Queries."""
    from app.modules.clients.router import _assert_ca_admin_or_cm_for_client
    import csv, io
    from fastapi.responses import StreamingResponse

    await _assert_ca_admin_or_cm_for_client(db, current_user.id, cid)
    if not await client_has_feature(db, cid, FEATURE_TOUCHPOINTS):
        raise HTTPException(
            status_code=403,
            detail={"code": "feature_not_enabled", "message": "Touchpoints not enabled."},
        )

    t = await _get_touchpoint_or_404(db, cid, tid)

    rows = (await db.execute(
        select(TouchpointResponse, TouchpointRecipient)
        .join(TouchpointRecipient, TouchpointRecipient.id == TouchpointResponse.recipient_id)
        .where(TouchpointResponse.touchpoint_id == tid)
        .order_by(TouchpointResponse.submitted_at.desc())
    )).all()

    q_rows = (await db.execute(
        select(TouchpointQuestion)
        .where(TouchpointQuestion.touchpoint_id == tid)
        .order_by(TouchpointQuestion.sort_order)
    )).scalars().all()

    ans_by_response: dict[str, dict[str, TouchpointResponseAnswer]] = {}
    if rows:
        response_ids = [r.id for r, _ in rows]
        ans_rows = (await db.execute(
            select(TouchpointResponseAnswer).where(
                TouchpointResponseAnswer.response_id.in_(response_ids),
            )
        )).scalars().all()
        for a in ans_rows:
            ans_by_response.setdefault(a.response_id, {})[a.question_id] = a

    from app.modules.sync.models import CoshCoreItem
    cosh_ids: set[str] = set()
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

    buf = io.StringIO()
    buf.write("sep=,\r\n")
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    header = [
        "Recipient Name", "Phone (masked)", "State", "District",
        "Timeline", "Fire At", "Submitted At",
    ]
    if t.content_type == TouchpointContentType.INTEREST_CAPTURE.value:
        header.append("Interest Answer")
    if t.content_type == TouchpointContentType.FEEDBACK.value:
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
            recipient.timeline_name or "",
            recipient.fire_at,
            resp.submitted_at.isoformat() if resp.submitted_at else "",
        ]
        if t.content_type == TouchpointContentType.INTEREST_CAPTURE.value:
            row.append(resp.interest_answer or "")
        if t.content_type == TouchpointContentType.FEEDBACK.value:
            row.append(str(resp.rating) if resp.rating is not None else "")
        for q in q_rows:
            a = ans_by_response.get(resp.id, {}).get(q.id)
            row.append(_display_answer(a) if a else "")
        writer.writerow(row)

    body = "﻿" + buf.getvalue()

    from app.modules.reports.audit import record_csv_download
    await record_csv_download(
        db,
        user=current_user,
        report_name="touchpoint_responses",
        client_id=cid,
        filters={
            "touchpoint_id": tid,
            "touchpoint_name": t.name,
            "content_type": t.content_type,
        },
        row_count=len(rows),
        request=request,
    )

    stamp = datetime.utcnow().strftime("%Y%m%d")
    filename = f"{cid[:8]}-{t.name[:30].replace(' ', '-')}-{stamp}.csv"
    resp_out = StreamingResponse(iter([body]), media_type="text/csv")
    resp_out.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp_out
