"""Messaging — PWA endpoints (Farmer / Dealer / Facilitator).

Recipient-side counterpart to the CA-portal router. All endpoints
assume the authenticated user IS the recipient; identity check is
via the CampaignRecipient row (not role).

Response rules (spec §3.4 / §6.4):
- Responses are FINAL — once submitted, cannot be edited or withdrawn.
- Expiry past = can still view but not submit.
- Test recipients (is_test=True) see the campaign like anyone else;
  their response IS stored but excluded from CA analytics.

Intentional feature-flag omission (2026-11-22 audit confirmation):
No endpoint here checks `client_has_feature(..., "messaging")`. This
is deliberate per spec §2 — when a paid feature expires, in-flight
campaigns and their response surfaces continue to work ("the
delivery promise was made at send time"). Only the compose side
is gated. Don't "fix" this.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.messaging.models import (
    AttachmentKind,
    Campaign, CampaignAttachment, CampaignQuestion, CampaignRecipient,
    CampaignResponse, CampaignResponseAnswer,
    CampaignStatus, CampaignType, QuestionType,
)


router = APIRouter(tags=["Messaging (PWA)"])


# ── Inbox list ───────────────────────────────────────────────────────────

@router.get("/pwa/messages/inbox")
async def inbox_list(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Recipient's active inbox. Expired/cancelled/archived campaigns
    are hidden (per spec: expired = gone from recipient's view).
    """
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(
            CampaignRecipient, Campaign,
        )
        .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
        .where(
            CampaignRecipient.user_id == current_user.id,
            Campaign.status == CampaignStatus.ACTIVE.value,
        )
        .order_by(Campaign.sent_at.desc())
    )).all()

    from app.modules.clients.models import Client
    client_ids = {c.client_id for _, c in rows}
    name_by_cid: dict[str, str] = {}
    if client_ids:
        crows = (await db.execute(
            select(Client.id, Client.display_name)
            .where(Client.id.in_(client_ids))
        )).all()
        name_by_cid = {cid: (name or "") for cid, name in crows}

    items = []
    unread = 0
    for recipient, camp in rows:
        is_unread = recipient.opened_at is None
        if is_unread:
            unread += 1
        expects_response = camp.campaign_type != CampaignType.ANNOUNCEMENT.value
        pending_response = expects_response and recipient.responded_at is None
        items.append({
            "recipient_id": recipient.id,
            "campaign_id": camp.id,
            "client_id": camp.client_id,
            "client_name": name_by_cid.get(camp.client_id, ""),
            "title": camp.title,
            "preview": (camp.body or "")[:120],
            "campaign_type": camp.campaign_type,
            "sent_at": camp.sent_at.isoformat() if camp.sent_at else None,
            "expires_at": camp.expires_at.isoformat() if camp.expires_at else None,
            "is_unread": is_unread,
            "expects_response": expects_response,
            "pending_response": pending_response,
            "is_test": recipient.is_test,
        })
    return {"items": items, "unread_count": unread}


# ── Badge count (cheap read for the nav icon) ────────────────────────────

@router.get("/pwa/messages/badge")
async def inbox_badge(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cheap scalar read for the bottom-nav badge. Counts unread
    ACTIVE campaigns for this recipient."""
    count = (await db.execute(
        select(func.count(CampaignRecipient.id))
        .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
        .where(
            CampaignRecipient.user_id == current_user.id,
            CampaignRecipient.opened_at.is_(None),
            Campaign.status == CampaignStatus.ACTIVE.value,
        )
    )).scalar_one()
    return {"unread_count": int(count or 0)}


# ── Message detail ───────────────────────────────────────────────────────

async def _get_own_recipient(
    db: AsyncSession, user_id: str, campaign_id: str,
) -> tuple[CampaignRecipient, Campaign]:
    row = (await db.execute(
        select(CampaignRecipient, Campaign)
        .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.user_id == user_id,
        )
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return row[0], row[1]


@router.get("/pwa/messages/{campaign_id}")
async def message_detail(
    campaign_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full message view. Includes questions (for SURVEY / FEEDBACK
    follow-ups), attachments (stub — will ship signed URLs in Phase
    2D), and the recipient's own answers (if already submitted — form
    becomes read-only)."""
    recipient, camp = await _get_own_recipient(db, current_user.id, campaign_id)
    now = datetime.now(timezone.utc)
    expired = camp.expires_at is not None and camp.expires_at <= now

    # Client name for the header.
    from app.modules.clients.models import Client
    client = (await db.execute(
        select(Client).where(Client.id == camp.client_id)
    )).scalar_one_or_none()

    questions = (await db.execute(
        select(CampaignQuestion)
        .where(CampaignQuestion.campaign_id == campaign_id)
        .order_by(CampaignQuestion.sort_order)
    )).scalars().all()

    attachments = (await db.execute(
        select(CampaignAttachment)
        .where(CampaignAttachment.campaign_id == campaign_id)
        .order_by(CampaignAttachment.sort_order)
    )).scalars().all()

    # If already responded, surface the recipient's own answers
    # (read-only once submitted — spec §6.4).
    response = None
    if recipient.responded_at is not None:
        resp = (await db.execute(
            select(CampaignResponse).where(
                CampaignResponse.campaign_id == campaign_id,
                CampaignResponse.user_id == current_user.id,
            )
        )).scalar_one_or_none()
        if resp is not None:
            ans_rows = (await db.execute(
                select(CampaignResponseAnswer).where(
                    CampaignResponseAnswer.response_id == resp.id,
                )
            )).scalars().all()
            response = {
                "interest_answer": resp.interest_answer,
                "rating": resp.rating,
                "answers": [_shape_answer(a) for a in ans_rows],
            }

    return {
        "campaign": {
            "id": camp.id,
            "title": camp.title,
            "body": camp.body,
            "campaign_type": camp.campaign_type,
            "rating_scale_type": camp.rating_scale_type,
            "sent_at": camp.sent_at.isoformat() if camp.sent_at else None,
            "expires_at": camp.expires_at.isoformat() if camp.expires_at else None,
            "client_name": (client.display_name if client else ""),
            "client_logo_url": (client.logo_url if client else None),
            "expired": expired,
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
                # Public S3 URL — storage_path IS the URL (same pattern
                # as the rest of RootsTalk's uploads). Phase 2E
                # (2026-11-22). CAs are told at publish time that
                # recipients can save/forward; we don't attempt to
                # enforce ephemerality on the web.
                "url": a.storage_path,
            }
            for a in attachments
        ],
        "recipient": {
            "id": recipient.id,
            "opened_at": recipient.opened_at.isoformat() if recipient.opened_at else None,
            "responded_at": recipient.responded_at.isoformat() if recipient.responded_at else None,
            "is_test": recipient.is_test,
        },
        "response": response,
    }


def _shape_answer(a: CampaignResponseAnswer) -> dict:
    return {
        "question_id": a.question_id,
        "value_text": a.value_text,
        "value_number": a.value_number,
        "value_choice": a.value_choice,
        "value_boolean": a.value_boolean,
        "value_date": a.value_date.isoformat() if a.value_date else None,
        "value_media_path": a.value_media_path,
    }


# ── Mark-opened ──────────────────────────────────────────────────────────

@router.post("/pwa/messages/{campaign_id}/opened")
async def mark_opened(
    campaign_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stamp opened_at the first time the recipient opens the message.
    Idempotent — repeat calls are no-ops. Also bumps the campaign-level
    opened_count on the first stamp only.

    Concurrency-safe (2026-11-22 audit): the opened_at flip is done
    via a conditional UPDATE with `opened_at IS NULL` in the WHERE
    clause + `RETURNING id`. Two concurrent POSTs from the same user
    (e.g. double-tap on a slow network) result in exactly one row
    update — the other UPDATE touches zero rows. Only the "winner"
    bumps the campaign counter.
    """
    recipient, _camp = await _get_own_recipient(db, current_user.id, campaign_id)
    if recipient.opened_at is not None:
        return {"ok": True, "already": True}
    now = datetime.now(timezone.utc)

    from sqlalchemy import update as _update
    result = await db.execute(
        _update(CampaignRecipient)
        .where(
            CampaignRecipient.id == recipient.id,
            CampaignRecipient.opened_at.is_(None),
        )
        .values(opened_at=now)
        .returning(CampaignRecipient.id)
    )
    won = result.scalar_one_or_none() is not None
    if not won:
        # Another concurrent call stamped first. Treat as already-opened.
        await db.commit()
        return {"ok": True, "already": True}

    # Bump campaign counter only for non-test opens, only when we
    # won the stamp race.
    if not recipient.is_test:
        await db.execute(
            _update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(opened_count=Campaign.opened_count + 1)
        )
    await db.commit()
    return {"ok": True, "opened_at": now.isoformat()}


# ── Response submit ──────────────────────────────────────────────────────

class AnswerIn(BaseModel):
    question_id: str
    value_text: Optional[str] = None
    value_number: Optional[float] = None
    value_choice: Optional[Any] = None   # str for SINGLE_CHOICE; list[str] for MULTIPLE_CHOICE
    value_boolean: Optional[bool] = None
    value_date: Optional[str] = None     # YYYY-MM-DD
    value_media_path: Optional[str] = None


class ResponseIn(BaseModel):
    # For INTEREST_CAPTURE
    interest_answer: Optional[str] = None
    # For FEEDBACK
    rating: Optional[int] = None
    # For SURVEY + FEEDBACK follow-ups
    answers: Optional[list[AnswerIn]] = None


def _coerce_answer(q: CampaignQuestion, a: AnswerIn) -> dict:
    """Validate + shape one answer per question-type. Returns dict
    of the columns to set on the CampaignResponseAnswer row."""
    qt = q.question_type
    out: dict = {}
    if qt == QuestionType.SHORT_TEXT.value or qt == QuestionType.PARAGRAPH_TEXT.value:
        if a.value_text is None and q.is_mandatory:
            raise HTTPException(
                status_code=422,
                detail={"code": "answer_required", "question_id": q.id},
            )
        out["value_text"] = (a.value_text or "").strip() or None
    elif qt == QuestionType.NUMBER.value or qt == QuestionType.RATING_SCALE.value:
        if a.value_number is None and q.is_mandatory:
            raise HTTPException(
                status_code=422,
                detail={"code": "answer_required", "question_id": q.id},
            )
        out["value_number"] = float(a.value_number) if a.value_number is not None else None
    elif qt == QuestionType.SINGLE_CHOICE.value:
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
    elif qt == QuestionType.MULTIPLE_CHOICE.value:
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
    elif qt == QuestionType.YES_NO.value:
        if a.value_boolean is None and q.is_mandatory:
            raise HTTPException(
                status_code=422,
                detail={"code": "answer_required", "question_id": q.id},
            )
        out["value_boolean"] = a.value_boolean
    elif qt == QuestionType.DATE.value:
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
    elif qt == QuestionType.PHOTO_UPLOAD.value:
        # Phase 2D will accept a real uploaded path. For now accept
        # whatever the client sends; mandatory check applies.
        if not a.value_media_path and q.is_mandatory:
            raise HTTPException(
                status_code=422,
                detail={"code": "answer_required", "question_id": q.id},
            )
        out["value_media_path"] = a.value_media_path
    return out


@router.post("/pwa/messages/{campaign_id}/response")
async def submit_response(
    campaign_id: str, body: ResponseIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Submit the recipient's response. Rules:
      - ANNOUNCEMENT: returns 422 (no response collected).
      - INTEREST_CAPTURE: requires `interest_answer ∈ {YES, NO, MAYBE}`.
      - FEEDBACK: requires `rating` (1..5 for STAR_5, 1..10 for NPS_10);
        optional follow-up answers.
      - SURVEY: requires `answers` for every mandatory question.
      - Campaign must be ACTIVE and not past expires_at.
      - Idempotent — resubmits return the existing response (NEVER
        overwrite, per spec: Responses are final).
    """
    recipient, camp = await _get_own_recipient(db, current_user.id, campaign_id)
    now = datetime.now(timezone.utc)

    if camp.status != CampaignStatus.ACTIVE.value:
        raise HTTPException(
            status_code=422,
            detail={"code": "campaign_not_active"},
        )
    if camp.expires_at is not None and camp.expires_at <= now:
        raise HTTPException(
            status_code=422,
            detail={"code": "campaign_expired"},
        )

    if camp.campaign_type == CampaignType.ANNOUNCEMENT.value:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_response_collected",
                "message": "Announcements don't collect responses.",
            },
        )

    # Idempotency — if a response already exists, return it unchanged.
    existing = (await db.execute(
        select(CampaignResponse).where(
            CampaignResponse.campaign_id == campaign_id,
            CampaignResponse.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return {
            "ok": True, "already": True,
            "response_id": existing.id,
            "submitted_at": existing.submitted_at.isoformat() if existing.submitted_at else None,
        }

    # Validate per-type.
    interest = None
    rating = None
    if camp.campaign_type == CampaignType.INTEREST_CAPTURE.value:
        a = (body.interest_answer or "").upper()
        if a not in ("YES", "NO", "MAYBE"):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_interest_answer"},
            )
        interest = a
    elif camp.campaign_type == CampaignType.FEEDBACK.value:
        scale = (camp.rating_scale_type or "STAR_5").upper()
        max_rating = 10 if scale == "NPS_10" else 5
        if body.rating is None or not (1 <= int(body.rating) <= max_rating):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_rating", "max": max_rating},
            )
        rating = int(body.rating)

    # SURVEY / FEEDBACK-follow-ups — validate mandatory answers.
    questions = (await db.execute(
        select(CampaignQuestion)
        .where(CampaignQuestion.campaign_id == campaign_id)
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

    # Write response + answers.
    resp = CampaignResponse(
        campaign_id=campaign_id,
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
            continue  # Silently drop unknown question ids.
        cols = _coerce_answer(q, a)
        db.add(CampaignResponseAnswer(
            response_id=resp.id,
            question_id=qid,
            **cols,
        ))

    recipient.responded_at = now
    # Bump campaign counter for non-test only.
    if not recipient.is_test:
        await db.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(responded_count=Campaign.responded_count + 1)
        )
    await db.commit()
    return {"ok": True, "response_id": resp.id, "submitted_at": now.isoformat()}
