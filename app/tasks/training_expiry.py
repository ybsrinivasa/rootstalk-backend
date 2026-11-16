"""Training Sandbox — hourly expiry sweep.

2026-11-16 — Rewritten from session-level to per-crop. The 12-day
clock no longer lives on the training Client (Client.training_ends_at
+ training_status were dropped in migration t2q7l5n18s4j). Each
training Subscription carries its own `auto_remove_at` stamped at
farmer-acceptance time (farmer_responded_at + 12d).

The sweep now:
  - Picks every training Subscription whose `auto_remove_at` has
    passed and that hasn't already been soft-removed.
  - Calls the shared `_soft_remove_training_sub` helper (same code
    path as the Promoter / Farmer Remove-training-crop buttons) with
    AssignmentStatus.AUTO_REMOVED as the end-status.

The session-level hard cascade (`_cascade_delete_training_child`)
still exists — it's what runs when a CA hits Close Session. No session
timer, no WINDING_DOWN state, no 24h grace, no auto-close of the
session itself (CA Start/Close is now the only session-level lever).

Runs hourly per celery beat schedule (see app/celery_app.py).
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.clients.models import (
    Client, ClientCrop, ClientLocation, ClientPromoter, ClientUser,
)
from app.modules.farmpundit.models import (
    Query, QueryRemark, QueryResponse,
)
from app.modules.orders.models import (
    Order, OrderItem, OrderItemEvent,
)
from app.modules.platform.models import User
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


# ── Per-crop auto-remove push ─────────────────────────────────────────────────

async def _push_training_crop_auto_removed(
    db: AsyncSession, sub: Subscription,
) -> None:
    """Fired when the hourly sweep auto-removes a training sub. One
    push to the farmer + one to the assigning promoter."""
    title = "[Training] Crop auto-removed"
    body = (
        "A training crop on your account has been auto-removed after "
        "the 12-day practice window. Ask your promoter to re-assign it "
        "any time."
    )
    recipient_ids: set[str] = {sub.farmer_user_id}
    pa = (await db.execute(
        select(PromoterAssignment.promoter_user_id).where(
            PromoterAssignment.subscription_id == sub.id,
        ).order_by(PromoterAssignment.assigned_at.desc()).limit(1)
    )).scalar_one_or_none()
    if pa:
        recipient_ids.add(pa)
    users = (await db.execute(
        select(User).where(User.id.in_(recipient_ids))
    )).scalars().all()
    for u in users:
        if not u or not u.fcm_token:
            continue
        try:
            await send_fcm(
                token=u.fcm_token, title=title, body=body,
                data={
                    "type": "TRAINING_CROP_AUTO_REMOVED",
                    "subscription_id": sub.id,
                    "click_action": "/",
                },
            )
        except Exception as exc:
            logger.error(f"FCM push failed for training-crop-auto-removed u={u.id}: {exc}")


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
    # 2026-11-16 — Expanded the collection to also pick up rows whose
    # `subscription_id` points to a training sub but whose `client_id`
    # points to the PARENT client. Seen on staging: farmer seed orders
    # recorded during training against the parent's client_id instead
    # of the training child's — the original client_id-only filter
    # missed those and the Subscription DELETE below blew up with
    # `seed_orders_full_subscription_id_fkey` FK violation. Both FKs
    # need clearing before the Subscription delete can land.
    try:
        from app.modules.seed_mgmt.models import SeedOrderFull
        _conditions = [SeedOrderFull.client_id == cid]
        if sub_ids:
            _conditions.append(SeedOrderFull.subscription_id.in_(sub_ids))
        seed_full_ids = (await db.execute(
            select(SeedOrderFull.id).where(or_(*_conditions))
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
        # PracticeAcknowledgement (farmer's daily-practice ticks).
        # Missing from Commit L cascade — caught 2026-07-25 when the
        # first hard-close attempt hit a live YSR Testing session with
        # acknowledged practices and blew up on FK violation.
        try:
            from app.modules.advisory.models import PracticeAcknowledgement
            await db.execute(
                delete(PracticeAcknowledgement).where(
                    PracticeAcknowledgement.subscription_id.in_(sub_ids)
                )
            )
        except ImportError:
            pass
        # LockedTimelineSnapshot (subscription-scoped despite living in
        # the snapshot_models module — `subscription_id` FK is the one
        # that blocks Subscription delete). Note: OrderItem also has a
        # SET NULL FK pointing at this table, which is fine — those
        # rows go via the order cascade above.
        try:
            from app.modules.subscriptions.snapshot_models import (
                LockedTimelineSnapshot,
            )
            await db.execute(
                delete(LockedTimelineSnapshot).where(
                    LockedTimelineSnapshot.subscription_id.in_(sub_ids)
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
    # CMClientAssignment — a SA-portal CM-assignment can point at any
    # ACTIVE client, including a training child (the picker doesn't
    # filter training out today, 2026-08-09). Without this delete the
    # Client row delete hits a FK violation from
    # `cm_client_assignments_client_id_fkey`. Caught 2026-08-09 when
    # the CA hit "End Session Now" on a Kalyan RamaRajya training child
    # that had a stray CM assignment.
    try:
        from app.modules.clients.models import CMClientAssignment
        await db.execute(
            delete(CMClientAssignment).where(CMClientAssignment.client_id == cid)
        )
    except ImportError:
        pass

    # Every other NO-ACTION FK to clients.id — defensive raw SQL deletes
    # so the cascade tolerates a stray row without needing a model
    # import per table. Full list generated 2026-08-10 by querying
    # information_schema for `constraint_type='FOREIGN KEY' AND
    # ccu.table_name='clients' AND rc.delete_rule='NO ACTION'`.
    # Tables already handled above (subscriptions, orders, queries,
    # seed_orders_full, standard_responses, client_farm_pundits,
    # pundit_invitations, subscription_pools, client_users,
    # cm_client_assignments, client_promoters, client_locations,
    # client_crops) are excluded here.
    #
    # Anchor: 2026-08-10 — a training child had rows in
    # `client_organisation_types` (from an SA client-edit), blocking
    # END. The SA-edit surface doesn't filter training children today,
    # so ANY of these could accumulate rows on any given training child.
    # Keep this list in sync with the FK audit whenever a new client-
    # scoped table is added — see feedback memory on training cascade.
    from sqlalchemy import text
    for table in (
        "client_organisation_types",
        "client_user_privileges",
        "crop_expert_assignments",
        "manufacturer_brand_portfolios",
        "packages",
        "parameters",
        "pg_recommendations",
        "product_qr_codes",
        "rm_cases",
        "seed_varieties",
        "sp_recommendations",
        "triggered_cha_entries",
        "variables",
    ):
        await db.execute(
            text(f"DELETE FROM {table} WHERE client_id = :cid"), {"cid": cid},
        )
    # dealer_relationships uses `manufacturer_client_id` — separate
    # column name so it needs its own delete.
    await db.execute(
        text("DELETE FROM dealer_relationships WHERE manufacturer_client_id = :cid"),
        {"cid": cid},
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

async def _run_sweep_with_session(db: AsyncSession, now=None) -> int:
    """Inner sweep — per-training-sub soft-delete. Split out so
    integration tests can inject the testcontainer session and assert
    on the rows the task commits.

    Each sub is removed in its own commit (via `_soft_remove_training_sub`)
    so a single failing row doesn't strand the rest of the batch — the
    session-level all-or-nothing of the old session-sweep was exactly
    the bug that stranded four staging clients on 2026-11-16.
    """
    from app.modules.clients.training_router import _soft_remove_training_sub
    from app.modules.subscriptions.models import AssignmentStatus

    now = now or datetime.now(timezone.utc)

    # One join per sweep. is_training filter keeps the index-hit cheap;
    # the deleted_at IS NULL filter is already covered by the partial
    # index on subscriptions.auto_remove_at.
    rows = (await db.execute(
        select(Subscription)
        .join(Client, Client.id == Subscription.client_id)
        .where(
            Subscription.auto_remove_at.is_not(None),
            Subscription.auto_remove_at <= now,
            Subscription.deleted_at.is_(None),
            Client.is_training == True,  # noqa: E712
        )
    )).scalars().all()

    removed = 0
    for sub in rows:
        try:
            # Farmer-attributed soft-remove would be misleading (the
            # farmer didn't initiate). Pass a synthetic system actor —
            # the deleted_by_user_id FK is SET NULL on actor delete so
            # passing None through needs a wrapper. Use the sub's own
            # farmer_user_id as the audit anchor; the AUTO_REMOVED
            # PromoterAssignment status makes the "sweep-did-this"
            # provenance unambiguous without an extra column.
            farmer_row = (await db.execute(
                select(User).where(User.id == sub.farmer_user_id)
            )).scalar_one_or_none()
            if farmer_row is None:
                logger.warning(
                    f"Training sweep: sub {sub.id} has no farmer row, skipping"
                )
                continue
            await _soft_remove_training_sub(
                db, sub, farmer_row, AssignmentStatus.AUTO_REMOVED,
            )
            await _push_training_crop_auto_removed(db, sub)
            removed += 1
        except Exception as exc:
            logger.error(
                f"Training sweep: soft-remove failed for sub {sub.id}: {exc}"
            )
            await db.rollback()

    if removed:
        logger.info(f"Training expiry sweep: auto-removed {removed} subs")
    return removed


async def _run() -> int:
    async with AsyncSessionLocal() as db:
        return await _run_sweep_with_session(db)


@celery_app.task(name="app.tasks.training_expiry.sweep_training_expiry")
def sweep_training_expiry():
    """Hourly sweep. See module docstring."""
    return asyncio.run(_run())
