"""Defence: farmer's /reroute-returned is blocked when a facilitator
owns the order.

User report 2026-06-08: NA items on a facilitator-routed order were
"falling back to the farmer" — the farmer's review page CTA was
firing /farmer/orders/{id}/reroute-returned which created a new
DRAFT with facilitator_user_id=None, silently stealing the order
out of the facilitator's loop. The PWA hides the CTA now; this
backend guard is defence-in-depth for stale tabs / direct URL hits.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from fastapi import HTTPException

from app.modules.clients.models import ClientPromoter
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import reroute_returned_items
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


async def _seed_facilitator_owned_order(db, *, with_facilitator: bool):
    """Seed an order with one NA item. When with_facilitator=True,
    facilitator_user_id is set — the farmer's reroute should be
    refused. Otherwise the existing reroute path works."""
    farmer = await make_user(db, name="Khaza Farmer")
    facilitator = await make_user(db, name="Sri Lakshmi", phone="+919800001234")
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
        facilitator_user_id=(facilitator.id if with_facilitator else None),
        dealer_user_id=None,
        status=OrderStatus.PARTIALLY_APPROVED,
        date_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 1, tzinfo=timezone.utc),
        expires_at=datetime(2026, 6, 14, tzinfo=timezone.utc),
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id,
        practice_id=practice.id, timeline_id=timeline.id,
        status=OrderItemStatus.NOT_AVAILABLE,
    ))
    await db.commit()
    return farmer, order


@requires_docker
@pytest.mark.asyncio
async def test_farmer_reroute_refused_for_facilitator_owned_order(db):
    """The farmer's reroute endpoint refuses with 403 +
    code=facilitator_owns_order when facilitator_user_id is set."""
    farmer, order = await _seed_facilitator_owned_order(
        db, with_facilitator=True,
    )
    with pytest.raises(HTTPException) as exc:
        await reroute_returned_items(
            order_id=order.id, data=None,
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 403
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "facilitator_owns_order"


@requires_docker
@pytest.mark.asyncio
async def test_farmer_reroute_works_when_no_facilitator(db):
    """Direct dealer flow (no facilitator) keeps the existing
    farmer-side reroute behaviour."""
    farmer, order = await _seed_facilitator_owned_order(
        db, with_facilitator=False,
    )
    # Reroute succeeds — fresh DRAFT created with the NA item migrated.
    result = await reroute_returned_items(
        order_id=order.id, data=None,
        db=db, current_user=farmer,
    )
    assert "new_draft_order_id" in result
    assert result["rerouted_count"] == 1
