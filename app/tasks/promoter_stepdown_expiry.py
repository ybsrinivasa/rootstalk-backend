"""Auto-revoke Promoter stepdown requests older than 7 days.

When an F-P or Dealer requests to step down (2026-08-10 shipped),
the request sits as `promoter_request_status='STEPDOWN_REQUESTED'`
until the CA / a Field Manager approves it via the CA-side revoke
endpoint. That endpoint runs the actual reclaim + flag flip.

This sweep is the escape hatch: any request older than 7 days without
CA action is auto-approved so a promoter who's checked out doesn't
sit in limbo forever. Mirrors the CA-side revoke logic — reclaim any
un-consumed PromoterAllocation units, then flip `is_promoter=False` +
`is_promoter_pundit=False` + `promoter_request_status='NONE'`.

Hourly cadence keeps the cost trivial (usually zero rows). Registered
in celery_app.beat_schedule.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

STEPDOWN_AUTO_REVOKE_DAYS = 7


async def _run_sweep_with_session(db: AsyncSession, now: datetime | None = None) -> int:
    """Inner sweep — split out so tests can inject the session."""
    from app.modules.clients.models import ClientPromoter
    from app.modules.subscriptions.promoter_allocation_models import PromoterAllocation
    from app.services.promoter_pool import reclaim_from_promoter

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=STEPDOWN_AUTO_REVOKE_DAYS)

    stale = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.promoter_request_status == "STEPDOWN_REQUESTED",
            ClientPromoter.promoter_request_responded_at <= cutoff,
            ClientPromoter.is_promoter.is_(True),
        )
    )).scalars().all()

    for cp in stale:
        try:
            alloc = (await db.execute(
                select(PromoterAllocation).where(
                    PromoterAllocation.client_id == cp.client_id,
                    PromoterAllocation.promoter_user_id == cp.user_id,
                )
            )).scalar_one_or_none()
            if alloc is not None and alloc.units_balance > 0:
                await reclaim_from_promoter(
                    db,
                    client_id=cp.client_id,
                    promoter_user_id=cp.user_id,
                    units=int(alloc.units_balance),
                )
            cp.is_promoter = False
            cp.is_promoter_pundit = False
            cp.promoter_request_status = "NONE"
            cp.promoter_request_responded_at = now
            await db.commit()
            logger.info(
                "Auto-revoked promoter stepdown request cp=%s (user=%s client=%s) "
                "after %d days without CA action",
                cp.id, cp.user_id, cp.client_id, STEPDOWN_AUTO_REVOKE_DAYS,
            )
        except Exception as exc:   # noqa: BLE001
            await db.rollback()
            logger.warning(
                "Auto-revoke failed for cp=%s: %s — will retry on next sweep",
                cp.id, exc,
            )

    return len(stale)


@celery_app.task(name="app.tasks.promoter_stepdown_expiry.sweep_stepdown_requests")
def sweep_stepdown_requests():
    """Beat entry point — runs the async sweep in its own event loop."""
    import asyncio

    async def _run() -> int:
        async with AsyncSessionLocal() as db:
            return await _run_sweep_with_session(db)

    return asyncio.run(_run())
