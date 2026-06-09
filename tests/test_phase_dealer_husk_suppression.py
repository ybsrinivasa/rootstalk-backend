"""Husk suppression on /dealer/orders.

Mirrors the facilitator husk suppression (2026-06-07 Batch 3).

User direction 2026-06-09 (Dealer mirroring Batch 3): the dealer's
active feed should not be crowded with REROUTED-only husks (orders
whose every active item has migrated away). /dealer/history opts
in via ?include_husks=true for the audit deep-dive (Cancelled
tab).

Mixed orders (some REROUTED + some live items) stay visible
because live items still need attention. Terminal-status orders
(CANCELLED / EXPIRED / REJECTED) also drop from the active feed
by default; include_husks=true brings them back.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from app.modules.clients.models import ClientPromoter
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import list_dealer_orders
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


async def _dealer_with_order(
    db, *,
    order_status: OrderStatus,
    item_statuses: list[OrderItemStatus],
):
    farmer = await make_user(db, name="Khaza Farmer")
    dealer = await make_user(db, name="Y B Srinivasa", phone="+919800099002")
    client = await make_client(db, full_name="Padmashali Seeds")
    package = await make_package(db, client, name="Tomato Pack")
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    timeline = await make_timeline(db, package)
    practice = await make_practice(db, timeline)

    db.add(ClientPromoter(
        client_id=client.id, user_id=dealer.id,
        promoter_type="DEALER", status="ACTIVE",
    ))

    order = Order(
        subscription_id=sub.id,
        farmer_user_id=farmer.id, client_id=client.id,
        dealer_user_id=dealer.id, facilitator_user_id=None,
        status=order_status,
        date_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 1, tzinfo=timezone.utc),
        expires_at=datetime(2026, 6, 14, tzinfo=timezone.utc),
    )
    db.add(order)
    await db.flush()
    for s in item_statuses:
        db.add(OrderItem(
            order_id=order.id,
            practice_id=practice.id, timeline_id=timeline.id,
            status=s,
        ))
    await db.commit()
    return dealer, order


@requires_docker
@pytest.mark.asyncio
async def test_pure_rerouted_husk_suppressed_by_default(db):
    """Order whose every active item is REROUTED disappears from
    the default feed."""
    dealer, order = await _dealer_with_order(
        db,
        order_status=OrderStatus.PROCESSING,
        item_statuses=[OrderItemStatus.REROUTED, OrderItemStatus.REROUTED],
    )
    out = await list_dealer_orders(
        include_husks=False, db=db, current_user=dealer,
    )
    assert not any(o["id"] == order.id for o in out)


@requires_docker
@pytest.mark.asyncio
async def test_pure_rerouted_husk_surfaces_with_include_husks(db):
    """Same order surfaces with ?include_husks=true (audit
    deep-dive)."""
    dealer, order = await _dealer_with_order(
        db,
        order_status=OrderStatus.PROCESSING,
        item_statuses=[OrderItemStatus.REROUTED, OrderItemStatus.REROUTED],
    )
    out = await list_dealer_orders(
        include_husks=True, db=db, current_user=dealer,
    )
    match = [o for o in out if o["id"] == order.id]
    assert len(match) == 1
    counts = match[0]["item_status_counts"]
    assert all(v == 0 for v in counts.values())  # all live items zero


@requires_docker
@pytest.mark.asyncio
async def test_mixed_order_stays_visible_with_live_only_counts(db):
    """Mixed REROUTED + APPROVED order stays in the active feed
    because the APPROVED items still need Packing handling. Counts
    are computed off LIVE items only (REROUTED excluded)."""
    dealer, order = await _dealer_with_order(
        db,
        order_status=OrderStatus.PARTIALLY_APPROVED,
        item_statuses=[
            OrderItemStatus.APPROVED,
            OrderItemStatus.APPROVED,
            OrderItemStatus.REROUTED,
        ],
    )
    out = await list_dealer_orders(
        include_husks=False, db=db, current_user=dealer,
    )
    match = [o for o in out if o["id"] == order.id]
    assert len(match) == 1
    assert match[0]["item_status_counts"]["approved"] == 2  # REROUTED excluded


@requires_docker
@pytest.mark.asyncio
async def test_cancelled_order_excluded_from_active_feed(db):
    """CANCELLED orders drop from the default feed (existing
    behavior preserved when include_husks=false)."""
    dealer, order = await _dealer_with_order(
        db,
        order_status=OrderStatus.CANCELLED,
        item_statuses=[OrderItemStatus.REROUTED],
    )
    out = await list_dealer_orders(
        include_husks=False, db=db, current_user=dealer,
    )
    assert not any(o["id"] == order.id for o in out)


@requires_docker
@pytest.mark.asyncio
async def test_cancelled_order_surfaces_with_include_husks(db):
    """CANCELLED + EXPIRED orders surface when include_husks=true
    — used by /dealer/history's Cancelled tab."""
    dealer, order = await _dealer_with_order(
        db,
        order_status=OrderStatus.CANCELLED,
        item_statuses=[OrderItemStatus.REROUTED],
    )
    out = await list_dealer_orders(
        include_husks=True, db=db, current_user=dealer,
    )
    assert any(o["id"] == order.id for o in out)
