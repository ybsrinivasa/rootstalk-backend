"""Coaching Sandbox — hourly auto-close sweep.

Coaching sessions have a 30-day lifetime measured from `started_at`
(not `created_at` — the coach can leave a session in DRAFT for a long
while before starting it). Once 30 days elapse, the session transitions
ACTIVE → CLOSED_AUTO. Workspaces stay in place (RESTRICT FK) so the
coach can still review student work for certification post-close;
students lose login access via the login gate in
`app.modules.coaching.service.guard_coaching_student_login`.

Runs hourly per celery beat schedule (wired in app/celery_app.py).

Follows the Training Sandbox precedent (`training_expiry.py`) but is
simpler — coaching has no WINDING_DOWN grace period (unlike training,
which needs 24h for in-flight orders to complete). Coaching students
are testing / practising, so a hard transition on the 30th day is
fine.
"""
import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.coaching.models import (
    CoachingSession, CoachingSessionStatus, utcnow,
)

logger = logging.getLogger(__name__)


async def _sweep_expired_sessions(db: AsyncSession) -> int:
    """Flip every ACTIVE session past its 30-day window to CLOSED_AUTO.
    Returns the number of sessions closed."""
    now = utcnow()
    sessions = (await db.execute(
        select(CoachingSession).where(
            CoachingSession.status == CoachingSessionStatus.ACTIVE.value,
        )
    )).scalars().all()

    closed = 0
    closed_sessions: list[CoachingSession] = []
    for session in sessions:
        if session.auto_close_due(now=now):
            session.status = CoachingSessionStatus.CLOSED_AUTO.value
            session.closed_at = now
            # closed_by_user_id intentionally left NULL — the audit
            # trail shows the transition wasn't user-driven.
            closed += 1
            closed_sessions.append(session)
            logger.info(
                "Coaching session %s auto-closed (started_at=%s)",
                session.id, session.started_at,
            )
    if closed:
        await db.commit()
        # Grace-start emails per student (2026-10-16, hybrid access).
        # Best-effort: don't let email failures roll back the close.
        for session in closed_sessions:
            try:
                await _dispatch_grace_start_emails(db, session)
            except Exception as e:
                logger.warning(
                    "grace-start email dispatch failed for session %s: %s",
                    session.id, e,
                )
    return closed


async def _dispatch_grace_start_emails(
    db: AsyncSession, session: CoachingSession,
) -> None:
    """Send the grace-start email to each student on an auto-closed
    session. Mirrors the dispatcher in `service.close_session` for the
    manual-close path. Kept here to avoid a cross-module tangle; the
    two call-sites are the only places sessions transition out of
    ACTIVE."""
    from app.config import settings
    from app.modules.clients.models import Client
    from app.modules.coaching.emails import send_grace_start_email
    from app.modules.coaching.models import (
        CoachingStudent, GRACE_DURATION_DAYS,
    )
    from app.modules.platform.models import User

    students = (await db.execute(
        select(CoachingStudent, User, Client)
        .join(User, User.id == CoachingStudent.user_id)
        .join(Client, Client.parent_session_id == session.id)
        .where(CoachingStudent.session_id == session.id)
    )).all()
    ref_client_name = (await db.execute(
        select(Client.full_name).where(
            Client.id == session.reference_client_id,
        )
    )).scalar_one_or_none() or "RootsTalk"
    coach_user = (await db.execute(
        select(User).where(User.id == session.coach_user_id)
    )).scalar_one_or_none()
    coach_name = (
        (coach_user.name if coach_user else None)
        or (coach_user.email if coach_user else None)
        or "rootsTALK Coach"
    )
    portal_root = (
        settings.frontend_base_url.rstrip("/")
        if settings.frontend_base_url else "https://rootstalk.in"
    )
    for cs, user_row, workspace in students:
        if not user_row.email:
            continue
        portal_url = f"{portal_root}/portal/{workspace.short_name}"
        send_grace_start_email(
            to_email=user_row.email,
            student_name=user_row.name or "Coaching Student",
            coach_name=coach_name,
            reference_client_name=ref_client_name,
            portal_url=portal_url,
            grace_days=GRACE_DURATION_DAYS,
        )


async def _run_sweep() -> int:
    async with AsyncSessionLocal() as db:
        return await _sweep_expired_sessions(db)


@celery_app.task(name="app.tasks.coaching_expiry.sweep_expired_coaching_sessions")
def sweep_expired_coaching_sessions() -> None:
    """Beat entry: runs hourly, closes ACTIVE sessions past their
    30-day mark. Idempotent — running it more often just re-scans the
    same set + closes nothing new."""
    count = asyncio.run(_run_sweep())
    logger.info("Coaching auto-close sweep closed %d session(s)", count)
