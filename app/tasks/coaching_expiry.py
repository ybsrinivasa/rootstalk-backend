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
    for session in sessions:
        if session.auto_close_due(now=now):
            session.status = CoachingSessionStatus.CLOSED_AUTO.value
            session.closed_at = now
            # closed_by_user_id intentionally left NULL — the audit
            # trail shows the transition wasn't user-driven.
            closed += 1
            logger.info(
                "Coaching session %s auto-closed (started_at=%s)",
                session.id, session.started_at,
            )
    if closed:
        await db.commit()
    return closed


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
