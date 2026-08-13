"""Orders V2 Batch 7 — postpone-expiry sweep.

When a dealer postpones an item, they pick a `days` value and we
stamp `postponed_until = today + days`. If the dealer doesn't
action the item by that timestamp, the farmer shouldn't be left
hanging — this sweep flips the item to NOT_AVAILABLE so it bubbles
up as a Returned-and-needs-rerouting card in the farmer's PWA.

Why auto-flip rather than expose a "dealer's clock has run out"
status: it keeps the item-status alphabet small and matches the
2026-05-31 narrative — "Postponed items can also be cancelled and
forwarded" implies the farmer's path forward for an unfulfilled
postpone is the *same* path as for a dealer-flagged NOT_AVAILABLE.

Runs hourly (the dealer's postpone resolution is in days, so a
per-hour sweep is plenty timely). Each flipped row writes a
`POSTPONE_EXPIRED` event keyed on the item's lineage_id so reports
can tell the difference between "dealer marked Not Available" and
"clock ran out on a postpone".
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.orders.models import (
    Order, OrderItem, OrderItemEvent, OrderItemStatus,
)
from app.modules.platform.models import User
from app.modules.seed_mgmt.models import SeedOrderFull, SeedOrderStatus
from app.services.fcm_service import send_fcm
from app.services.order_events import record_event

_ACTIVE_WITH_DEALER = (
    OrderItemStatus.PENDING,
    OrderItemStatus.AVAILABLE,
    OrderItemStatus.POSTPONED,
    OrderItemStatus.SENT_FOR_APPROVAL,
)

logger = logging.getLogger(__name__)

# 2026-07-16 — Push the farmer that a postponed item's clock ran
# out, so they know to visit the Returned & Rerouting list before
# the crop stage passes.
POSTPONE_EXPIRED_FCM_TITLE = "A postponed item is now unavailable"
POSTPONE_EXPIRED_FCM_BODY = (
    "The dealer didn't fulfill a postponed item in time. Reroute it "
    "in RootsTalk to keep the item moving."
)


async def _sweep_expired_postpones_with_session(db, now=None) -> int:
    """Inner sweep — split out so integration tests can pass the
    testcontainer session and assert on the rows the task commits.

    Handles BOTH OrderItem and SeedOrderFull postpones (Batch 12).
    Same rule on both: postponed_until <= now → flip to
    NOT_AVAILABLE + write POSTPONE_EXPIRED event keyed on lineage.
    """
    now = now or datetime.now(timezone.utc)
    flipped = 0

    # ── OrderItem (pesticide / fertiliser) ────────────────────────
    items = (await db.execute(
        select(OrderItem).where(
            OrderItem.status == OrderItemStatus.POSTPONED,
            OrderItem.postponed_until.isnot(None),
            OrderItem.postponed_until <= now,
        )
    )).scalars().all()

    for item in items:
        prev_until = item.postponed_until
        item.status = OrderItemStatus.NOT_AVAILABLE
        await record_event(
            db,
            lineage_id=item.lineage_id,
            event_type="POSTPONE_EXPIRED",
            actor_role="SYSTEM",
            order_id=item.order_id,
            order_item_id=item.id,
            prev_status=OrderItemStatus.POSTPONED.value,
            new_status=OrderItemStatus.NOT_AVAILABLE.value,
            metadata={
                "postponed_until": prev_until.isoformat() if prev_until else None,
            },
        )
        flipped += 1

    # ── SeedOrderFull (Batch 12 — seeds share the lifecycle) ──────
    seed_rows = (await db.execute(
        select(SeedOrderFull).where(
            SeedOrderFull.status == SeedOrderStatus.POSTPONED,
            SeedOrderFull.postponed_until.isnot(None),
            SeedOrderFull.postponed_until <= now,
        )
    )).scalars().all()

    for so in seed_rows:
        prev_until = so.postponed_until
        so.status = SeedOrderStatus.NOT_AVAILABLE
        await record_event(
            db,
            lineage_id=so.lineage_id,
            event_type="POSTPONE_EXPIRED",
            actor_role="SYSTEM",
            seed_order_id=so.id,
            prev_status=SeedOrderStatus.POSTPONED.value,
            new_status=SeedOrderStatus.NOT_AVAILABLE.value,
            metadata={
                "postponed_until": prev_until.isoformat() if prev_until else None,
            },
        )
        flipped += 1

    if flipped:
        await db.commit()
        logger.info(f"Orders V2: flipped {flipped} expired postpones to NOT_AVAILABLE")
        # 2026-08-13 — U-turn: only push the farmer when THIS expiry
        # actually released the wrapper (order transitioned to
        # quiescence — no PENDING / AVAILABLE / POSTPONED / SFA items
        # left). If other postpones are still holding the batch, the
        # farmer wouldn't see anything change yet, so the push would
        # mislead. Same logic for seed orders: seed order flipping to
        # NOT_AVAILABLE IS quiescence (single-item lifecycle).
        order_ids = list({i.order_id for i in items}) if items else []
        released_order_farmer_ids: list[str] = []
        if order_ids:
            orders_by_id = {
                o.id: o for o in (await db.execute(
                    select(Order).where(Order.id.in_(order_ids))
                )).scalars().all()
            }
            remaining_active = (await db.execute(
                select(OrderItem.order_id).where(
                    OrderItem.order_id.in_(order_ids),
                    OrderItem.status.in_(_ACTIVE_WITH_DEALER),
                    OrderItem.archived_at.is_(None),
                )
            )).scalars().all()
            still_holding = set(remaining_active)
            for oid in order_ids:
                if oid in still_holding:
                    continue
                o = orders_by_id.get(oid)
                if o and o.farmer_user_id:
                    released_order_farmer_ids.append(o.farmer_user_id)
        seed_farmer_ids = list({so.farmer_user_id for so in seed_rows}) if seed_rows else []
        farmer_ids = list({*released_order_farmer_ids, *seed_farmer_ids})
        if farmer_ids:
            farmers = (await db.execute(
                select(User).where(User.id.in_(farmer_ids))
            )).scalars().all()
            for f in farmers:
                if not f.fcm_token:
                    continue
                try:
                    await send_fcm(
                        token=f.fcm_token,
                        title=POSTPONE_EXPIRED_FCM_TITLE,
                        body=POSTPONE_EXPIRED_FCM_BODY,
                        data={"type": "POSTPONE_EXPIRED"},
                    )
                except Exception as e:
                    logger.error(f"FCM send raised for farmer {f.id}: {e}")
    return flipped


async def _sweep_expired_postpones():
    async with AsyncSessionLocal() as db:
        return await _sweep_expired_postpones_with_session(db)


@celery_app.task(name="app.tasks.postpone_expiry.sweep_expired_postpones")
def sweep_expired_postpones():
    """Hourly check. See module docstring."""
    asyncio.run(_sweep_expired_postpones())


# 2026-08-13 — U-turn model: dealer gets a 24h heads-up before a
# postpone auto-flips so they can either resolve it (mark available
# with brand+volume, or mark N/A) or accept the auto-flip. Runs
# hourly, so use a 1-hour window centred on 24h-from-now — each
# item's postponed_until falls in exactly ONE hourly sweep window,
# guaranteeing exactly one FCM per postpone.
POSTPONE_WARNING_FCM_TITLE = "A postponed item expires in 24 hours"
POSTPONE_WARNING_FCM_BODY = (
    "One or more items you postponed are about to auto-return to the "
    "farmer. Open RootsTalk to resolve or accept the auto-return."
)


async def _sweep_postpone_warnings_with_session(db, now=None) -> int:
    now = now or datetime.now(timezone.utc)
    lower = now + timedelta(hours=23, minutes=30)
    upper = now + timedelta(hours=24, minutes=30)

    items = (await db.execute(
        select(OrderItem).where(
            OrderItem.status == OrderItemStatus.POSTPONED,
            OrderItem.postponed_until.isnot(None),
            OrderItem.postponed_until > lower,
            OrderItem.postponed_until <= upper,
        )
    )).scalars().all()

    seed_rows = (await db.execute(
        select(SeedOrderFull).where(
            SeedOrderFull.status == SeedOrderStatus.POSTPONED,
            SeedOrderFull.postponed_until.isnot(None),
            SeedOrderFull.postponed_until > lower,
            SeedOrderFull.postponed_until <= upper,
        )
    )).scalars().all()

    if not items and not seed_rows:
        return 0

    dealer_ids: set[str] = set()
    if items:
        order_ids = list({i.order_id for i in items})
        orders = (await db.execute(
            select(Order).where(Order.id.in_(order_ids))
        )).scalars().all()
        for o in orders:
            if o.dealer_user_id:
                dealer_ids.add(o.dealer_user_id)
    for so in seed_rows:
        if so.dealer_user_id:
            dealer_ids.add(so.dealer_user_id)

    if not dealer_ids:
        return 0

    dealers = (await db.execute(
        select(User).where(User.id.in_(list(dealer_ids)))
    )).scalars().all()
    pushed = 0
    for d in dealers:
        if not d.fcm_token:
            continue
        try:
            await send_fcm(
                token=d.fcm_token,
                title=POSTPONE_WARNING_FCM_TITLE,
                body=POSTPONE_WARNING_FCM_BODY,
                data={"type": "POSTPONE_WARNING_24H"},
            )
            pushed += 1
        except Exception as e:
            logger.error(f"FCM warning send raised for dealer {d.id}: {e}")
    logger.info(f"Postpone H-24: pushed {pushed} dealer warnings")
    return pushed


async def _sweep_postpone_warnings():
    async with AsyncSessionLocal() as db:
        return await _sweep_postpone_warnings_with_session(db)


@celery_app.task(name="app.tasks.postpone_expiry.sweep_postpone_warnings")
def sweep_postpone_warnings():
    """Hourly H-24 warning sweep. See function docstring."""
    asyncio.run(_sweep_postpone_warnings())
