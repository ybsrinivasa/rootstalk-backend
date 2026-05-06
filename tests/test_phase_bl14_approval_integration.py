"""BL-14 audit — DB-backed integration tests for the brand-reveal fix.

Pure-function coverage of `is_brand_visible_to_farmer` lives in
`tests/test_bl14.py` (5 tests). This file drives
`get_farmer_order_detail` directly with seeded rows in the
testcontainer DB to verify the route's response payload exposes the
canonical brand_name at the right stage of the lifecycle.

Pre-audit the route revealed brand_name only when status==APPROVED,
so a farmer at the SENT_FOR_APPROVAL step saw `brand_name: null` and
had to approve blind. Post-fix, brand_name is visible from
SENT_FOR_APPROVAL onward.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import get_farmer_order_detail
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


async def _seed_farmer_order_with_item(db, item_status: OrderItemStatus):
    """Seed farmer + dealer + sub + practice + Order + OrderItem with
    a populated brand_name in the requested item status. Returns
    (farmer, order, item)."""
    farmer = await make_user(db, name="Farmer V")
    dealer = await make_user(db, name="Dealer V")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    tl = await make_timeline(db, package, name="TL_BL14")
    practice = await make_practice(db, tl, l1="PESTICIDE", l2="MANCOZEB")
    await make_element(db, practice, value="2", unit_cosh_id="kg_per_acre")

    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id,
        client_id=client.id, dealer_user_id=dealer.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        status=OrderStatus.SENT_FOR_APPROVAL,
    )
    db.add(order); await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        brand_cosh_id="brand:dithane-m45", brand_name="Dithane M-45",
        given_volume=5, volume_unit="kg", price=800,
        status=item_status,
    )
    db.add(item)
    await db.commit()
    return farmer, order, item


@requires_docker
@pytest.mark.asyncio
async def test_brand_visible_at_sent_for_approval(db):
    """The headline fix: at the approval step, the farmer's view of
    the order MUST include brand_name so they can decide
    informedly. Pre-fix this returned null."""
    farmer, order, _ = await _seed_farmer_order_with_item(
        db, OrderItemStatus.SENT_FOR_APPROVAL,
    )
    out = await get_farmer_order_detail(
        order_id=order.id, db=db, current_user=farmer,
    )
    assert len(out["items"]) == 1
    assert out["items"][0]["brand_name"] == "Dithane M-45"


@requires_docker
@pytest.mark.asyncio
async def test_brand_visible_after_approval(db):
    """Brand stays visible after the farmer approves — the purchased-
    items view depends on it."""
    farmer, order, _ = await _seed_farmer_order_with_item(
        db, OrderItemStatus.APPROVED,
    )
    out = await get_farmer_order_detail(
        order_id=order.id, db=db, current_user=farmer,
    )
    assert out["items"][0]["brand_name"] == "Dithane M-45"


@requires_docker
@pytest.mark.asyncio
async def test_brand_hidden_at_available_before_dealer_submits(db):
    """Pre-SENT_FOR_APPROVAL the dealer is still working out brand
    selection. The farmer should not see what the dealer is leaning
    towards before they commit via submit_for_approval."""
    farmer, order, _ = await _seed_farmer_order_with_item(
        db, OrderItemStatus.AVAILABLE,
    )
    out = await get_farmer_order_detail(
        order_id=order.id, db=db, current_user=farmer,
    )
    assert out["items"][0]["brand_name"] is None
