"""Training Sandbox — hourly expiry sweep.

Commit F of the 2026-07-24 build plan. Two transitions:

1. ACTIVE → WINDING_DOWN when `training_ends_at` <= now:
   push everyone involved (CA of parent client + every farmer with
   a training subscription) with a "[Training] Session ended — 24
   hours to wrap up" FCM. In-flight orders/queries can still
   complete their next hop; no new writes are blocked (deferred to
   post-V1 per user "wait and see" 2026-07-24).

2. WINDING_DOWN → hard-cascade-delete when `training_ends_at + 24h`
   <= now: push each farmer one last time with "[Training]
   Practice complete", then delete the entire descendant graph in
   FK-dependency order (QueryResponseMedia → QueryResponse →
   QueryRemark → Query, PromoterAssignment, OrderItemEvent →
   OrderItem → Order, SubscriptionPaymentRequest →
   FarmerSubscriptionHistory → Subscription, ClientPromoter,
   ClientLocation, ClientCrop, Client itself).

Runs hourly per celery beat schedule (see app/celery_app.py — beat
entry lands in the same commit).
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.clients.models import (
    Client, ClientCrop, ClientLocation, ClientPromoter, ClientUser,
    ClientUserRole,
)
from app.modules.farmpundit.models import (
    Query, QueryRemark, QueryResponse,
)
from app.modules.orders.models import (
    Order, OrderItem, OrderItemEvent,
)
from app.modules.platform.models import StatusEnum, User
from app.modules.subscriptions.models import (
    FarmerSubscriptionHistory, PromoterAssignment, Subscription,
    SubscriptionPaymentRequest,
)
from app.services.fcm_service import send_fcm

logger = logging.getLogger(__name__)


TRAINING_WIND_DOWN_HOURS = 24


# ── FCM helpers ───────────────────────────────────────────────────────────────

async def _push_training_ending(db: AsyncSession, child: Client) -> None:
    """Fired at ACTIVE → WINDING_DOWN transition. Recipients: CA of
    the parent client + every farmer with a training subscription."""
    ends_at_str = (
        child.training_ends_at.strftime("%d %b %H:%M UTC")
        if child.training_ends_at else "shortly"
    )
    title = "[Training] Session ended"
    body = (
        f"The training session for {child.display_name or child.full_name} "
        f"is winding down. In-flight orders and queries have 24 hours to "
        f"complete; after that the sandbox will be cleared."
    )
    # CA of the parent — they're the one who started the session.
    ca_rows = (await db.execute(
        select(User)
        .join(ClientUser, ClientUser.user_id == User.id)
        .where(
            ClientUser.client_id == child.parent_client_id,
            ClientUser.role == ClientUserRole.CA,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalars().all()
    # Farmers with subs under the training child.
    farmer_rows = (await db.execute(
        select(User)
        .join(Subscription, Subscription.farmer_user_id == User.id)
        .where(Subscription.client_id == child.id)
        .distinct()
    )).scalars().all()
    for u in list(ca_rows) + list(farmer_rows):
        if not u or not u.fcm_token:
            continue
        try:
            await send_fcm(
                token=u.fcm_token, title=title, body=body,
                data={
                    "type": "TRAINING_WINDING_DOWN",
                    "training_client_id": child.id,
                    "parent_client_id": child.parent_client_id,
                    "click_action": "/",
                },
            )
        except Exception as exc:
            logger.error(f"FCM push failed for training-ending u={u.id}: {exc}")


async def _push_training_complete(
    db: AsyncSession, child: Client, farmer_ids: Iterable[str],
) -> None:
    """Fired at DELETE-cascade time — one final push to every farmer
    whose training sub is about to vanish, and to the CA."""
    title = "[Training] Practice complete"
    body = (
        f"The {child.display_name or child.full_name} sandbox has been "
        f"cleared. Any practice subscriptions have been removed."
    )
    recipient_ids = set(farmer_ids)
    ca_rows = (await db.execute(
        select(User.id)
        .join(ClientUser, ClientUser.user_id == User.id)
        .where(
            ClientUser.client_id == child.parent_client_id,
            ClientUser.role == ClientUserRole.CA,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalars().all()
    recipient_ids.update(ca_rows)
    if not recipient_ids:
        return
    users = (await db.execute(
        select(User).where(User.id.in_(recipient_ids))
    )).scalars().all()
    for u in users:
        if not u.fcm_token:
            continue
        try:
            await send_fcm(
                token=u.fcm_token, title=title, body=body,
                data={
                    "type": "TRAINING_COMPLETE",
                    "parent_client_id": child.parent_client_id,
                    "click_action": "/",
                },
            )
        except Exception as exc:
            logger.error(f"FCM push failed for training-complete u={u.id}: {exc}")


# ── Cascade delete ────────────────────────────────────────────────────────────

async def _cascade_delete_training_child(
    db: AsyncSession, child: Client,
) -> None:
    """Hard-delete every descendant row of the training child in
    FK-dependency order, then the Client row itself. Idempotent —
    empty tables just no-op. Runs in the caller's transaction so a
    partial failure rolls back everything and the next hourly sweep
    can retry cleanly.

    Note: ClientUser rows are NOT expected under a training child
    (portal auth is filtered out in Commit E and no code path
    creates them), so this doesn't attempt to delete them. If any
    ever appear, the Client delete at the end will 500 with a FK
    violation and we can extend the sweep.
    """
    cid = child.id

    # Subscriptions and their descendants.
    sub_ids = (await db.execute(
        select(Subscription.id).where(Subscription.client_id == cid)
    )).scalars().all()
    if sub_ids:
        await db.execute(
            delete(PromoterAssignment).where(
                PromoterAssignment.subscription_id.in_(sub_ids)
            )
        )
        await db.execute(
            delete(SubscriptionPaymentRequest).where(
                SubscriptionPaymentRequest.subscription_id.in_(sub_ids)
            )
        )
        await db.execute(
            delete(FarmerSubscriptionHistory).where(
                FarmerSubscriptionHistory.subscription_id.in_(sub_ids)
            )
        )

    # Orders and their descendants.
    order_ids = (await db.execute(
        select(Order.id).where(Order.client_id == cid)
    )).scalars().all()
    if order_ids:
        await db.execute(
            delete(OrderItemEvent).where(
                OrderItemEvent.order_id.in_(order_ids)
            )
        )
        await db.execute(
            delete(OrderItem).where(OrderItem.order_id.in_(order_ids))
        )
        await db.execute(delete(Order).where(Order.id.in_(order_ids)))

    # Queries and their descendants.
    query_ids = (await db.execute(
        select(Query.id).where(Query.client_id == cid)
    )).scalars().all()
    if query_ids:
        response_ids = (await db.execute(
            select(QueryResponse.id).where(
                QueryResponse.query_id.in_(query_ids)
            )
        )).scalars().all()
        # QueryResponseMedia FK → QueryResponse.id. Import locally
        # so this file doesn't hard-depend on it if the model
        # location shifts.
        try:
            from app.modules.farmpundit.models import QueryResponseMedia
            if response_ids:
                await db.execute(
                    delete(QueryResponseMedia).where(
                        QueryResponseMedia.response_id.in_(response_ids)
                    )
                )
        except ImportError:
            pass
        await db.execute(
            delete(QueryResponse).where(
                QueryResponse.query_id.in_(query_ids)
            )
        )
        await db.execute(
            delete(QueryRemark).where(QueryRemark.query_id.in_(query_ids))
        )
        await db.execute(delete(Query).where(Query.id.in_(query_ids)))

    # Subscription rows themselves — after all their descendants.
    if sub_ids:
        await db.execute(
            delete(Subscription).where(Subscription.id.in_(sub_ids))
        )

    # ClientPromoter rows created under the training child (promoter
    # invitations to farmers — see Commit G). Real promoters live
    # under the parent; those are untouched.
    await db.execute(
        delete(ClientPromoter).where(ClientPromoter.client_id == cid)
    )

    # Defensive — training children shouldn't have Locations or
    # Crops of their own (they borrow from parent), but if any
    # slipped in we don't want them blocking the Client delete.
    await db.execute(
        delete(ClientLocation).where(ClientLocation.client_id == cid)
    )
    await db.execute(
        delete(ClientCrop).where(ClientCrop.client_id == cid)
    )

    # Finally the Client row itself.
    await db.execute(delete(Client).where(Client.id == cid))


# ── The sweep ─────────────────────────────────────────────────────────────────

async def _run_sweep_with_session(db: AsyncSession, now=None) -> tuple[int, int]:
    """Inner sweep — split out so integration tests can inject the
    testcontainer session and assert on the rows the task commits."""
    now = now or datetime.now(timezone.utc)

    # 1) ACTIVE → WINDING_DOWN when the 12-day clock has passed.
    to_wind = (await db.execute(
        select(Client).where(
            Client.is_training == True,  # noqa: E712
            Client.training_status == "ACTIVE",
            Client.training_ends_at <= now,
        )
    )).scalars().all()
    for child in to_wind:
        child.training_status = "WINDING_DOWN"
        await _push_training_ending(db, child)

    # 2) WINDING_DOWN → DELETE when the 24h grace has also passed.
    cutoff = now - timedelta(hours=TRAINING_WIND_DOWN_HOURS)
    to_delete = (await db.execute(
        select(Client).where(
            Client.is_training == True,  # noqa: E712
            Client.training_status == "WINDING_DOWN",
            Client.training_ends_at <= cutoff,
        )
    )).scalars().all()
    for child in to_delete:
        farmer_ids = (await db.execute(
            select(Subscription.farmer_user_id)
            .where(Subscription.client_id == child.id)
            .distinct()
        )).scalars().all()
        await _push_training_complete(db, child, farmer_ids)
        await _cascade_delete_training_child(db, child)

    if to_wind or to_delete:
        await db.commit()
        logger.info(
            f"Training expiry sweep: wound {len(to_wind)}, "
            f"cascade-deleted {len(to_delete)}"
        )
    return len(to_wind), len(to_delete)


async def _run() -> tuple[int, int]:
    async with AsyncSessionLocal() as db:
        return await _run_sweep_with_session(db)


@celery_app.task(name="app.tasks.training_expiry.sweep_training_expiry")
def sweep_training_expiry():
    """Hourly sweep. See module docstring."""
    return asyncio.run(_run())
