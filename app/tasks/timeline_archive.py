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
- CALENDAR timeline → end = the order item's creation-year Jan 1
  plus `(to_value - 1)` days (DOY-anchored window). Captures the
  intent that an order placed in year N for a DOY-band targets
  that year's occurrence; once year N's window has passed, archive
  even though the same band will re-open next year (perennial
  re-occurrences create new advisory / order activity, they don't
  resurrect last year's). Wrap-around windows (from > to) are
  V1-unsupported — mirror of `order_bundle.py:_timeline_window`.
  Pre-fix (2026-06-20): CALENDAR was skipped entirely; items
  lingered until the 14-day order-level expiry.
- Items in OrderItemStatus.REROUTED / REMOVED are already
  effectively archived; we skip them so we don't waste an event.
- IST date is the reference, matching the alerts pipeline
  (memory: feedback_ist_for_scheduled_tasks).

Runs hourly at :50 — far enough from the postpone-expiry sweep at
:45 that the per-minute event log stays readable.

2026-07-06 addendum — ghost-order sweep. After the item pass,
Orders whose every live item has been archived get flipped to
EXPIRED. Without this, an order sat on the dealer's Pending pill
(status PROCESSING) for 5-13 days until the 14-day Order.expires_at
fallback caught it — even though every underlying item's window
had already closed. User caught 5 such orders on prod 2026-07-06.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.advisory.models import Timeline
from app.modules.orders.models import (
    OrderItem, OrderItemStatus, Order, OrderStatus,
)
from app.modules.subscriptions.models import Subscription
from app.services.order_events import record_event
from sqlalchemy import func

logger = logging.getLogger(__name__)


def _timeline_end_for(
    timeline: Timeline,
    sub_crop_start,
    item: OrderItem,
    today_ist: "date",
) -> "date | None":
    """Mirror of `_timeline_end_date` in orders/router.py — duplicated
    here so the task doesn't import from the router module (avoids a
    Celery-time circular import path).

    2026-06-20 — CALENDAR branch added. Uses the order item's
    creation year as the reference year (see module docstring), so
    a multi-year perennial doesn't trip over the same DOY window
    re-opening every January.
    """
    ft = timeline.from_type.value if hasattr(timeline.from_type, "value") else str(timeline.from_type)
    if ft in ("DAS", "DBS"):
        if not sub_crop_start:
            return None
        start = sub_crop_start.date() if hasattr(sub_crop_start, "date") else sub_crop_start
        if ft == "DAS":
            return start + timedelta(days=int(timeline.to_value or 0))
        # DBS — BL-17: DBS to=0 closes day BEFORE sowing → clamp upper bound.
        return start - timedelta(days=max(int(timeline.to_value or 0), 1))
    if ft == "CALENDAR":
        if not timeline.to_value:
            return None
        # Wrap-around windows (Dec → Jan, from_value > to_value) are
        # V1-unsupported per order_bundle.py:_timeline_window.
        if timeline.from_value is not None and int(timeline.from_value) > int(timeline.to_value):
            return None
        ref_year = (
            item.created_at.year
            if getattr(item, "created_at", None) is not None
            else today_ist.year
        )
        year_start = date(ref_year, 1, 1)
        return year_start + timedelta(days=int(timeline.to_value) - 1)
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
        window_end = _timeline_end_for(tl, sub.crop_start_date, item, ist_today)
        if window_end is None:
            # Reasons: DAS/DBS with no crop_start_date, CALENDAR
            # wrap-around, CALENDAR with no to_value. Order-level
            # 14-day expiry remains the fallback for these.
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

    # 2026-07-06 — Ghost-order sweep. When every live item on an
    # order is archived (their timeline windows all passed), the
    # order shell keeps showing on the dealer's Pending pill + the
    # farmer's active orders list until the 14-day Order.expires_at
    # fallback kicks in. That's 5-13 days of "why is this still here?"
    # per user 2026-07-06. Flip the order to EXPIRED right after the
    # last item archives so it disappears from active surfaces in
    # tandem. Runs same task, same hour so the dealer + farmer see
    # the removal together with the advisory hiding.
    #
    # Predicate: (a) order is non-terminal, (b) at least one live-non-
    # rerouted-non-removed item exists, (c) every such item has
    # archived_at set. Live-status check is critical — an order with
    # no items at all shouldn't get swept here (that's what the 14-day
    # Order.expires_at is for). REROUTED / REMOVED items are treated
    # as effectively dead — the dealer feed excludes them already.
    live_item_statuses = [
        s for s in OrderItemStatus
        if s not in (OrderItemStatus.REROUTED, OrderItemStatus.REMOVED)
    ]
    ghost_order_ids = (await db.execute(
        select(Order.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(
            Order.status.notin_([
                OrderStatus.COMPLETED,
                OrderStatus.CANCELLED,
                OrderStatus.EXPIRED,
            ]),
            OrderItem.status.in_(live_item_statuses),
        )
        .group_by(Order.id)
        # count(*) == count(archived_at) means every live item has
        # been archived — Postgres COUNT(col) skips NULLs.
        .having(func.count() == func.count(OrderItem.archived_at))
    )).scalars().all()

    ghost_orders_flipped = 0
    if ghost_order_ids:
        for order in (await db.execute(
            select(Order).where(Order.id.in_(ghost_order_ids))
        )).scalars().all():
            prev_status = (
                order.status.value if hasattr(order.status, "value")
                else str(order.status)
            )
            order.status = OrderStatus.EXPIRED
            await record_event(
                db,
                # 2026-07-06 — Order has no `lineage_id` column
                # (only OrderItem + SeedOrderFull do). Per the
                # record_event docstring, pass the order's own id
                # when there's no item context — order ids and item
                # lineage ids share the UUID namespace.
                lineage_id=order.id,
                event_type="ORDER_TIMELINE_EXPIRED",
                actor_role="SYSTEM",
                order_id=order.id,
                prev_status=prev_status,
                new_status=OrderStatus.EXPIRED.value,
                metadata={
                    "reason": "all_live_items_archived",
                    "ist_today": ist_today.isoformat(),
                },
            )
            ghost_orders_flipped += 1
        await db.commit()
        logger.info(
            f"Orders V2: flipped {ghost_orders_flipped} ghost orders to EXPIRED"
        )

    return archived


async def _archive_expired_timeline_items():
    async with AsyncSessionLocal() as db:
        return await _archive_expired_timeline_items_with_session(db)


@celery_app.task(name="app.tasks.timeline_archive.archive_expired_timeline_items")
def archive_expired_timeline_items():
    """Hourly check. See module docstring."""
    asyncio.run(_archive_expired_timeline_items())
