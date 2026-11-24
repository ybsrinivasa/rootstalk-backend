"""Touchpoint dispatch — FCM push for newly-fired recipients.

Enqueued by `touchpoint_firing.sweep_touchpoint_firings` when it
writes new rows. Also safe to run standalone — walks every
TouchpointRecipient with `pushed_at IS NULL` across all
touchpoints, so a missed enqueue (crash, Celery restart) self-
heals on the next run.

Mirror of `campaign_dispatch.dispatch_campaign`:
- Rate-limited: 500/batch, 2s between batches (= ~15k/min ceiling).
- FCM best-effort (52% prod coverage per 2026-11-22 baseline).
- pushed_at stamped on every attempt (even missing-token) so
  delivery analytics reflects "we tried" not "it arrived".
- Idempotent: already-stamped recipients are filtered out.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.clients.models import Client
from app.modules.platform.models import User
from app.modules.touchpoints.models import Touchpoint, TouchpointRecipient
from app.services.fcm_service import send_fcm


logger = logging.getLogger(__name__)

BATCH_SIZE = 500
BATCH_SLEEP_SECONDS = 2.0


@celery_app.task(name="app.tasks.touchpoint_dispatch.dispatch_touchpoint_firings")
def dispatch_touchpoint_firings() -> dict:
    return asyncio.run(_run())


async def _run() -> dict:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        # Pull all un-pushed recipients + their touchpoint for the
        # push title/body + their client for the "New message from
        # {Company}" line.
        rows = (await db.execute(
            select(TouchpointRecipient, Touchpoint, Client)
            .join(Touchpoint, Touchpoint.id == TouchpointRecipient.touchpoint_id)
            .join(Client, Client.id == Touchpoint.client_id)
            .where(
                TouchpointRecipient.pushed_at.is_(None),
                TouchpointRecipient.expires_at > now,
            )
        )).all()
        if not rows:
            return {"attempted": 0, "pushed": 0}

        # Pre-fetch FCM tokens in one pass.
        uids = [r.user_id for r, _, _ in rows if r.user_id]
        token_by_uid: dict[str, str | None] = {}
        if uids:
            user_rows = (await db.execute(
                select(User.id, User.fcm_token).where(User.id.in_(uids))
            )).all()
            token_by_uid = {uid: tok for uid, tok in user_rows}

        attempted = 0
        pushed_ok = 0

        # Process in batches so progress is durable + rate-limited.
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            for recipient, touchpoint, client in batch:
                attempted += 1
                recipient.pushed_at = now
                token = token_by_uid.get(recipient.user_id or "")
                if not token:
                    # No token — PWA badge + top-banner-on-open cover
                    # discovery. Still stamp pushed_at.
                    continue
                try:
                    company_name = client.display_name or "RootsTalk"
                    ok = await send_fcm(
                        token=token,
                        title=f"New message from {company_name}",
                        body=touchpoint.title,
                        data={
                            "type": "touchpoint",
                            "touchpoint_id": touchpoint.id,
                            "recipient_id": recipient.id,
                            "fire_at": recipient.fire_at,
                        },
                    )
                except Exception as exc:
                    ok = False
                    logger.exception("FCM push failed: %s", exc)
                if ok:
                    pushed_ok += 1

            # Commit per-batch (makes pushed_at durable), then sleep.
            await db.commit()
            if i + BATCH_SIZE < len(rows):
                await asyncio.sleep(BATCH_SLEEP_SECONDS)

        return {
            "attempted": attempted,
            "pushed": pushed_ok,
            "swept_at": now.isoformat(),
        }
