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

from sqlalchemy import delete, or_, select
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

# 2026-07-25 — Extended cascade (Commit L). Imported inline in
# _cascade_delete_training_child so a model file rename doesn't
# fail this task at import time; each block guarded so a missing
# model just skips its layer rather than aborting the sweep.

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

    2026-07-25 — Extended (Commit L) to cover every table that can
    legitimately carry a row under a training client:
      - SeedOrderFull (real gap — farmer can place a seed order
        during training).
      - PackingList / SeedOrder (order-scoped).
      - Alert / AlertRecipient / SubscriptionWaitlist /
        ConditionalAnswer / TriggeredCHAEntry /
        FarmPunditPreferences / DiagnosisSession (subscription-scoped).
      - QueryMedia (query-scoped, was missing).
      - QRScan (referencing training-scoped order_items / seed orders).
      - Defensive: ClientFarmPundit / PunditInvitation /
        StandardResponse / SubscriptionPool / ClientUser (should
        never exist under a training child but empty deletes are
        cheap and prevent a future flow slipping in).

    Order matters — always delete the FK-referring rows before
    the referenced rows. See the inline sections for exact
    ordering.
    """
    cid = child.id

    # ── Collect the id-sets we'll reference in multiple layers ────
    sub_ids: list[str] = (await db.execute(
        select(Subscription.id).where(Subscription.client_id == cid)
    )).scalars().all()
    order_ids: list[str] = (await db.execute(
        select(Order.id).where(Order.client_id == cid)
    )).scalars().all()
    query_ids: list[str] = (await db.execute(
        select(Query.id).where(Query.client_id == cid)
    )).scalars().all()

    # SeedOrderFull sits directly under Client (seed_mgmt module).
    try:
        from app.modules.seed_mgmt.models import SeedOrderFull
        seed_full_ids = (await db.execute(
            select(SeedOrderFull.id).where(SeedOrderFull.client_id == cid)
        )).scalars().all()
    except ImportError:
        SeedOrderFull = None  # type: ignore
        seed_full_ids = []

    # SeedOrder (older orders module) sits under Subscription.
    try:
        from app.modules.orders.models import SeedOrder
        seed_order_ids: list[str] = []
        if sub_ids:
            seed_order_ids = (await db.execute(
                select(SeedOrder.id).where(
                    SeedOrder.subscription_id.in_(sub_ids)
                )
            )).scalars().all()
    except ImportError:
        SeedOrder = None  # type: ignore
        seed_order_ids = []

    # Item ids for QR scan cleanup below.
    item_ids: list[str] = []
    if order_ids:
        item_ids = (await db.execute(
            select(OrderItem.id).where(OrderItem.order_id.in_(order_ids))
        )).scalars().all()

    # ── Layer 1: media / QR scans (deepest descendants) ───────────
    if query_ids:
        response_ids = (await db.execute(
            select(QueryResponse.id).where(
                QueryResponse.query_id.in_(query_ids)
            )
        )).scalars().all()
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
        # QueryMedia (attached to the Query itself, not the response).
        # Missing from the V1 cascade — closed 2026-07-25.
        try:
            from app.modules.farmpundit.models import QueryMedia
            await db.execute(
                delete(QueryMedia).where(QueryMedia.query_id.in_(query_ids))
            )
        except ImportError:
            pass

    # QRScan may reference training-scoped order_items / seed orders.
    # Real farmers scanning inside training is unlikely but possible;
    # scans with no ondelete would block the item/seed delete below.
    try:
        from app.modules.qr.models import QRScan
        qr_where = []
        if item_ids:
            qr_where.append(QRScan.order_item_id.in_(item_ids))
        if seed_full_ids:
            qr_where.append(QRScan.seed_order_id.in_(seed_full_ids))
        if qr_where:
            await db.execute(delete(QRScan).where(or_(*qr_where)))
    except ImportError:
        pass

    # ── Layer 2: query descendants proper ─────────────────────────
    if query_ids:
        await db.execute(
            delete(QueryResponse).where(
                QueryResponse.query_id.in_(query_ids)
            )
        )
        await db.execute(
            delete(QueryRemark).where(QueryRemark.query_id.in_(query_ids))
        )

    # ── Layer 3: subscription-scoped rows (before Subscription) ───
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
        # Alert + AlertRecipient (subscription_id FK).
        try:
            from app.modules.subscriptions.models import Alert, AlertRecipient
            await db.execute(
                delete(AlertRecipient).where(
                    AlertRecipient.subscription_id.in_(sub_ids)
                )
            )
            await db.execute(
                delete(Alert).where(Alert.subscription_id.in_(sub_ids))
            )
        except ImportError:
            pass
        # SubscriptionWaitlist (training subs go straight to ACTIVE
        # so shouldn't exist here, but defensive).
        try:
            from app.modules.subscriptions.models import SubscriptionWaitlist
            await db.execute(
                delete(SubscriptionWaitlist).where(
                    SubscriptionWaitlist.subscription_id.in_(sub_ids)
                )
            )
        except ImportError:
            pass
        # ConditionalAnswer (BL-01 guided-elimination answers).
        try:
            from app.modules.subscriptions.models import ConditionalAnswer
            await db.execute(
                delete(ConditionalAnswer).where(
                    ConditionalAnswer.subscription_id.in_(sub_ids)
                )
            )
        except ImportError:
            pass
        # TriggeredCHAEntry (pundit-triggered CHA/QA advisory entries).
        try:
            from app.modules.subscriptions.models import TriggeredCHAEntry
            await db.execute(
                delete(TriggeredCHAEntry).where(
                    TriggeredCHAEntry.subscription_id.in_(sub_ids)
                )
            )
        except ImportError:
            pass
        # FarmPunditPreferences (farmer's chosen pundit per sub).
        try:
            from app.modules.farmpundit.models import FarmPunditPreference
            await db.execute(
                delete(FarmPunditPreference).where(
                    FarmPunditPreference.subscription_id.in_(sub_ids)
                )
            )
        except ImportError:
            pass
        # DiagnosisSession (farmer's Diagnose flow).
        try:
            from app.modules.diagnosis.models import DiagnosisSession
            await db.execute(
                delete(DiagnosisSession).where(
                    DiagnosisSession.subscription_id.in_(sub_ids)
                )
            )
        except ImportError:
            pass

    # ── Layer 4: query itself ─────────────────────────────────────
    if query_ids:
        await db.execute(delete(Query).where(Query.id.in_(query_ids)))

    # ── Layer 5: order-scoped rows (before Order) ─────────────────
    if order_ids:
        await db.execute(
            delete(OrderItemEvent).where(
                OrderItemEvent.order_id.in_(order_ids)
            )
        )
        # PackingList (order_id AND/OR seed_order_id FKs — a training
        # order's packing list may have either populated).
        try:
            from app.modules.orders.models import PackingList
            pl_where = [PackingList.order_id.in_(order_ids)]
            if seed_order_ids:
                pl_where.append(PackingList.seed_order_id.in_(seed_order_ids))
            await db.execute(delete(PackingList).where(or_(*pl_where)))
        except ImportError:
            pass
        # SeedOrder (older orders module — subscription_id already
        # gated above; delete now before OrderItem for symmetry).
        if seed_order_ids and SeedOrder is not None:
            await db.execute(
                delete(SeedOrder).where(SeedOrder.id.in_(seed_order_ids))
            )
        await db.execute(
            delete(OrderItem).where(OrderItem.order_id.in_(order_ids))
        )
        await db.execute(delete(Order).where(Order.id.in_(order_ids)))

    # ── Layer 6: SeedOrderFull (client-scoped, independent of Order) ─
    if seed_full_ids and SeedOrderFull is not None:
        await db.execute(
            delete(SeedOrderFull).where(SeedOrderFull.id.in_(seed_full_ids))
        )

    # ── Layer 7: Subscription itself (after every subscription-scoped
    #    row above has cleared). ──────────────────────────────────
    if sub_ids:
        await db.execute(
            delete(Subscription).where(Subscription.id.in_(sub_ids))
        )

    # ── Layer 8: defensive client-scoped tables (should never exist
    #    under a training child but empty deletes cost nothing) ────
    # StandardResponse (CA-authored Q&A library — training doesn't
    # author).
    try:
        from app.modules.farmpundit.models import StandardResponse
        await db.execute(
            delete(StandardResponse).where(StandardResponse.client_id == cid)
        )
    except ImportError:
        pass
    # ClientFarmPundit / PunditInvitation (pundit onboarding to a
    # client — training doesn't onboard pundits).
    try:
        from app.modules.farmpundit.models import (
            ClientFarmPundit, PunditInvitation,
        )
        await db.execute(
            delete(ClientFarmPundit).where(ClientFarmPundit.client_id == cid)
        )
        await db.execute(
            delete(PunditInvitation).where(PunditInvitation.client_id == cid)
        )
    except ImportError:
        pass
    # SubscriptionPool (training bypasses pools — Commit D — but
    # defensive).
    try:
        from app.modules.subscriptions.models import SubscriptionPool
        await db.execute(
            delete(SubscriptionPool).where(SubscriptionPool.client_id == cid)
        )
    except ImportError:
        pass
    # ClientUser (portal auth is filtered out of training via Commit
    # E; no code path creates these under a training child).
    await db.execute(
        delete(ClientUser).where(ClientUser.client_id == cid)
    )

    # ── Layer 9: ClientPromoter + ClientLocation + ClientCrop ─────
    await db.execute(
        delete(ClientPromoter).where(ClientPromoter.client_id == cid)
    )
    await db.execute(
        delete(ClientLocation).where(ClientLocation.client_id == cid)
    )
    await db.execute(
        delete(ClientCrop).where(ClientCrop.client_id == cid)
    )

    # ── Layer 10: Client itself (PromoterAllocation / EnterpriseLicense
    #    / LockedTimelineSnapshot / SubscriptionPoolConfigError all
    #    have ondelete=CASCADE on their client_id FK — the Client
    #    delete below sweeps them automatically). ─────────────────
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
