"""Campaign expiry sweep — flip ACTIVE → EXPIRED past expires_at.

Runs hourly. Also removes expired campaigns from the farmer's inbox
view by virtue of the inbox read-side filter on status, so no
per-recipient write is needed at expiry — just the campaign-level
status flip.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.messaging.models import Campaign, CampaignStatus


logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.campaign_expiry.sweep_campaign_expiry")
def sweep_campaign_expiry() -> dict:
    return asyncio.run(_run())


async def _run() -> dict:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        to_expire = (await db.execute(
            select(Campaign.id).where(
                Campaign.status == CampaignStatus.ACTIVE.value,
                Campaign.expires_at.isnot(None),
                Campaign.expires_at <= now,
            )
        )).scalars().all()
        if not to_expire:
            return {"expired": 0}
        await db.execute(
            update(Campaign)
            .where(Campaign.id.in_(to_expire))
            .values(status=CampaignStatus.EXPIRED.value, updated_at=now)
        )
        await db.commit()
        for cid in to_expire:
            logger.info("campaign_expired: id=%s", cid)
        return {"expired": len(to_expire)}
