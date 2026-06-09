"""Farmer husk suppression on /farmer/subscriptions/{id}/orders.

Mirrors the dealer + facilitator husk suppression. The Farmer
Manage tab's Routed pill matched any order with awaiting +
returned + pickup === 0, which silently included REROUTED-only
husks. New ?include_husks=false default skips those husks;
?include_husks=true lifts the filter for the per-crop History
page's audit deep-dive.

Each SubOrder row also ships rerouted_count so the History
Cancelled tab can surface lineage husks (whose order.status is
typically still PROCESSING or PARTIALLY_APPROVED, not CANCELLED).
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
# Import SeedOrderFull at module level so its table is registered
# on Base.metadata before the testcontainer create_all runs (see
# feedback_test_lazy_model_import.md).
from app.modules.seed_mgmt.models import SeedOrderFull  # noqa: F401
from app.modules.orders.router import list_subscription_orders
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


async def _seed_subscription_with_order(
    db, *,
    order_status: OrderStatus,
    item_statuses: list[OrderItemStatus],
):
    farmer = await make_user(db, name="Khaza Farmer")
    client = await make_client(db, full_name="Padmashali Seeds")
    package = await make_package(db, client, name="Tomato Pack")
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    timeline = await make_timeline(db, package)
    practice = await make_practice(db, timeline)

    order = Order(
        subscription_id=sub.id,
        farmer_user_id=farmer.id, client_id=client.id,
        dealer_user_id=None, facilitator_user_id=None,
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
    return farmer, sub, order


@requires_docker
@pytest.mark.asyncio
async def test_pure_rerouted_husk_suppressed_by_default(db):
    """Order whose every active item is REROUTED disappears from
    the default response."""
    farmer, sub, order = await _seed_subscription_with_order(
        db,
        order_status=OrderStatus.PROCESSING,
        item_statuses=[OrderItemStatus.REROUTED, OrderItemStatus.REROUTED],
    )
    out = await list_subscription_orders(
        subscription_id=sub.id, include_husks=False,
        db=db, current_user=farmer,
    )
    matches = [o for o in out["orders"] if o["id"] == order.id]
    assert matches == []


@requires_docker
@pytest.mark.asyncio
async def test_pure_rerouted_husk_surfaces_with_include_husks(db):
    """Same order surfaces with include_husks=true. rerouted_count
    exposed so the PWA's History Cancelled tab can mark it as a
    lineage husk."""
    farmer, sub, order = await _seed_subscription_with_order(
        db,
        order_status=OrderStatus.PROCESSING,
        item_statuses=[OrderItemStatus.REROUTED, OrderItemStatus.REROUTED],
    )
    out = await list_subscription_orders(
        subscription_id=sub.id, include_husks=True,
        db=db, current_user=farmer,
    )
    matches = [o for o in out["orders"] if o["id"] == order.id]
    assert len(matches) == 1
    assert matches[0]["rerouted_count"] == 2


@requires_docker
@pytest.mark.asyncio
async def test_mixed_order_stays_visible(db):
    """An order with some REROUTED + some live items stays visible
    in the active feed because the live items still need attention.
    Per-status counts already exclude REROUTED from the
    awaiting/returned/postponed/approved tallies."""
    farmer, sub, order = await _seed_subscription_with_order(
        db,
        order_status=OrderStatus.PARTIALLY_APPROVED,
        item_statuses=[
            OrderItemStatus.APPROVED,
            OrderItemStatus.NOT_AVAILABLE,
            OrderItemStatus.REROUTED,
        ],
    )
    out = await list_subscription_orders(
        subscription_id=sub.id, include_husks=False,
        db=db, current_user=farmer,
    )
    matches = [o for o in out["orders"] if o["id"] == order.id]
    assert len(matches) == 1
    assert matches[0]["returned_count"] == 1  # NA
    assert matches[0]["rerouted_count"] == 1
