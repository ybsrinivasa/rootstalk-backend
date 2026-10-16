"""Coaching Student Feedback — reminder scheduler (2026-10-16).

Hourly Celery beat task. For each Moment2Feedback row where
`submitted_at IS NULL`:
  - +48h past requested_at + reminder_sent_at IS NULL → auto-send a
    reminder email, stamp reminder_sent_at.
  - +96h past requested_at + coach_nudge_sent_at IS NULL → stamp the
    coach_nudge_sent_at (frontend reads this to show a nudge card on
    the coach's student list). No email to the coach — the dashboard
    nudge is sufficient; coach is expected to be checking regularly.

Both windows are measured from the SAME requested_at (so the +96h
coach-nudge fires 48h after the student reminder, not 96h after it).

Idempotent — stamping the timestamps prevents re-fires.
"""
import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.config import settings
from app.database import AsyncSessionLocal
from app.modules.coaching.models import (
    CoachingStudent, CoachingStudentMoment2Feedback, utcnow,
)
from app.modules.platform.models import User


logger = logging.getLogger(__name__)

REMINDER_AFTER_HOURS = 48
COACH_NUDGE_AFTER_HOURS = 96


async def _sweep_reminders(db: AsyncSession) -> dict:
    now = utcnow()
    reminder_cutoff = now - timedelta(hours=REMINDER_AFTER_HOURS)
    nudge_cutoff = now - timedelta(hours=COACH_NUDGE_AFTER_HOURS)

    pending = (await db.execute(
        select(CoachingStudentMoment2Feedback).where(
            CoachingStudentMoment2Feedback.submitted_at.is_(None),
        )
    )).scalars().all()

    reminders_sent = 0
    nudges_stamped = 0

    for row in pending:
        # +96h → coach nudge
        if (
            row.coach_nudge_sent_at is None
            and row.requested_at <= nudge_cutoff
        ):
            row.coach_nudge_sent_at = now
            nudges_stamped += 1
            logger.info(
                "Moment2 coach-nudge stamped for student %s",
                row.student_id,
            )

        # +48h → student reminder email
        if (
            row.reminder_sent_at is None
            and row.requested_at <= reminder_cutoff
        ):
            try:
                student = await db.get(CoachingStudent, row.student_id)
                if student is None:
                    continue
                student_user = (await db.execute(
                    select(User).where(User.id == student.user_id)
                )).scalar_one_or_none()
                if student_user is None or not student_user.email:
                    row.reminder_sent_at = now
                    continue
                portal_root = (
                    settings.frontend_base_url.rstrip("/")
                    if settings.frontend_base_url else "https://rootstalk.in"
                )
                feedback_url = f"{portal_root}/f/{row.feedback_token}"
                from app.modules.coaching.emails import send_feedback_request_email
                send_feedback_request_email(
                    to_email=student_user.email,
                    student_name=student_user.name or "Coaching Student",
                    feedback_url=feedback_url,
                    is_reminder=True,
                )
                row.reminder_sent_at = now
                reminders_sent += 1
                logger.info(
                    "Moment2 reminder email sent for student %s",
                    row.student_id,
                )
            except Exception as e:
                logger.warning(
                    "Moment2 reminder dispatch failed for student %s: %s",
                    row.student_id, e,
                )

    if reminders_sent or nudges_stamped:
        await db.commit()
    return {"reminders_sent": reminders_sent, "nudges_stamped": nudges_stamped}


async def _run_sweep() -> dict:
    async with AsyncSessionLocal() as db:
        return await _sweep_reminders(db)


@celery_app.task(name="app.tasks.coaching_feedback_reminders.sweep_feedback_reminders")
def sweep_feedback_reminders() -> None:
    """Beat entry: runs hourly."""
    result = asyncio.run(_run_sweep())
    logger.info(
        "Coaching feedback reminder sweep — reminders_sent=%d, nudges_stamped=%d",
        result["reminders_sent"], result["nudges_stamped"],
    )
