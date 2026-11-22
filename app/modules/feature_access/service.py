"""Feature Access — read-side helpers used by feature-gating code.

The write side (SA activating/extending/turning-off) lives in
`router.py` and is SA-gated. These helpers are open — any code path
that needs to know "does this client have this feature right now?"
calls `client_has_feature(...)`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.feature_access.models import ClientFeatureAccess


async def latest_feature_row(
    db: AsyncSession, client_id: str, feature: str,
) -> Optional[ClientFeatureAccess]:
    """Return the most recent row for (client, feature), or None if
    the feature has never been activated for this client. The latest
    row IS the current state in the append-only model.
    """
    stmt = (
        select(ClientFeatureAccess)
        .where(
            ClientFeatureAccess.client_id == client_id,
            ClientFeatureAccess.feature == feature,
        )
        .order_by(ClientFeatureAccess.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def client_has_feature(
    db: AsyncSession, client_id: str, feature: str,
    now: Optional[datetime] = None,
) -> bool:
    """True when the client's latest row for `feature` has
    `access_until > now()`. False when never activated, expired,
    or turned off.

    `now` injectable for testing; defaults to real UTC now.
    """
    row = await latest_feature_row(db, client_id, feature)
    if row is None:
        return False
    n = now if now is not None else datetime.now(timezone.utc)
    return row.access_until > n


def row_is_active(row: ClientFeatureAccess, now: Optional[datetime] = None) -> bool:
    """Pure computation — handy when the row is already loaded."""
    n = now if now is not None else datetime.now(timezone.utc)
    return row.access_until > n


def row_days_until_expiry(
    row: ClientFeatureAccess, now: Optional[datetime] = None,
) -> int:
    """Negative = already expired, positive = days remaining."""
    n = now if now is not None else datetime.now(timezone.utc)
    delta = row.access_until - n
    return int(delta.total_seconds() // 86400)
