"""Order Management V2 Batch 3 — cancel migrates to a fresh DRAFT.

Locks the 2026-05-31 narrative rules:
- Cancel doesn't drop the order's items. It moves them to a new
  DRAFT (same lineage_id) so the farmer can pick a different
  recipient and re-send without re-deriving the bundle.
- The CANCELLED husk keeps the original OrderItem rows under
  status REROUTED so reports can still see what was on this
  leg of the journey.
- Every move writes paired REROUTED_FROM / REROUTED_TO rows to
  `order_item_events`, plus a CANCELLED_BY_FARMER row keyed on
  the order's own id. Reports group by lineage_id.
- DELETE on a CANCELLED husk is allowed and the events table
  preserves the trail via SET NULL on order_id / order_item_id.
- DELETE on a non-CANCELLED order is refused.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemEvent,
    OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import cancel_order, delete_cancelled_order
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


async def _make_sent_order_with_item(db):
    """Helper — minimal sent order with one PENDING item."""
    user = await make_user(db, name="Farmer Migrate")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_migrate",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    pest = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES",
    )
    await db.commit()

    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.SENT,
    )
    db.add(order)
    await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=pest.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return user, order, item


@requires_docker
@pytest.mark.asyncio
async def test_cancel_migrates_items_to_fresh_draft(db):
    user, order, item = await _make_sent_order_with_item(db)
    original_lineage = item.lineage_id
    assert original_lineage is not None

    result = await cancel_order(order_id=order.id, db=db, current_user=user)

    # Husk is terminal.
    await db.refresh(order)
    assert order.status == OrderStatus.CANCELLED

    # Original item stays on the husk under REROUTED.
    await db.refresh(item)
    assert item.status == OrderItemStatus.REROUTED
    assert item.order_id == order.id  # stayed put

    # Response carries the new draft id.
    new_draft_id = result["new_draft_order_id"]
    assert new_draft_id and new_draft_id != order.id
    assert result["migrated_item_count"] == 1

    draft = (await db.execute(select(Order).where(Order.id == new_draft_id))).scalar_one()
    assert draft.status == OrderStatus.DRAFT
    assert draft.dealer_user_id is None
    assert draft.facilitator_user_id is None
    assert draft.category == order.category
    assert draft.date_from == order.date_from
    assert draft.date_to == order.date_to

    # Draft has a fresh OrderItem row in PENDING with the SAME lineage.
    new_items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == new_draft_id)
    )).scalars().all()
    assert len(new_items) == 1
    assert new_items[0].lineage_id == original_lineage
    assert new_items[0].status == OrderItemStatus.PENDING
    assert new_items[0].id != item.id


@requires_docker
@pytest.mark.asyncio
async def test_cancel_emits_audit_events(db):
    user, order, item = await _make_sent_order_with_item(db)
    lineage = item.lineage_id

    await cancel_order(order_id=order.id, db=db, current_user=user)

    item_events = (await db.execute(
        select(OrderItemEvent).where(OrderItemEvent.lineage_id == lineage)
    )).scalars().all()
    types = sorted(e.event_type for e in item_events)
    assert types == ["REROUTED_FROM", "REROUTED_TO"]

    order_events = (await db.execute(
        select(OrderItemEvent).where(OrderItemEvent.lineage_id == order.id)
    )).scalars().all()
    assert len(order_events) == 1
    assert order_events[0].event_type == "CANCELLED_BY_FARMER"
    assert order_events[0].new_status == "CANCELLED"


@requires_docker
@pytest.mark.asyncio
async def test_cancel_refused_while_dealer_viewing(db):
    user, order, _ = await _make_sent_order_with_item(db)
    order.dealer_viewing_until = datetime.now(timezone.utc) + timedelta(seconds=20)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await cancel_order(order_id=order.id, db=db, current_user=user)
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert isinstance(detail, dict) and detail["code"] == "dealer_currently_viewing"


@requires_docker
@pytest.mark.asyncio
async def test_delete_cancelled_husk_succeeds_and_keeps_lineage(db):
    user, order, item = await _make_sent_order_with_item(db)
    lineage = item.lineage_id

    await cancel_order(order_id=order.id, db=db, current_user=user)
    await delete_cancelled_order(order_id=order.id, db=db, current_user=user)

    # Husk gone.
    deleted = (await db.execute(select(Order).where(Order.id == order.id))).scalar_one_or_none()
    assert deleted is None

    # Events table still has the lineage trail.
    surviving_events = (await db.execute(
        select(OrderItemEvent).where(OrderItemEvent.lineage_id == lineage)
    )).scalars().all()
    assert len(surviving_events) >= 2  # REROUTED_FROM + REROUTED_TO


@requires_docker
@pytest.mark.asyncio
async def test_delete_non_cancelled_order_refused(db):
    user, order, _ = await _make_sent_order_with_item(db)

    with pytest.raises(HTTPException) as exc:
        await delete_cancelled_order(order_id=order.id, db=db, current_user=user)
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert isinstance(detail, dict) and detail["code"] == "must_cancel_first"
