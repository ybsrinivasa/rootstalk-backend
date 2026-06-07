"""Anti-manipulation: facilitator sees per-item brand / qty / cost
ONLY when the item is APPROVED. Locks the redaction shape at the
facilitator detail endpoint so a future refactor can't silently
re-leak.

User direction 2026-06-07: pre-approval the facilitator may see
counts only; post farmer-approval the approved list is needed for
pickup at the dealer's shop.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from app.modules.clients.models import ClientPromoter
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import get_facilitator_order
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


async def _facilitator_with_order(db, *, item_status: OrderItemStatus):
    """Seed an active facilitator + an order with one item in the
    requested status, including brand / qty / cost on the row. The
    redaction decision happens in the read endpoint regardless of
    whether the row carries values."""
    farmer = await make_user(db, name="Khaza Farmer")
    facilitator = await make_user(db, name="Sri Lakshmi", phone="+919800000001")
    client = await make_client(db, full_name="Padmashali Seeds")
    package = await make_package(db, client, name="Tomato Pack 2026")
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    timeline = await make_timeline(db, package)
    practice = await make_practice(db, timeline)

    db.add(ClientPromoter(
        client_id=client.id, user_id=facilitator.id,
        promoter_type="FACILITATOR", status="ACTIVE",
    ))
    order = Order(
        subscription_id=sub.id,
        farmer_user_id=farmer.id, client_id=client.id,
        facilitator_user_id=facilitator.id, dealer_user_id=None,
        status=OrderStatus.SENT,
        date_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 1, tzinfo=timezone.utc),
        expires_at=datetime(2026, 6, 14, tzinfo=timezone.utc),
    )
    db.add(order)
    await db.flush()
    item = OrderItem(
        order_id=order.id,
        practice_id=practice.id,
        timeline_id=timeline.id,
        status=item_status,
        brand_cosh_id="cosh-trade-name-id",
        brand_name="Agroneem",
        given_volume=1.5,
        volume_unit="L",
        price=540.0,
    )
    db.add(item)
    await db.commit()
    return facilitator, order


@pytest.mark.parametrize("status", [
    OrderItemStatus.PENDING,
    OrderItemStatus.AVAILABLE,
    OrderItemStatus.POSTPONED,
    OrderItemStatus.NOT_AVAILABLE,
    OrderItemStatus.SENT_FOR_APPROVAL,
    OrderItemStatus.REJECTED,
])
@requires_docker
@pytest.mark.asyncio
async def test_facilitator_cannot_see_brand_qty_cost_pre_approval(db, status):
    """For every non-APPROVED status, the facilitator detail endpoint
    must null out brand_cosh_id / brand_name / given_volume /
    volume_unit / price. Status + id + practice_id stay visible (the
    facilitator needs the count + state)."""
    facilitator, order = await _facilitator_with_order(db, item_status=status)
    out = await get_facilitator_order(
        order_id=order.id, db=db, current_user=facilitator,
    )
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["status"] == status
    assert item["brand_cosh_id"] is None
    assert item["brand_name"] is None
    assert item["given_volume"] is None
    assert item["volume_unit"] is None
    assert item["price"] is None


@requires_docker
@pytest.mark.asyncio
async def test_facilitator_sees_brand_qty_cost_post_approval(db):
    """Once the item is APPROVED the facilitator sees the full
    details so they can pick up at the dealer."""
    facilitator, order = await _facilitator_with_order(
        db, item_status=OrderItemStatus.APPROVED,
    )
    out = await get_facilitator_order(
        order_id=order.id, db=db, current_user=facilitator,
    )
    item = out["items"][0]
    assert item["status"] == OrderItemStatus.APPROVED
    assert item["brand_cosh_id"] == "cosh-trade-name-id"
    assert item["brand_name"] == "Agroneem"
    assert item["given_volume"] == 1.5
    assert item["volume_unit"] == "L"
    assert item["price"] == 540.0
