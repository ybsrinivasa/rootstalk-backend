"""BL-10: Daily order expiry — mark stale unprocessed orders as EXPIRED."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, or_, and_
from app.database import AsyncSessionLocal
from app.modules.orders.models import (
    Order, OrderStatus, OrderItem, OrderItemStatus, PackingList,
)
from app.modules.platform.models import User
from app.services.fcm_service import send_fcm
from app.celery_app import celery_app

logger = logging.getLogger(__name__)

ORDER_EXPIRY_DAYS = 14

# 2026-08-16 — Grace period post-timeline-close, for Pickup ONLY.
# Any item that's APPROVED + Final Confirmed + not-yet-received counts
# as "in Pickup grace" — that's a real physical commitment the dealer
# has packed and the farmer's coming to collect; 7 days past date_to
# is the tolerance. Every other kind of stuck item (awaiting Final
# Confirmation, POSTPONED, PENDING, AVAILABLE, SFA) gets ZERO grace —
# the advisory is over, the practice is agronomically stale, don't
# nag the farmer to act on it.
PICKUP_GRACE_DAYS = 7

# 2026-07-16 — Push the farmer that their order timed out. Without this
# the DRAFT quietly disappears from their Manage tab and the farmer
# only discovers the loss on their next visit.
EXPIRY_FCM_TITLE = "Your order has expired"
EXPIRY_FCM_BODY = (
    "The timeline for this order has passed. "
    "Re-route the items in RootsTalk if you still need them."
)


@celery_app.task(name="app.tasks.order_expiry.expire_stale_orders")
def expire_stale_orders():
    asyncio.run(_run())


async def _run():
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        today = now.date()

        # 2026-08-16 — Two independent expiry criteria, applied together:
        #
        # A) Legacy expires_at deadline (pre-Phase 2). Kept for
        #    non-terminal non-completed orders that never got dealer
        #    action within their expires_at window.
        # B) NEW: timeline_close = order.date_to has passed. This is
        #    the user's rule: "there shouldn't be any grace period"
        #    (paraphrased 2026-08-16) — the advisory window has closed,
        #    stuck items shouldn't linger on the farmer's Routed pill.
        #    ONE exception: if the order has APPROVED + Final Confirmed
        #    items awaiting physical pickup, add PICKUP_GRACE_DAYS (7d)
        #    so the farmer can still receive what the dealer packed.
        #    See _has_pickup_in_grace().
        result = await db.execute(
            select(Order).where(
                Order.status.notin_([
                    OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                    OrderStatus.PURCHASED, OrderStatus.REROUTED,
                ]),
                or_(
                    and_(
                        Order.expires_at.isnot(None),
                        Order.expires_at < now,
                        Order.status != OrderStatus.COMPLETED,
                    ),
                    Order.date_to.isnot(None),
                ),
            )
        )
        candidates = result.scalars().all()

        expired: list[Order] = []
        for order in candidates:
            # Criterion A already matched via SQL; if the row is here
            # solely because of criterion B's date_to.isnot(None), we
            # still need to gate on "past date_to" + no-pickup-grace.
            expires_at_hit = (
                order.expires_at is not None
                and order.expires_at < now
                and order.status != OrderStatus.COMPLETED
            )
            timeline_hit = False
            if order.date_to is not None and order.date_to.date() < today:
                if not await _has_pickup_in_grace(db, order, today):
                    timeline_hit = True
            if expires_at_hit or timeline_hit:
                order.status = OrderStatus.EXPIRED
                expired.append(order)
                logger.info(
                    f"Expired order {order.id} "
                    f"(expires_at={expires_at_hit}, timeline={timeline_hit})"
                )
        if expired:
            await db.commit()
            logger.info(f"Expired {len(expired)} stale orders")
            # Push each affected farmer. Loop is small (rare batch) so
            # sequential is fine; fire-and-forget so one bad token
            # doesn't stall the sweep.
            farmer_ids = list({o.farmer_user_id for o in expired})
            farmers = (await db.execute(
                select(User).where(User.id.in_(farmer_ids))
            )).scalars().all()
            farmers_by_id = {f.id: f for f in farmers}
            for order in expired:
                farmer = farmers_by_id.get(order.farmer_user_id)
                if not farmer or not farmer.fcm_token:
                    continue
                try:
                    ref = order.reference_number or ""
                    body = (
                        f"Order {ref} has expired after 14 days without dealer action. "
                        "Re-route the items in RootsTalk to keep them moving."
                        if ref else EXPIRY_FCM_BODY
                    )
                    await send_fcm(
                        token=farmer.fcm_token,
                        title=EXPIRY_FCM_TITLE,
                        body=body,
                        data={
                            "type": "ORDER_EXPIRED",
                            "order_id": order.id,
                            "click_action": f"/crop-detail/{order.subscription_id}/orders",
                        },
                    )
                except Exception as e:
                    logger.error(
                        f"FCM send raised unexpectedly for farmer {farmer.id}: {e}"
                    )


async def _has_pickup_in_grace(db, order: Order, today) -> bool:
    """Returns True iff the order has any APPROVED + Final Confirmed
    item that the farmer hasn't received yet AND we're still within
    PICKUP_GRACE_DAYS of date_to. That's the ONLY case where a stuck
    order legitimately extends past date_to — the dealer packed real
    goods, the farmer's coming to collect.

    Awaiting-Final-Confirmation items get NO grace (per user rule
    2026-08-16: 'there shouldn't be any grace period' beyond the
    pickup exception — the advisory has already lapsed).
    """
    grace_cutoff = order.date_to.date() + timedelta(days=PICKUP_GRACE_DAYS)
    if today > grace_cutoff:
        return False
    row = (await db.execute(
        select(OrderItem, PackingList)
        .outerjoin(PackingList, PackingList.order_id == OrderItem.order_id)
        .where(
            OrderItem.order_id == order.id,
            OrderItem.status == OrderItemStatus.APPROVED,
            OrderItem.final_confirmed_at.isnot(None),
            OrderItem.archived_at.is_(None),
        )
        .limit(1)
    )).first()
    if not row:
        return False
    _item, pl = row
    # Received already? No grace needed for this order's Pickup path.
    if pl is not None and pl.farmer_received_at is not None:
        return False
    return True
