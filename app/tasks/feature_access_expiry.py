"""Daily sweep that logs feature-access transitions for audit.

The expiry check itself is a derived computation (`access_until < now`
on the latest row per client+feature), so this sweep does NOT flip
any status column. Its purpose is:

1. Emit one `logger.info` line per feature that silently expired in
   the last 24 hours — gives us a human-readable breadcrumb in the
   worker journal even when nobody's looking at the SA dashboard.
2. Future hook point for renewal-reminder emails to the SA (not
   shipped in Phase 1; the dashboard panel + Expiring-Soon banner in
   CP cover the UX for now).

Scheduled at 00:15 UTC daily (05:45 IST) — off the hot minute-zero
slots used by hourly sweeps.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.celery_app import celery_app
from app.database import AsyncSessionLocal


logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.feature_access_expiry.sweep_expiries")
def sweep_expiries() -> dict:
    """Celery entrypoint. Thin wrapper around the async sweep."""
    return asyncio.run(_run_sweep())


async def _run_sweep() -> dict:
    """Walk the latest row per (client, feature). For each row whose
    `access_until` crossed into the past within the last 24 hours,
    log a line. Returns a small summary dict for the Celery result.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=24)

    # Latest-row-per-(client, feature) via a window function — same
    # shape the SA overview endpoint uses.
    latest_sql = text("""
        WITH ranked AS (
          SELECT
            cfa.*,
            ROW_NUMBER() OVER (
              PARTITION BY cfa.client_id, cfa.feature
              ORDER BY cfa.created_at DESC
            ) AS rn
          FROM client_feature_access cfa
        )
        SELECT r.client_id, r.feature, r.access_until, c.display_name
        FROM ranked r
        JOIN clients c ON c.id = r.client_id
        WHERE r.rn = 1
          AND r.access_until <= :now
          AND r.access_until > :window_start
    """)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            latest_sql, {"now": now, "window_start": window_start},
        )).all()

    expired_count = 0
    for client_id, feature, access_until, display_name in rows:
        logger.info(
            "feature_access_expired: client=%s (%s) feature=%s "
            "access_until=%s",
            display_name, client_id, feature,
            access_until.isoformat() if access_until else None,
        )
        expired_count += 1

    return {
        "expired_in_last_24h": expired_count,
        "swept_at": now.isoformat(),
    }
