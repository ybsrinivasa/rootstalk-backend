"""Orders V2 Batch 23 — SE adds a new Timeline after the farmer
already has live orders.

This is a load-bearing demo scenario: under climate change, clients
must be able to add NEW Timelines (not just edit existing ones) to
respond to emerging risks. Existing farmers with live orders on
other timelines should be able to order the new TL's practices
immediately.

Verifies:
- A new TL on the package is included in compute_bundle for the
  farmer (the new TL is unlocked, BL-13 step 4 says unlocked
  timelines auto-upgrade on next render).
- The PO LOCK from another order on a different timeline does NOT
  block the new TL — locks are per-timeline (BL-05a step 6).
- Order creation against the new TL succeeds and takes its own
  snapshot.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import OrderCreate, create_order
from app.services.order_bundle import (
    CATEGORY_PESTICIDE, compute_bundle,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_onboarded_dealer, make_package,
    make_practice, make_subscription, make_timeline, make_user,
)


@requires_docker
@pytest.mark.asyncio
async def test_new_timeline_added_after_existing_order_is_bundle_eligible(db):
    """Farmer has Order O1 on TL_A (locked via PENDING). SE adds
    TL_B to the package. compute_bundle for a new order must
    include TL_B's practice."""
    user = await make_user(db, name="Farmer Climate")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=2)
    await db.commit()

    tl_a = await make_timeline(
        db, pkg, name="TL_A",
        from_type=TimelineFromType.DAS, from_value=0, to_value=15,
    )
    p_a = await make_practice(db, tl_a, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_a, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:carbendazim")
    await db.commit()

    dealer = await make_onboarded_dealer(db, client=client, name="D-Climate")
    await db.commit()

    # Place Order O1 with p_a (TL_A becomes PO-locked via PENDING).
    o1 = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=20),
        status=OrderStatus.SENT,
        dealer_user_id=dealer.id,
    )
    db.add(o1)
    await db.flush()
    db.add(OrderItem(
        order_id=o1.id, practice_id=p_a.id, timeline_id=tl_a.id,
        status=OrderItemStatus.PENDING,
    ))
    await db.commit()

    # SE adds TL_B in response to climate change.
    tl_b = await make_timeline(
        db, pkg, name="TL_B_NewPest",
        from_type=TimelineFromType.DAS, from_value=5, to_value=25,
    )
    p_b = await make_practice(db, tl_b, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_b, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:spinosad")
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=30), today=date.today(),
    )
    ids = {row["id"] for row in bundle["practices"]}
    # TL_B's practice must be orderable — TL_B is brand new, no lock.
    assert p_b.id in ids
    # TL_A's practice is in O1 already → excluded.
    assert p_a.id not in ids


@requires_docker
@pytest.mark.asyncio
async def test_new_timeline_can_be_ordered_end_to_end(db):
    """Round-trip: SE adds new TL, farmer creates order against
    its practice, OrderItem lands with a fresh snapshot."""
    user = await make_user(db, name="Farmer E2E")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=2)
    await db.commit()

    # Farmer has a live order on TL_A.
    tl_a = await make_timeline(
        db, pkg, name="TL_A",
        from_type=TimelineFromType.DAS, from_value=0, to_value=15,
    )
    p_a = await make_practice(db, tl_a, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_a, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:carbendazim")
    dealer = await make_onboarded_dealer(db, client=client, name="D-E2E")
    await db.commit()

    o1 = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=20),
        status=OrderStatus.SENT,
        dealer_user_id=dealer.id,
    )
    db.add(o1)
    await db.flush()
    db.add(OrderItem(
        order_id=o1.id, practice_id=p_a.id, timeline_id=tl_a.id,
        status=OrderItemStatus.PENDING,
    ))
    await db.commit()

    # SE adds TL_B post-hoc.
    tl_b = await make_timeline(
        db, pkg, name="TL_B_Climate",
        from_type=TimelineFromType.DAS, from_value=5, to_value=25,
    )
    p_b = await make_practice(db, tl_b, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_b, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:spinosad")
    await db.commit()

    # Farmer orders against TL_B.
    res = await create_order(
        request=OrderCreate(
            subscription_id=sub.id, client_id=client.id,
            date_from=datetime.now(timezone.utc),
            date_to=datetime.now(timezone.utc) + timedelta(days=30),
            practice_ids=[p_b.id],
            dealer_user_id=dealer.id,
            farm_area_acres=1.0,
        ),
        db=db, current_user=user,
    )
    o2_id = res["id"]

    # Order placed cleanly.
    items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == o2_id)
    )).scalars().all()
    assert len(items) == 1
    assert items[0].practice_id == p_b.id
    # Phase 3.2: snapshot landed for the new timeline.
    assert items[0].snapshot_id is not None
