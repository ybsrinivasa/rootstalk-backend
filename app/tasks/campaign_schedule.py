"""Campaign scheduling sweep — flip SCHEDULED → ACTIVE at send time.

Runs every minute (celery beat). For each SCHEDULED campaign whose
scheduled_at has passed, snapshot the audience, flip status to
ACTIVE, write the send audit row, and fire the dispatch task.

Audience snapshotting happens HERE, not at /send time, so SCHEDULED
campaigns capture the current recipient set at the moment they go
out — a CA who schedules "all Chilli farmers in Bagalkot" for Friday
morning hits the set that exists Friday morning, not the set that
existed when they drafted on Monday. Matches the spec §4.4 rule
"audience snapshot at send time, not draft time" extended to apply
to scheduled sends too.

Per-campaign work is wrapped in try/except so one bad row doesn't
strand the whole batch.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.messaging.models import Campaign, CampaignStatus


logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.campaign_schedule.activate_scheduled_campaigns")
def activate_scheduled_campaigns() -> dict:
    return asyncio.run(_run())


async def _run() -> dict:
    now = datetime.now(timezone.utc)
    activated = 0
    errored: list[str] = []

    async with AsyncSessionLocal() as db:
        due = (await db.execute(
            select(Campaign).where(
                Campaign.status == CampaignStatus.SCHEDULED.value,
                Campaign.scheduled_at.isnot(None),
                Campaign.scheduled_at <= now,
            )
        )).scalars().all()
        if not due:
            return {"activated": 0}

        # Delayed imports — these pull the full messaging service chain
        # and we only want them in the sweep process, not at module import.
        from app.modules.messaging.service import compute_audience_user_ids
        from app.modules.messaging.router import (
            _snapshot_recipients, _write_send_audit,
        )
        from app.modules.platform.models import User

        for camp in due:
            try:
                # The system acts as the sender. We don't have a User
                # context in a beat task, so audit row records with
                # sender_user_id = created_by (the CA who scheduled it).
                sender = None
                if camp.created_by_user_id:
                    sender = (await db.execute(
                        select(User).where(User.id == camp.created_by_user_id)
                    )).scalar_one_or_none()
                user_ids = await compute_audience_user_ids(
                    db, camp.client_id, camp.recipient_role,
                    camp.audience_filters or {},
                )
                if not user_ids:
                    # Empty audience at activation time — skip the
                    # campaign (stays SCHEDULED, won't auto-resolve;
                    # CA can cancel or adjust filters and re-schedule).
                    logger.warning(
                        "campaign_schedule: empty audience at activation "
                        "for campaign=%s client=%s",
                        camp.id, camp.client_id,
                    )
                    continue
                snapped = await _snapshot_recipients(
                    db, camp, user_ids, is_test=False,
                )
                camp.status = CampaignStatus.ACTIVE.value
                camp.sent_at = now
                camp.recipient_count = snapped
                camp.updated_at = now
                if sender is not None:
                    await _write_send_audit(db, camp, sender, snapped, is_test=False)
                await db.commit()

                from app.tasks.campaign_dispatch import dispatch_campaign
                dispatch_campaign.delay(camp.id, test_only=False)
                activated += 1
                logger.info("campaign_scheduled_activated: id=%s recipients=%s", camp.id, snapped)
            except Exception as exc:
                await db.rollback()
                errored.append(camp.id)
                logger.exception(
                    "campaign_schedule: activation failed for campaign=%s: %s",
                    camp.id, exc,
                )

    return {"activated": activated, "errored": errored, "swept_at": now.isoformat()}
