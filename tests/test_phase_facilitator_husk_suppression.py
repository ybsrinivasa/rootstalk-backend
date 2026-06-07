"""Husk suppression on the facilitator active feed.

User direction 2026-06-07: when the facilitator reroutes returned
items to a new dealer (or hands them back to the farmer), the
source order's items become REROUTED — audit-only pointers, no
live work. The source must NOT appear in the facilitator's active
queue unless `?include_husks=true`.

Mixed orders (some REROUTED + some live items still in flight)
stay visible because the live items need attention.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from app.modules.clients.models import ClientPromoter
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import list_facilitator_orders
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


async def _facilitator_with_order(db, *, item_statuses: list[OrderItemStatus]):
    farmer = await make_user(db, name="Khaza Farmer")
    facilitator = await make_user(db, name="Sri Lakshmi", phone="+919800000099")
    client = await make_client(db, full_name="Padmashali Seeds")
    package = await make_package(db, client, name="Tomato Pack")
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
    for s in item_statuses:
        db.add(OrderItem(
            order_id=order.id,
            practice_id=practice.id,
            timeline_id=timeline.id,
            status=s,
        ))
    await db.commit()
    return facilitator, order


@requires_docker
@pytest.mark.asyncio
async def test_pure_husk_suppressed_by_default(db):
    """An order where every active item is REROUTED disappears from
    the default response."""
    facilitator, order = await _facilitator_with_order(
        db, item_statuses=[OrderItemStatus.REROUTED, OrderItemStatus.REROUTED],
    )
    out = await list_facilitator_orders(
        status_filter=None, include_husks=False,
        db=db, current_user=facilitator,
    )
    assert not any(o["id"] == order.id for o in out)


@requires_docker
@pytest.mark.asyncio
async def test_pure_husk_surfaces_with_include_husks(db):
    """Same order surfaces when the caller opts in to audit deep-
    dive via include_husks=true."""
    facilitator, order = await _facilitator_with_order(
        db, item_statuses=[OrderItemStatus.REROUTED, OrderItemStatus.REROUTED],
    )
    out = await list_facilitator_orders(
        status_filter=None, include_husks=True,
        db=db, current_user=facilitator,
    )
    husks = [o for o in out if o["id"] == order.id]
    assert len(husks) == 1
    # Husk's counts are all zero — no live items by definition.
    assert husks[0]["item_count"] == 0
    counts = husks[0]["item_status_counts"]
    assert all(v == 0 for v in counts.values())


@requires_docker
@pytest.mark.asyncio
async def test_mixed_order_stays_visible(db):
    """An order with some REROUTED items + some live items (e.g.
    APPROVED + REROUTED) stays in the active feed because the live
    items still need handling. item_count + counts exclude REROUTED."""
    facilitator, order = await _facilitator_with_order(
        db, item_statuses=[
            OrderItemStatus.APPROVED,
            OrderItemStatus.APPROVED,
            OrderItemStatus.REROUTED,
        ],
    )
    out = await list_facilitator_orders(
        status_filter=None, include_husks=False,
        db=db, current_user=facilitator,
    )
    match = [o for o in out if o["id"] == order.id]
    assert len(match) == 1
    # 2 live items (the 2 APPROVED), REROUTED excluded.
    assert match[0]["item_count"] == 2
    assert match[0]["item_status_counts"]["approved"] == 2
    # REROUTED is not surfaced as a count.
    assert "rerouted" not in match[0]["item_status_counts"]
