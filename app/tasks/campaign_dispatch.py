"""Campaign dispatch — push notifications for a sent campaign.

Called by `app/modules/messaging/router.py::send_campaign` after it
flips the campaign to ACTIVE and writes the CampaignRecipient rows.

Design:
- Rate-limited: dispatch in batches with a small sleep between each
  batch so a 50k-farmer campaign doesn't slam FCM in one burst.
- FCM is best-effort (see spec §3.4 — 52% coverage prod baseline).
  The per-recipient `pushed_at` is stamped on every ATTEMPTED push
  (even if FCM says no token / fails), so delivery analytics
  reflects "we tried" not "it arrived". A separate `delivered`
  concept isn't knowable from FCM for data-only payloads.
- Idempotent: if the task runs twice (retry / crash), recipients
  already stamped with `pushed_at` are skipped.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.messaging.models import (
    Campaign, CampaignRecipient, CampaignStatus,
)
from app.modules.platform.models import User
from app.services.fcm_service import send_fcm


logger = logging.getLogger(__name__)


# Rate-limit knobs. 500 recipients per batch, 2-second sleep between
# batches → ~15k/minute ceiling. Leaves FCM breathing room while
# still clearing a prod-scale campaign in a few minutes.
BATCH_SIZE = 500
BATCH_SLEEP_SECONDS = 2.0


@celery_app.task(name="app.tasks.campaign_dispatch.dispatch_campaign")
def dispatch_campaign(campaign_id: str, test_only: bool = False) -> dict:
    """Celery entrypoint. test_only=True dispatches only to recipients
    marked is_test=True (small set from a test-send)."""
    return asyncio.run(_run(campaign_id, test_only))


async def _run(campaign_id: str, test_only: bool) -> dict:
    async with AsyncSessionLocal() as db:
        c = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if c is None:
            logger.warning("dispatch_campaign: campaign %s not found", campaign_id)
            return {"error": "campaign_not_found"}
        # Guard: test-only can run against any status (DRAFT, usually).
        # Real-send only runs against ACTIVE. Deferred SCHEDULED → ACTIVE
        # flip is Phase 2D's job.
        if not test_only and c.status != CampaignStatus.ACTIVE.value:
            logger.warning(
                "dispatch_campaign: campaign %s status=%s, not dispatching",
                campaign_id, c.status,
            )
            return {"error": "campaign_not_active"}

        # Pick pending recipients.
        stmt = select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.pushed_at.is_(None),
        )
        if test_only:
            stmt = stmt.where(CampaignRecipient.is_test.is_(True))
        else:
            stmt = stmt.where(CampaignRecipient.is_test.is_(False))
        recipients = (await db.execute(stmt)).scalars().all()
        if not recipients:
            return {"attempted": 0, "pushed": 0}

        # Load client display name for the push title line.
        from app.modules.clients.models import Client
        client = (await db.execute(
            select(Client).where(Client.id == c.client_id)
        )).scalar_one_or_none()
        company_name = (client.display_name if client else "RootsTalk") or "RootsTalk"

        # Hydrate FCM tokens for the recipients we're about to push.
        uids = [r.user_id for r in recipients if r.user_id]
        token_by_uid: dict[str, str | None] = {}
        if uids:
            user_rows = (await db.execute(
                select(User.id, User.fcm_token).where(User.id.in_(uids))
            )).all()
            token_by_uid = {uid: tok for uid, tok in user_rows}

        attempted = 0
        pushed_ok = 0
        now = datetime.now(timezone.utc)

        # Iterate in batches for rate-limiting.
        for i in range(0, len(recipients), BATCH_SIZE):
            batch = recipients[i:i + BATCH_SIZE]
            for r in batch:
                attempted += 1
                r.pushed_at = now
                token = token_by_uid.get(r.user_id or "")
                if not token:
                    # No token — PWA badge + top-banner-on-open cover
                    # discovery. Still stamp pushed_at so "we tried"
                    # is recorded.
                    continue
                try:
                    ok = await send_fcm(
                        token=token,
                        title=f"New message from {company_name}",
                        body=c.title,
                        data={
                            "type": "campaign",
                            "campaign_id": c.id,
                            "campaign_type": c.campaign_type,
                        },
                    )
                except Exception as exc:
                    ok = False
                    logger.exception("FCM push failed: %s", exc)
                if ok:
                    pushed_ok += 1

            # Commit per batch so partial progress is durable.
            await db.commit()
            # Bump the campaign-level counter.
            await db.execute(
                update(Campaign)
                .where(Campaign.id == campaign_id)
                .values(pushed_count=Campaign.pushed_count + len(batch))
            )
            await db.commit()

            if i + BATCH_SIZE < len(recipients):
                await asyncio.sleep(BATCH_SLEEP_SECONDS)

        return {
            "campaign_id": campaign_id,
            "attempted": attempted,
            "pushed": pushed_ok,
            "test_only": test_only,
        }
