"""Phase 3.2 — order_items.snapshot_id is populated at order create.

Calls the POST /farmer/orders + buy-all-dbs route functions directly with
seeded fixtures. Verifies:

  - On order create, every order_item gets snapshot_id pointing to a row
    in locked_timeline_snapshots that matches (sub_id, item.timeline_id).
  - The snapshot exists with lock_trigger='PURCHASE_ORDER'.
  - All items for the same timeline share the same snapshot_id.
  - Re-ordering against a timeline already snapshotted (e.g. previously
    by a VIEWED lock) reuses that snapshot's id (immutability — Rules 1-2).
  - buy-all-dbs populates snapshot_id on every consolidated DBS item.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.advisory.models import (
    PracticeL0, TimelineFromType,
)
from app.modules.orders.models import OrderItem
from app.modules.orders.router import OrderCreate, create_order
from app.modules.subscriptions.router import buy_all_dbs
from app.modules.subscriptions.snapshot_models import LockedTimelineSnapshot
from app.services.snapshot import take_snapshot
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


async def _seed_orderable_subscription(db):
    """User/client/package/sub + DAS timeline 0..30 with 2 INPUT practices."""
    user = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=package,
    )
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    tl = await make_timeline(
        db, package, name="ORDER_TL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p1 = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2="UREA")
    await make_element(db, p1, value="50", unit_cosh_id="kg_per_acre")
    p2 = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2="DAP",
        display_order=1,
    )
    await make_element(db, p2, value="25", unit_cosh_id="kg_per_acre")
    return user, client, package, sub, tl, [p1, p2]


async def _seed_dbs_orderable(db):
    """Subscription with two DBS practices for buy-all-dbs."""
    user = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=package,
    )
    sub.crop_start_date = datetime.now(timezone.utc) + timedelta(days=15)
    sub.farm_area_acres = 1.0
    sub.area_unit = "acres"
    await db.commit()

    tl = await make_timeline(
        db, package, name="DBS_TL",
        from_type=TimelineFromType.DBS, from_value=10, to_value=30,
    )
    p1 = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="MANCOZEB",
    )
    await make_element(db, p1, value="2.5", unit_cosh_id="kg_per_acre")
    return user, client, package, sub, tl, p1


# ── POST /farmer/orders ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_order_populates_snapshot_id(db):
    user, client, _pkg, sub, tl, practices = await _seed_orderable_subscription(db)

    payload = OrderCreate(
        subscription_id=sub.id,
        client_id=client.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        practice_ids=[p.id for p in practices],
        farm_area_acres=1.0,
        area_unit="acres",
    )
    result = await create_order(request=payload, db=db, current_user=user)
    order_id = result["id"]

    items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order_id)
    )).scalars().all()
    assert len(items) == 2

    # Snapshot row exists.
    snaps = (await db.execute(
        select(LockedTimelineSnapshot).where(
            LockedTimelineSnapshot.subscription_id == sub.id,
            LockedTimelineSnapshot.timeline_id == tl.id,
            LockedTimelineSnapshot.source == "CCA",
        )
    )).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].lock_trigger == "PURCHASE_ORDER"

    # Every item points to that snapshot.
    for it in items:
        assert it.snapshot_id == snaps[0].id


@requires_docker
@pytest.mark.asyncio
async def test_create_order_reuses_existing_snapshot(db):
    """If a snapshot already exists (e.g. taken on first today-view), the
    order's items must point to the SAME snapshot row, not a new one
    (Rules 1-2 immutability)."""
    user, client, _pkg, sub, tl, practices = await _seed_orderable_subscription(db)

    pre_snap = await take_snapshot(
        db, sub.id, tl.id, "VIEWED", source="CCA",
    )

    payload = OrderCreate(
        subscription_id=sub.id,
        client_id=client.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        practice_ids=[p.id for p in practices],
        farm_area_acres=1.0,
        area_unit="acres",
    )
    result = await create_order(request=payload, db=db, current_user=user)
    order_id = result["id"]

    items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order_id)
    )).scalars().all()
    for it in items:
        assert it.snapshot_id == pre_snap.id

    # Still exactly one snapshot row.
    snap_count = len((await db.execute(
        select(LockedTimelineSnapshot).where(
            LockedTimelineSnapshot.subscription_id == sub.id,
            LockedTimelineSnapshot.timeline_id == tl.id,
        )
    )).scalars().all())
    assert snap_count == 1

    # Trigger is still the original VIEWED — immutability includes the
    # original lock_trigger value.
    snap_after = (await db.execute(
        select(LockedTimelineSnapshot).where(
            LockedTimelineSnapshot.id == pre_snap.id,
        )
    )).scalar_one()
    assert snap_after.lock_trigger == "VIEWED"


# ── POST /farmer/subscriptions/{id}/orders/buy-all-dbs ──────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_buy_all_dbs_populates_snapshot_id(db):
    user, _client, _pkg, sub, tl, _p = await _seed_dbs_orderable(db)

    result = await buy_all_dbs(
        subscription_id=sub.id,
        data={"category": "PESTICIDE"},
        db=db, current_user=user,
    )
    order_id = result["order_id"]

    items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order_id)
    )).scalars().all()
    assert len(items) >= 1

    snaps = (await db.execute(
        select(LockedTimelineSnapshot).where(
            LockedTimelineSnapshot.subscription_id == sub.id,
            LockedTimelineSnapshot.timeline_id == tl.id,
            LockedTimelineSnapshot.source == "CCA",
        )
    )).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].lock_trigger == "PURCHASE_ORDER"

    for it in items:
        assert it.snapshot_id == snaps[0].id
