"""Pundit Exclusivity — auto-approve Pundit end-requests after 7 days.

Hourly celery task. Flips any ACTIVE agreement whose
`pundit_end_requested_at` is older than 7 days → status
ENDED_BY_PUNDIT_REQUEST. Mirrors the 2026-08-10 promoter-stepdown
auto-revoke pattern so a ghosted CA can't lock a Pundit in
indefinitely.

The partial index `ix_pundit_exclusivity_end_requested_at` makes
this scan tiny — only rows with a pending end-request are visited.
Terminal (DECLINED / ENDED_*) rows are skipped by the status filter.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.farmpundit.models import (
    PunditExclusivityAgreement, PunditExclusivityStatus,
)

logger = logging.getLogger(__name__)


AUTO_APPROVE_DAYS = 7


async def _run_with_session(db: AsyncSession, now=None) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=AUTO_APPROVE_DAYS)
    rows = (await db.execute(
        select(PunditExclusivityAgreement).where(
            PunditExclusivityAgreement.status == PunditExclusivityStatus.ACTIVE,
            PunditExclusivityAgreement.pundit_end_requested_at.is_not(None),
            PunditExclusivityAgreement.pundit_end_requested_at <= cutoff,
        )
    )).scalars().all()
    for a in rows:
        a.status = PunditExclusivityStatus.ENDED_BY_PUNDIT_REQUEST
        a.ended_at = now
        # ended_by_user_id intentionally left NULL — the system
        # (auto-approval) ended it, not a user. Caller code that
        # resolves `ended_by_name` should treat NULL as "Auto".
    if rows:
        await db.commit()
        logger.info(
            f"Pundit exclusivity auto-approve: ended {len(rows)} "
            f"agreements past the {AUTO_APPROVE_DAYS}-day Pundit "
            f"end-request window"
        )
    return len(rows)


async def _run() -> int:
    async with AsyncSessionLocal() as db:
        return await _run_with_session(db)


@celery_app.task(
    name="app.tasks.pundit_exclusivity_expiry.auto_approve_end_requests"
)
def auto_approve_end_requests():
    """Hourly sweep. See module docstring."""
    return asyncio.run(_run())
