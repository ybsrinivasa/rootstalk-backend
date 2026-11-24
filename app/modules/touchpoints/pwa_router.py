"""Subscription Touchpoints — PWA endpoints (recipient-side).

Companion to `app/modules/messaging/pwa_router.py`. Keyed by
`recipient_id` (not touchpoint_id) because a single touchpoint can
fire multiple times to the same farmer — once per (assignment,
subscription) pair. The recipient row is the natural per-firing
handle.

Intentional feature-flag omission — mirror of the Messaging rule:
no endpoint here checks `client_has_feature(..., "touchpoints")`.
In-flight firings continue to work after the feature-access window
expires (spec §2 — delivery promise was made at firing time). Only
compose-side endpoints in `router.py` are gated. Don't "fix" this.

Response rules (spec §6.4):
- Responses are FINAL — resubmit returns existing, never overwrites.
- Expiry past = can still view but not submit.
- Content-lock side effect: first recipient's open sets
  `touchpoints.content_locked_at`. The composer PATCH on this
  touchpoint will 422 after that moment.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.touchpoints.models import (
    Touchpoint, TouchpointAttachment, TouchpointContentType,
    TouchpointQuestion, TouchpointQuestionType, TouchpointRecipient,
    TouchpointResponse, TouchpointResponseAnswer,
)


router = APIRouter(tags=["Touchpoints (PWA)"])


# ── Helpers ────────────────────────────────────────────────────────────

async def _get_own_firing(
    db: AsyncSession, user_id: str, recipient_id: str,
) -> tuple[TouchpointRecipient, Touchpoint]:
    row = (await db.execute(
        select(TouchpointRecipient, Touchpoint)
        .join(Touchpoint, Touchpoint.id == TouchpointRecipient.touchpoint_id)
        .where(
            TouchpointRecipient.id == recipient_id,
            TouchpointRecipient.user_id == user_id,
        )
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return row[0], row[1]


def _fmt_fire_at(fire_at: str) -> str:
    return "Beginning of" if fire_at == "BEGINNING" else "End of"


def _shape_answer(a: TouchpointResponseAnswer) -> dict:
    return {
        "question_id": a.question_id,
        "value_text": a.value_text,
        "value_number": a.value_number,
        "value_choice": a.value_choice,
        "value_boolean": a.value_boolean,
        "value_date": a.value_date.isoformat() if a.value_date else None,
        "value_media_path": a.value_media_path,
    }


# ── Detail ──────────────────────────────────────────────────────────────

@router.get("/pwa/messages/touchpoints/{recipient_id}")
async def touchpoint_detail(
    recipient_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full touchpoint-firing view — same shape as the Messaging
    detail endpoint so PWA response-form widgets can be reused as-is.

    Adds a `subscription_context` object specific to touchpoints
    (crop name, timeline name, fire_at label) so the PWA can render
    the "About your Chilli Kharif Standard · End of Vegetative
    Growth" subtitle without a second fetch.
    """
    recipient, tp = await _get_own_firing(db, current_user.id, recipient_id)
    now = datetime.now(timezone.utc)
    expired = recipient.expires_at <= now

    # Client meta for header
    from app.modules.clients.models import Client
    client = (await db.execute(
        select(Client).where(Client.id == tp.client_id)
    )).scalar_one_or_none()

    questions = (await db.execute(
        select(TouchpointQuestion)
        .where(TouchpointQuestion.touchpoint_id == tp.id)
        .order_by(TouchpointQuestion.sort_order)
    )).scalars().all()

    attachments = (await db.execute(
        select(TouchpointAttachment)
        .where(TouchpointAttachment.touchpoint_id == tp.id)
        .order_by(TouchpointAttachment.sort_order)
    )).scalars().all()

    # Resolve crop_cosh_id → name for the context tag.
    from app.modules.sync.models import CoshCoreItem
    crop_name = ""
    if recipient.crop_cosh_id:
        core = (await db.execute(
            select(CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id == recipient.crop_cosh_id)
        )).scalar_one_or_none()
        if core is not None:
            crop_name = (core or {}).get("en") or ""

    response = None
    if recipient.responded_at is not None:
        resp = (await db.execute(
            select(TouchpointResponse).where(
                TouchpointResponse.recipient_id == recipient.id,
            )
        )).scalar_one_or_none()
        if resp is not None:
            ans_rows = (await db.execute(
                select(TouchpointResponseAnswer).where(
                    TouchpointResponseAnswer.response_id == resp.id,
                )
            )).scalars().all()
            response = {
                "interest_answer": resp.interest_answer,
                "rating": resp.rating,
                "answers": [_shape_answer(a) for a in ans_rows],
            }

    return {
        # Keyed under "touchpoint" (not "campaign") so the PWA page
        # can distinguish the shape when reusing widgets. Field names
        # match the campaign shape where possible.
        "touchpoint": {
            "id": tp.id,
            "client_id": tp.client_id,
            "title": tp.title,
            "body": tp.body,
            # Reuse `campaign_type` key name for widget-level
            # compatibility — same string values so widgets switch
            # on it identically.
            "campaign_type": tp.content_type,
            "rating_scale_type": tp.rating_scale_type,
            "fired_at": recipient.fired_at.isoformat() if recipient.fired_at else None,
            "expires_at": recipient.expires_at.isoformat() if recipient.expires_at else None,
            "client_name": (client.display_name if client else "") or "",
            "client_logo_url": (client.logo_url if client else None),
            "expired": expired,
        },
        "subscription_context": {
            "crop_name": crop_name,
            "crop_cosh_id": recipient.crop_cosh_id,
            "timeline_name": recipient.timeline_name,
            "fire_at": recipient.fire_at,
            "fire_at_label": _fmt_fire_at(recipient.fire_at),
        },
        "questions": [
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
            for q in questions
        ],
        "attachments": [
            {
                "id": a.id,
                "kind": a.kind,
                "original_filename": a.original_filename,
                "mime_type": a.mime_type,
                "size_bytes": a.size_bytes,
                "url": a.storage_path,
            }
            for a in attachments
        ],
        "recipient": {
            "id": recipient.id,
            "opened_at": recipient.opened_at.isoformat() if recipient.opened_at else None,
            "responded_at": recipient.responded_at.isoformat() if recipient.responded_at else None,
        },
        "response": response,
    }


# ── Mark-opened ────────────────────────────────────────────────────────

@router.post("/pwa/messages/touchpoints/{recipient_id}/opened")
async def mark_opened(
    recipient_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stamp opened_at idempotently. First-open across all firings of
    a touchpoint also stamps `touchpoints.content_locked_at` — the
    composer's content-edit guard references this (spec §4.2 lock-
    on-first-open).

    Atomic flip via conditional UPDATE + RETURNING to avoid the
    double-increment race fixed during the Messaging audit.
    """
    recipient, tp = await _get_own_firing(db, current_user.id, recipient_id)
    if recipient.opened_at is not None:
        return {"ok": True, "already": True}
    now = datetime.now(timezone.utc)

    result = await db.execute(
        update(TouchpointRecipient)
        .where(
            TouchpointRecipient.id == recipient.id,
            TouchpointRecipient.opened_at.is_(None),
        )
        .values(opened_at=now)
        .returning(TouchpointRecipient.id)
    )
    won = result.scalar_one_or_none() is not None
    if not won:
        # A concurrent call stamped first.
        await db.commit()
        return {"ok": True, "already": True}

    # First-open content-lock. Set content_locked_at on the parent
    # touchpoint iff this was the first recipient to open any firing
    # of this touchpoint (across ALL recipients). Conditional UPDATE
    # with `content_locked_at IS NULL` makes this safe under races.
    if tp.content_locked_at is None:
        await db.execute(
            update(Touchpoint)
            .where(
                Touchpoint.id == tp.id,
                Touchpoint.content_locked_at.is_(None),
            )
            .values(content_locked_at=now)
        )

    await db.commit()
    return {"ok": True, "opened_at": now.isoformat()}


# ── Response submit ────────────────────────────────────────────────────

class AnswerIn(BaseModel):
    question_id: str
    value_text: Optional[str] = None
    value_number: Optional[float] = None
    value_choice: Optional[Any] = None
    value_boolean: Optional[bool] = None
    value_date: Optional[str] = None
    value_media_path: Optional[str] = None


class ResponseIn(BaseModel):
    interest_answer: Optional[str] = None
    rating: Optional[int] = None
    answers: Optional[list[AnswerIn]] = None


def _coerce_answer(q: TouchpointQuestion, a: AnswerIn) -> dict:
    """Per-type validation (mirror of messaging). MULTIPLE_CHOICE
    stored as comma-joined; options validated to not contain commas
    at composer time so this is safe."""
    qt = q.question_type
    out: dict = {}
    if qt in (TouchpointQuestionType.SHORT_TEXT.value, TouchpointQuestionType.PARAGRAPH_TEXT.value):
        if a.value_text is None and q.is_mandatory:
            raise HTTPException(
                status_code=422,
                detail={"code": "answer_required", "question_id": q.id},
            )
        out["value_text"] = (a.value_text or "").strip() or None
    elif qt in (TouchpointQuestionType.NUMBER.value, TouchpointQuestionType.RATING_SCALE.value):
        if a.value_number is None and q.is_mandatory:
            raise HTTPException(
                status_code=422,
                detail={"code": "answer_required", "question_id": q.id},
            )
        out["value_number"] = float(a.value_number) if a.value_number is not None else None
    elif qt == TouchpointQuestionType.SINGLE_CHOICE.value:
        v = a.value_choice
        if (v is None or v == "") and q.is_mandatory:
            raise HTTPException(
                status_code=422,
                detail={"code": "answer_required", "question_id": q.id},
            )
        if v is not None and q.choice_options and v not in q.choice_options:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_choice", "question_id": q.id},
            )
        out["value_choice"] = str(v) if v is not None else None
    elif qt == TouchpointQuestionType.MULTIPLE_CHOICE.value:
        v = a.value_choice or []
        if isinstance(v, str):
            v = [v]
        if not v and q.is_mandatory:
            raise HTTPException(
                status_code=422,
                detail={"code": "answer_required", "question_id": q.id},
            )
        if q.choice_options:
            for item in v:
                if item not in q.choice_options:
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "invalid_choice", "question_id": q.id, "value": item},
                    )
        out["value_choice"] = ",".join(str(x) for x in v) if v else None
    elif qt == TouchpointQuestionType.YES_NO.value:
        if a.value_boolean is None and q.is_mandatory:
            raise HTTPException(
                status_code=422,
                detail={"code": "answer_required", "question_id": q.id},
            )
        out["value_boolean"] = a.value_boolean
    elif qt == TouchpointQuestionType.DATE.value:
        if not a.value_date and q.is_mandatory:
            raise HTTPException(
                status_code=422,
                detail={"code": "answer_required", "question_id": q.id},
            )
        if a.value_date:
            try:
                out["value_date"] = datetime.fromisoformat(a.value_date).date()
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "invalid_date", "question_id": q.id},
                )
        else:
            out["value_date"] = None
    elif qt == TouchpointQuestionType.PHOTO_UPLOAD.value:
        if not a.value_media_path and q.is_mandatory:
            raise HTTPException(
                status_code=422,
                detail={"code": "answer_required", "question_id": q.id},
            )
        out["value_media_path"] = a.value_media_path
    return out


@router.post("/pwa/messages/touchpoints/{recipient_id}/response")
async def submit_response(
    recipient_id: str, body: ResponseIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Submit the recipient's response. Mirror of
    messaging.submit_response rules: idempotent, final-once-submitted,
    per-type validation, mandatory-question check.

    Keyed by `recipient_id` (not touchpoint_id) because a single
    touchpoint can fire multiple times to the same user (one per
    subscription on a matching PoP) — each firing has its own
    response.
    """
    recipient, tp = await _get_own_firing(db, current_user.id, recipient_id)
    now = datetime.now(timezone.utc)

    if tp.status != "ACTIVE":
        raise HTTPException(
            status_code=422, detail={"code": "touchpoint_not_active"},
        )
    if recipient.expires_at <= now:
        raise HTTPException(
            status_code=422, detail={"code": "touchpoint_expired"},
        )
    if tp.content_type == TouchpointContentType.ANNOUNCEMENT.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_response_collected",
                "message": "Announcements don't collect responses.",
            },
        )

    # Idempotency — resubmit returns the existing response unchanged.
    existing = (await db.execute(
        select(TouchpointResponse).where(
            TouchpointResponse.recipient_id == recipient.id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return {
            "ok": True, "already": True,
            "response_id": existing.id,
            "submitted_at": existing.submitted_at.isoformat() if existing.submitted_at else None,
        }

    interest = None
    rating = None
    if tp.content_type == TouchpointContentType.INTEREST_CAPTURE.value:
        a = (body.interest_answer or "").upper()
        if a not in ("YES", "NO", "MAYBE"):
            raise HTTPException(
                status_code=422, detail={"code": "invalid_interest_answer"},
            )
        interest = a
    elif tp.content_type == TouchpointContentType.FEEDBACK.value:
        scale = (tp.rating_scale_type or "STAR_5").upper()
        max_rating = 10 if scale == "NPS_10" else 5
        if body.rating is None or not (1 <= int(body.rating) <= max_rating):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_rating", "max": max_rating},
            )
        rating = int(body.rating)

    questions = (await db.execute(
        select(TouchpointQuestion).where(TouchpointQuestion.touchpoint_id == tp.id)
    )).scalars().all()
    q_by_id = {q.id: q for q in questions}
    answers_in = {a.question_id: a for a in (body.answers or [])}
    for q in questions:
        a = answers_in.get(q.id)
        if a is None and q.is_mandatory:
            raise HTTPException(
                status_code=422,
                detail={"code": "answer_required", "question_id": q.id},
            )

    resp = TouchpointResponse(
        touchpoint_id=tp.id,
        recipient_id=recipient.id,
        user_id=current_user.id,
        interest_answer=interest,
        rating=rating,
        submitted_at=now,
    )
    db.add(resp)
    await db.flush()

    for qid, a in answers_in.items():
        q = q_by_id.get(qid)
        if q is None:
            continue
        cols = _coerce_answer(q, a)
        db.add(TouchpointResponseAnswer(
            response_id=resp.id,
            question_id=qid,
            **cols,
        ))

    recipient.responded_at = now
    await db.commit()
    return {"ok": True, "response_id": resp.id, "submitted_at": now.isoformat()}
