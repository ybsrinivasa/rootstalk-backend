"""Order Management V2 Batch 6 — event sweep on dealer & farmer actions.

Locks in that `order_item_events` actually receives a row every
time a key state change happens. Reports lean on this — without
the events, the lineage chain is just a single in-place status.

This file checks the *write*, not the *side effect* — the
endpoints' state-machine behaviour is already covered elsewhere.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemEvent, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import (
    accept_order, approve_order_item, mark_item_unavailable, postpone_item,
    reject_order_item, route_order_to_dealer,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_onboarded_facilitator,
    make_package, make_practice, make_subscription, make_timeline, make_user,
)


async def _sent_order_with_two_items(db):
    """Farmer + dealer + sent order with two PENDING items."""
    user = await make_user(db, name="Farmer Events")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_events",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p1 = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    p2 = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()

    dealer = await make_onboarded_dealer(db, name="D-Events")
    await db.commit()

    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.SENT,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    item1 = OrderItem(order_id=order.id, practice_id=p1.id, timeline_id=tl.id, status=OrderItemStatus.PENDING)
    item2 = OrderItem(order_id=order.id, practice_id=p2.id, timeline_id=tl.id, status=OrderItemStatus.PENDING)
    db.add(item1)
    db.add(item2)
    await db.commit()
    await db.refresh(item1)
    await db.refresh(item2)
    return user, dealer, order, item1, item2


@requires_docker
@pytest.mark.asyncio
async def test_accept_order_writes_event(db):
    _, dealer, order, _, _ = await _sent_order_with_two_items(db)
    await accept_order(order_id=order.id, db=db, current_user=dealer)

    events = (await db.execute(
        select(OrderItemEvent).where(OrderItemEvent.lineage_id == order.id)
    )).scalars().all()
    assert any(e.event_type == "ACCEPTED" for e in events)
    acc = [e for e in events if e.event_type == "ACCEPTED"][0]
    assert acc.actor_role == "DEALER"
    assert acc.prev_status == OrderStatus.SENT.value
    assert acc.new_status == OrderStatus.PROCESSING.value


@requires_docker
@pytest.mark.asyncio
async def test_postpone_writes_event_with_days(db):
    _, dealer, order, item1, _ = await _sent_order_with_two_items(db)
    await accept_order(order_id=order.id, db=db, current_user=dealer)

    postpone_until = datetime.now(timezone.utc) + timedelta(days=3)
    await postpone_item(
        order_id=order.id, item_id=item1.id,
        data={"postponed_until": postpone_until, "days": 3},
        db=db, current_user=dealer,
    )

    events = (await db.execute(
        select(OrderItemEvent).where(
            OrderItemEvent.lineage_id == item1.lineage_id,
            OrderItemEvent.event_type == "MARKED_POSTPONED",
        )
    )).scalars().all()
    assert len(events) == 1
    assert events[0].actor_role == "DEALER"
    assert events[0].event_metadata is not None
    assert events[0].event_metadata.get("days") == 3


@requires_docker
@pytest.mark.asyncio
async def test_mark_not_available_writes_event(db):
    _, dealer, order, item1, _ = await _sent_order_with_two_items(db)
    await accept_order(order_id=order.id, db=db, current_user=dealer)

    await mark_item_unavailable(
        order_id=order.id, item_id=item1.id,
        db=db, current_user=dealer,
    )

    events = (await db.execute(
        select(OrderItemEvent).where(
            OrderItemEvent.lineage_id == item1.lineage_id,
            OrderItemEvent.event_type == "MARKED_NOT_AVAILABLE",
        )
    )).scalars().all()
    assert len(events) == 1
    assert events[0].new_status == OrderItemStatus.NOT_AVAILABLE.value


@requires_docker
@pytest.mark.asyncio
async def test_farmer_approve_emits_purchase_recorded(db):
    user, dealer, order, item1, _ = await _sent_order_with_two_items(db)
    await accept_order(order_id=order.id, db=db, current_user=dealer)
    # Cheat: move item1 directly to SENT_FOR_APPROVAL so we can hit the
    # farmer's approve endpoint without the full BL-07 brand path.
    item1.status = OrderItemStatus.SENT_FOR_APPROVAL
    item1.brand_name = "TestBrand"
    item1.price = 199.50
    await db.commit()

    await approve_order_item(
        order_id=order.id, item_id=item1.id,
        db=db, current_user=user,
    )

    ev = (await db.execute(
        select(OrderItemEvent).where(
            OrderItemEvent.lineage_id == item1.lineage_id,
            OrderItemEvent.event_type == "PURCHASE_RECORDED",
        )
    )).scalar_one()
    assert ev.actor_role == "FARMER"
    assert ev.event_metadata is not None
    assert ev.event_metadata.get("brand_name") == "TestBrand"


@requires_docker
@pytest.mark.asyncio
async def test_farmer_reject_writes_event(db):
    user, dealer, order, item1, _ = await _sent_order_with_two_items(db)
    await accept_order(order_id=order.id, db=db, current_user=dealer)
    item1.status = OrderItemStatus.SENT_FOR_APPROVAL
    await db.commit()

    await reject_order_item(
        order_id=order.id, item_id=item1.id,
        db=db, current_user=user,
    )

    ev = (await db.execute(
        select(OrderItemEvent).where(
            OrderItemEvent.lineage_id == item1.lineage_id,
            OrderItemEvent.event_type == "REJECTED",
        )
    )).scalar_one()
    assert ev.actor_role == "FARMER"


@requires_docker
@pytest.mark.asyncio
async def test_facilitator_route_to_dealer_writes_event(db):
    # Send the order to a facilitator first.
    user = await make_user(db, name="Farmer F2D")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_f2d",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()

    fac = await make_onboarded_facilitator(db, name="F-route")
    dealer = await make_onboarded_dealer(db, name="D-after")
    await db.commit()

    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.SENT,
        facilitator_user_id=fac.id,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(order_id=order.id, practice_id=p.id, timeline_id=tl.id, status=OrderItemStatus.PENDING))
    await db.commit()

    await route_order_to_dealer(
        order_id=order.id,
        data={"dealer_user_id": dealer.id},
        db=db, current_user=fac,
    )

    ev = (await db.execute(
        select(OrderItemEvent).where(
            OrderItemEvent.lineage_id == order.id,
            OrderItemEvent.event_type == "ROUTED_TO_DEALER",
        )
    )).scalar_one()
    assert ev.actor_role == "FACILITATOR"
    assert ev.event_metadata is not None
    assert ev.event_metadata.get("dealer_user_id") == dealer.id
