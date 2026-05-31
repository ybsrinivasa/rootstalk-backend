"""Orders V2 Batch 8 — timeline tandem archive sweep.

When a timeline's window closes, the items it covered should
disappear from the active advisory view AND from the active order
view in tandem (2026-05-31 narrative). The advisory side already
filters by current date; this task mirrors that behaviour on the
order side by stamping `OrderItem.archived_at = now`.

Soft-archive rather than hard-delete:
- Preserves the farmer's History view + the `order_item_events`
  audit trail. Reports still walk the lineage; History still shows
  the last-known status.
- Active surfaces filter `archived_at IS NULL` so the live order
  detail / dealer inbox / facilitator pane all stop showing
  archived rows naturally.

Rules:
- DAS timeline → end = `crop_start_date + to_value` (days after
  sowing).
- DBS timeline → end = `crop_start_date - to_value` (pre-sowing
  window — closed even earlier).
- CALENDAR → not date-anchored; we don't auto-archive. The 14-day
  order_expiry sweep covers the order-level fallback.
- Items in OrderItemStatus.REROUTED / REMOVED are already
  effectively archived; we skip them so we don't waste an event.
- IST date is the reference, matching the alerts pipeline
  (memory: feedback_ist_for_scheduled_tasks).

Runs hourly at :50 — far enough from the postpone-expiry sweep at
:45 that the per-minute event log stays readable.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.advisory.models import Timeline
from app.modules.orders.models import OrderItem, OrderItemStatus, Order
from app.modules.subscriptions.models import Subscription
from app.services.order_events import record_event

logger = logging.getLogger(__name__)


def _timeline_end_for(timeline: Timeline, sub_crop_start) -> "datetime | None":
    """Mirror of `_timeline_end_date` in orders/router.py — duplicated
    here so the task doesn't import from the router module (avoids a
    Celery-time circular import path)."""
    if not sub_crop_start:
        return None
    start = sub_crop_start.date() if hasattr(sub_crop_start, "date") else sub_crop_start
    ft = timeline.from_type.value if hasattr(timeline.from_type, "value") else str(timeline.from_type)
    if ft == "DAS":
        return start + timedelta(days=int(timeline.to_value or 0))
    if ft == "DBS":
        return start - timedelta(days=int(timeline.to_value or 0))
    return None


async def _archive_expired_timeline_items_with_session(db, now=None) -> int:
    """Inner sweep — exposed so integration tests can pass the
    testcontainer session and assert on the rows the task commits.
    """
    now = now or datetime.now(timezone.utc)
    ist_today = (now + timedelta(hours=5, minutes=30)).date()

    skip = [
        OrderItemStatus.REROUTED,
        OrderItemStatus.REMOVED,
    ]

    # Pull every still-active item with its timeline + subscription.
    # The active set is small enough at V1 scale that a per-row
    # window calculation in Python beats a SQL date-arithmetic JOIN.
    rows = (await db.execute(
        select(OrderItem, Timeline, Subscription)
        .join(Timeline, Timeline.id == OrderItem.timeline_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Subscription, Subscription.id == Order.subscription_id)
        .where(
            OrderItem.archived_at.is_(None),
            OrderItem.status.notin_(skip),
        )
    )).all()

    archived = 0
    for item, tl, sub in rows:
        window_end = _timeline_end_for(tl, sub.crop_start_date)
        if window_end is None:
            # CALENDAR timeline or unset crop_start_date — leave to
            # order-level expiry.
            continue
        if window_end >= ist_today:
            continue  # still in-window

        item.archived_at = now
        await record_event(
            db,
            lineage_id=item.lineage_id,
            event_type="TIMELINE_EXPIRED",
            actor_role="SYSTEM",
            order_id=item.order_id,
            order_item_id=item.id,
            prev_status=(item.status.value if hasattr(item.status, "value") else item.status),
            new_status=None,  # status doesn't change; archive flag is what shifts
            metadata={
                "timeline_id": tl.id,
                "timeline_end": window_end.isoformat(),
                "ist_today": ist_today.isoformat(),
            },
        )
        archived += 1

    if archived:
        await db.commit()
        logger.info(f"Orders V2: archived {archived} timeline-expired items")
    return archived


async def _archive_expired_timeline_items():
    async with AsyncSessionLocal() as db:
        return await _archive_expired_timeline_items_with_session(db)


@celery_app.task(name="app.tasks.timeline_archive.archive_expired_timeline_items")
def archive_expired_timeline_items():
    """Hourly check. See module docstring."""
    asyncio.run(_archive_expired_timeline_items())
