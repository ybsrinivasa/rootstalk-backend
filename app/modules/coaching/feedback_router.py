"""Coaching Student Feedback — Moment 1 (onboarding) + Moment 2 (post-session).

See `project_coaching_student_feedback_design_2026_10_16.md` for the
full design. Thin router; business logic in sibling service functions.

Endpoints:
  Moment 1 (student-authored, requires auth as the student themselves):
    GET  /coaching/moment1
    PUT  /coaching/moment1
  Moment 2 — Coach side (requires coach or SA):
    POST /coaching/sessions/{sid}/students/{stid}/capture-feedback
    GET  /coaching/sessions/{sid}/students/{stid}/moment2-status
    GET  /coaching/sessions/{sid}/students/{stid}/moment2-content
  Moment 2 — Student side (token-authenticated, no login):
    GET  /coaching/feedback/{token}
    POST /coaching/feedback/{token}/submit
  Neytiri aggregation (SA + CM):
    GET  /neytiri/coaching-feedback
"""
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.clients.models import Client
from app.modules.coaching import service as coaching_service
from app.modules.coaching.emails import send_feedback_request_email
from app.modules.coaching.models import (
    CoachingSession, CoachingStudent,
    CoachingStudentMoment1Response, CoachingStudentMoment2Feedback,
    utcnow,
)
from app.modules.platform.models import RoleType, StatusEnum, User


router = APIRouter(tags=["Coaching Feedback"])


# ── Pydantic schemas ──────────────────────────────────────────────────────

class Moment1AnswersPayload(BaseModel):
    """Opaque dict keyed by question identifier — the backend doesn't
    constrain the key set; frontend decides which questions to ship.
    All answers are optional per design (students may come with no
    expectations). Values are text; empty strings are treated as
    'not answered'."""
    answers: dict


class Moment1AnswersOut(BaseModel):
    answers: dict
    updated_at: Optional[datetime] = None


class CaptureFeedbackOut(BaseModel):
    student_id: str
    requested_at: datetime
    submitted_at: Optional[datetime]
    status: str  # "pending" | "submitted"


class Moment2SubmitPayload(BaseModel):
    """Student-submitted feedback content. Opaque JSON — the frontend
    assembles the three-section structure (mirror / benchmark / open)
    with 1-5 star rating and recommend Yes/Maybe/No. The backend
    doesn't validate the shape."""
    content: dict


class Moment2FormOut(BaseModel):
    """Student-facing form state. Includes mirror of Moment 1 answers
    (for Section 1 of the form) and token metadata."""
    student_name: str
    reference_client_name: str
    moment1_answers: dict
    requested_at: datetime
    already_submitted: bool


class Moment2StatusOut(BaseModel):
    requested_at: Optional[datetime]
    submitted_at: Optional[datetime]
    reminder_sent_at: Optional[datetime]
    coach_nudge_sent_at: Optional[datetime]
    status: str  # "not_requested" | "pending" | "submitted"


class Moment2ContentOut(BaseModel):
    """Post-cert read of the student's submitted content. Coach side."""
    submitted_at: datetime
    content: dict


class NeytiriFeedbackRow(BaseModel):
    student_id: str
    student_name: str
    session_id: str
    reference_client_name: str
    coach_name: str
    requested_at: datetime
    submitted_at: Optional[datetime]
    content: Optional[dict]


# ── Helpers ───────────────────────────────────────────────────────────────

async def _get_student_for_this_user(
    db: AsyncSession, user_id: str,
) -> CoachingStudent:
    """Return the CoachingStudent row for the authenticated user; 404
    if the user isn't a coaching student."""
    cs = (await db.execute(
        select(CoachingStudent).where(CoachingStudent.user_id == user_id)
    )).scalar_one_or_none()
    if cs is None:
        raise HTTPException(
            status_code=404,
            detail="Not a coaching student",
        )
    return cs


async def _require_coach_or_sa(current_user: User) -> None:
    """Permit if the user has the COACH role OR is SA (sa_email).
    Session-ownership + the specific coach↔session binding are
    enforced by `require_session_owner` where applicable."""
    if current_user.email == settings.sa_email:
        return
    active_roles = {
        r.role_type for r in current_user.roles
        if r.status == StatusEnum.ACTIVE
    }
    if RoleType.COACH not in active_roles:
        raise HTTPException(
            status_code=403, detail="Coach or SA access required",
        )


async def _require_sa_or_cm(current_user: User) -> None:
    """Neytiri aggregation gate."""
    if current_user.email == settings.sa_email:
        return
    active_roles = {
        r.role_type for r in current_user.roles
        if r.status == StatusEnum.ACTIVE
    }
    if RoleType.CONTENT_MANAGER not in active_roles:
        raise HTTPException(
            status_code=403,
            detail="SA or Content Manager access required",
        )


# ── Moment 1 — Student-authored ────────────────────────────────────────────

@router.get("/coaching/moment1", response_model=Moment1AnswersOut)
async def get_moment1(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch the current student's Moment 1 answers (expectations).
    Returns empty answers if the student hasn't submitted anything."""
    cs = await _get_student_for_this_user(db, current_user.id)
    row = (await db.execute(
        select(CoachingStudentMoment1Response).where(
            CoachingStudentMoment1Response.student_id == cs.id,
        )
    )).scalar_one_or_none()
    if row is None:
        return Moment1AnswersOut(answers={}, updated_at=None)
    return Moment1AnswersOut(answers=row.answers_json or {}, updated_at=row.updated_at)


@router.put("/coaching/moment1", response_model=Moment1AnswersOut)
async def put_moment1(
    payload: Moment1AnswersPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upsert the current student's Moment 1 answers. Allowed during
    ACTIVE coaching (per design — student's own words, their
    perspective is allowed to evolve)."""
    cs = await _get_student_for_this_user(db, current_user.id)
    row = (await db.execute(
        select(CoachingStudentMoment1Response).where(
            CoachingStudentMoment1Response.student_id == cs.id,
        )
    )).scalar_one_or_none()
    if row is None:
        row = CoachingStudentMoment1Response(
            student_id=cs.id,
            answers_json=payload.answers or {},
        )
        db.add(row)
    else:
        row.answers_json = payload.answers or {}
    await db.commit()
    await db.refresh(row)
    return Moment1AnswersOut(answers=row.answers_json or {}, updated_at=row.updated_at)


# ── Moment 1 — Coach-side read ─────────────────────────────────────────────

@router.get(
    "/coaching/sessions/{session_id}/students/{student_id}/moment1",
    response_model=Moment1AnswersOut,
)
async def coach_view_moment1(
    session_id: str,
    student_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Coach-side read of a student's Moment 1 answers (expectations).

    Unlike Moment 2 content — which is held back until the certificate
    is issued to keep certification pressure out of feedback — Moment 1
    is visible to the coach throughout. The coach tailors their coaching
    better when they can see what the student came in hoping for.

    Permission: coach of this session (or SA/CM).
    Returns `answers={}` + `updated_at=None` when the student skipped
    or hasn't started Moment 1 yet (same empty shape as the student's
    own GET)."""
    await _require_coach_or_sa(current_user)
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    student = (await db.execute(
        select(CoachingStudent).where(
            CoachingStudent.id == student_id,
            CoachingStudent.session_id == session.id,
        )
    )).scalar_one_or_none()
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")

    row = (await db.execute(
        select(CoachingStudentMoment1Response).where(
            CoachingStudentMoment1Response.student_id == student.id,
        )
    )).scalar_one_or_none()
    if row is None:
        return Moment1AnswersOut(answers={}, updated_at=None)
    return Moment1AnswersOut(
        answers=row.answers_json or {},
        updated_at=row.updated_at,
    )


# ── Moment 2 — Coach triggers capture ──────────────────────────────────────

@router.post(
    "/coaching/sessions/{session_id}/students/{student_id}/capture-feedback",
    response_model=CaptureFeedbackOut,
)
async def capture_feedback(
    session_id: str,
    student_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Coach clicks 'Capture Feedback' on a student card after the
    session has closed. Creates a Moment2Feedback row (if not already)
    and sends the student a one-click email with the feedback form link.

    Idempotent on re-click: if a row exists but hasn't been submitted,
    re-sends the email (useful if the student lost the first one).
    If a row exists and HAS been submitted, 409 — nothing to re-do.

    Session must be CLOSED (grading preconditions mirror the certify
    endpoint)."""
    await _require_coach_or_sa(current_user)
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    if session.status not in ("CLOSED_MANUAL", "CLOSED_AUTO"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_not_closed",
                "message": "Feedback can only be captured after the session has closed.",
            },
        )
    student = (await db.execute(
        select(CoachingStudent).where(
            CoachingStudent.id == student_id,
            CoachingStudent.session_id == session.id,
        )
    )).scalar_one_or_none()
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")

    row = (await db.execute(
        select(CoachingStudentMoment2Feedback).where(
            CoachingStudentMoment2Feedback.student_id == student.id,
        )
    )).scalar_one_or_none()
    if row is None:
        row = CoachingStudentMoment2Feedback(
            student_id=student.id,
            feedback_token=secrets.token_urlsafe(32),
            requested_by_coach_user_id=current_user.id,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    else:
        if row.submitted_at is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "feedback_already_submitted",
                    "message": "The student has already submitted feedback.",
                },
            )

    # Dispatch (or re-dispatch) the email. Best-effort.
    try:
        student_user = (await db.execute(
            select(User).where(User.id == student.user_id)
        )).scalar_one()
        if student_user.email:
            portal_root = (
                settings.frontend_base_url.rstrip("/")
                if settings.frontend_base_url else "https://rootstalk.in"
            )
            feedback_url = f"{portal_root}/f/{row.feedback_token}"
            send_feedback_request_email(
                to_email=student_user.email,
                student_name=student_user.name or "Coaching Student",
                feedback_url=feedback_url,
                is_reminder=False,
            )
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "capture-feedback email dispatch failed for student %s",
            student.id,
        )

    return CaptureFeedbackOut(
        student_id=student.id,
        requested_at=row.requested_at,
        submitted_at=row.submitted_at,
        status="submitted" if row.submitted_at else "pending",
    )


@router.get(
    "/coaching/sessions/{session_id}/students/{student_id}/moment2-status",
    response_model=Moment2StatusOut,
)
async def moment2_status(
    session_id: str,
    student_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Coach-side read: 'has the student submitted feedback yet?'
    Content stays hidden until the certificate is issued."""
    await _require_coach_or_sa(current_user)
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    student = (await db.execute(
        select(CoachingStudent).where(
            CoachingStudent.id == student_id,
            CoachingStudent.session_id == session.id,
        )
    )).scalar_one_or_none()
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")

    row = (await db.execute(
        select(CoachingStudentMoment2Feedback).where(
            CoachingStudentMoment2Feedback.student_id == student.id,
        )
    )).scalar_one_or_none()
    if row is None:
        return Moment2StatusOut(
            requested_at=None, submitted_at=None,
            reminder_sent_at=None, coach_nudge_sent_at=None,
            status="not_requested",
        )
    return Moment2StatusOut(
        requested_at=row.requested_at,
        submitted_at=row.submitted_at,
        reminder_sent_at=row.reminder_sent_at,
        coach_nudge_sent_at=row.coach_nudge_sent_at,
        status="submitted" if row.submitted_at else "pending",
    )


@router.get(
    "/coaching/sessions/{session_id}/students/{student_id}/moment2-content",
    response_model=Moment2ContentOut,
)
async def moment2_content(
    session_id: str,
    student_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Coach-side read of the student's submitted content. Only
    available AFTER the certificate has been issued — the gate removes
    the perceived 'coach reads my feedback before deciding my cert'
    pressure. See design memo for the rationale."""
    await _require_coach_or_sa(current_user)
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    student = (await db.execute(
        select(CoachingStudent).where(
            CoachingStudent.id == student_id,
            CoachingStudent.session_id == session.id,
        )
    )).scalar_one_or_none()
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    if student.certified_at is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "cert_not_issued",
                "message": (
                    "Feedback content becomes readable after the "
                    "certificate is issued."
                ),
            },
        )

    row = (await db.execute(
        select(CoachingStudentMoment2Feedback).where(
            CoachingStudentMoment2Feedback.student_id == student.id,
        )
    )).scalar_one_or_none()
    if row is None or row.submitted_at is None:
        raise HTTPException(status_code=404, detail="No feedback submitted")
    return Moment2ContentOut(
        submitted_at=row.submitted_at,
        content=row.content_json or {},
    )


# ── Moment 2 — Student-facing form (token-authenticated) ──────────────────

@router.get("/coaching/feedback/{token}", response_model=Moment2FormOut)
async def get_feedback_form(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Student taps the email link → lands here. Token identifies the
    student + session; returns the mirror of Moment 1 for Section 1
    of the form. No login required.

    Already-submitted token still returns 200 so the student sees
    confirmation they've already shared feedback — but
    `already_submitted=True` tells the frontend to render a
    thank-you page instead of the form."""
    row = (await db.execute(
        select(CoachingStudentMoment2Feedback).where(
            CoachingStudentMoment2Feedback.feedback_token == token,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Invalid feedback link")
    student = await db.get(CoachingStudent, row.student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    student_user = (await db.execute(
        select(User).where(User.id == student.user_id)
    )).scalar_one()
    session = await db.get(CoachingSession, student.session_id)
    ref_client = (await db.execute(
        select(Client).where(Client.id == session.reference_client_id)
    )).scalar_one()
    moment1 = (await db.execute(
        select(CoachingStudentMoment1Response).where(
            CoachingStudentMoment1Response.student_id == student.id,
        )
    )).scalar_one_or_none()

    return Moment2FormOut(
        student_name=student_user.name or "Coaching Student",
        reference_client_name=ref_client.full_name,
        moment1_answers=(moment1.answers_json if moment1 else {}) or {},
        requested_at=row.requested_at,
        already_submitted=row.submitted_at is not None,
    )


@router.post("/coaching/feedback/{token}/submit")
async def submit_feedback(
    token: str,
    payload: Moment2SubmitPayload,
    db: AsyncSession = Depends(get_db),
):
    """Student submits the form. Content_json shape is up to the
    frontend; backend treats it as opaque. Token becomes effectively
    single-use after this (submission is final — design says 'not
    editable'; subsequent regret goes via direct message)."""
    row = (await db.execute(
        select(CoachingStudentMoment2Feedback).where(
            CoachingStudentMoment2Feedback.feedback_token == token,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Invalid feedback link")
    if row.submitted_at is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "already_submitted",
                "message": (
                    "Thanks — your feedback has already been received. "
                    "If you'd like to revise it, please contact your "
                    "coach directly."
                ),
            },
        )
    row.content_json = payload.content or {}
    row.submitted_at = utcnow()
    await db.commit()
    return {"ok": True}


# ── Neytiri aggregation ────────────────────────────────────────────────────

@router.get("/neytiri/coaching-feedback", response_model=list[NeytiriFeedbackRow])
async def list_aggregated_feedback(
    only_submitted: bool = True,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Neytiri-wide feedback aggregation. SA + CM only. Returns rows
    across all Clients + Sessions. v1 is a plain list; filtering by
    time range / rating / Client can be added later."""
    await _require_sa_or_cm(current_user)
    q = (
        select(
            CoachingStudentMoment2Feedback,
            CoachingStudent,
            CoachingSession,
            Client,
            User,
        )
        .join(CoachingStudent, CoachingStudent.id == CoachingStudentMoment2Feedback.student_id)
        .join(CoachingSession, CoachingSession.id == CoachingStudent.session_id)
        .join(Client, Client.id == CoachingSession.reference_client_id)
        .join(User, User.id == CoachingStudent.user_id)
        .order_by(CoachingStudentMoment2Feedback.submitted_at.desc().nulls_last())
    )
    if only_submitted:
        q = q.where(CoachingStudentMoment2Feedback.submitted_at.isnot(None))
    rows = (await db.execute(q)).all()

    # Fetch coach names in one shot
    coach_ids = {r[2].coach_user_id for r in rows if r[2].coach_user_id}
    coaches_by_id: dict[str, str] = {}
    if coach_ids:
        coach_rows = (await db.execute(
            select(User.id, User.name, User.email).where(User.id.in_(coach_ids))
        )).all()
        for cid, name, email in coach_rows:
            coaches_by_id[cid] = name or email or "Coach"

    out: list[NeytiriFeedbackRow] = []
    for fb, cs, session, ref_client, student_user in rows:
        out.append(NeytiriFeedbackRow(
            student_id=cs.id,
            student_name=student_user.name or student_user.email or "Student",
            session_id=session.id,
            reference_client_name=ref_client.full_name,
            coach_name=coaches_by_id.get(session.coach_user_id, "Coach"),
            requested_at=fb.requested_at,
            submitted_at=fb.submitted_at,
            content=fb.content_json if fb.submitted_at else None,
        ))
    return out
