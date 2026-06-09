"""Dealer decline preserves facilitator when the source was
facilitator-routed.

User direction 2026-06-09: when farmer → facilitator → dealer and
the dealer declines, the order should bounce back to the
facilitator's queue (they committed by forwarding), not silently
fall back to the farmer. Mirrors the "returned items stay with the
facilitator" rule at the order level.

Direct dealer flow (farmer → dealer, dealer declines) keeps the
existing cancel-and-migrate-to-DRAFT behavior for the farmer.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from sqlalchemy import select

from app.modules.clients.models import ClientPromoter
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import dealer_decline_order
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


async def _seed_sent_order(db, *, with_facilitator: bool):
    """Seed a SENT order routed to a dealer. When with_facilitator
    is True, the order also has facilitator_user_id set — simulating
    a farmer → facilitator → dealer chain."""
    farmer = await make_user(db, name="Khaza Farmer")
    facilitator = await make_user(db, name="Sri Lakshmi", phone="+919800099001")
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
    if with_facilitator:
        db.add(ClientPromoter(
            client_id=client.id, user_id=facilitator.id,
            promoter_type="FACILITATOR", status="ACTIVE",
        ))

    order = Order(
        subscription_id=sub.id,
        farmer_user_id=farmer.id, client_id=client.id,
        dealer_user_id=dealer.id,
        facilitator_user_id=facilitator.id if with_facilitator else None,
        status=OrderStatus.SENT,
        date_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 1, tzinfo=timezone.utc),
        expires_at=datetime(2026, 6, 14, tzinfo=timezone.utc),
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id,
        practice_id=practice.id, timeline_id=timeline.id,
        status=OrderItemStatus.PENDING,
    ))
    await db.commit()
    return farmer, facilitator, dealer, order


@requires_docker
@pytest.mark.asyncio
async def test_decline_preserves_facilitator_when_routed_via_facilitator(db):
    """Source had facilitator_user_id set → new order keeps it +
    lands in ACCEPTED so the facilitator can forward to a new
    dealer without re-Accepting."""
    farmer, facilitator, dealer, order = await _seed_sent_order(
        db, with_facilitator=True,
    )
    result = await dealer_decline_order(
        order_id=order.id, db=db, current_user=dealer,
    )
    assert result["routed_back_to"] == "FACILITATOR"

    new_draft = (await db.execute(
        select(Order).where(Order.id == result["new_draft_order_id"])
    )).scalar_one()
    assert new_draft.facilitator_user_id == facilitator.id
    assert new_draft.dealer_user_id is None
    assert new_draft.status == OrderStatus.ACCEPTED


@requires_docker
@pytest.mark.asyncio
async def test_decline_drafts_to_farmer_when_no_facilitator(db):
    """Direct farmer → dealer case unchanged: new order is a DRAFT
    with both recipient fields cleared so the farmer picks a new
    recipient on the Manage tab."""
    farmer, _, dealer, order = await _seed_sent_order(
        db, with_facilitator=False,
    )
    result = await dealer_decline_order(
        order_id=order.id, db=db, current_user=dealer,
    )
    assert result["routed_back_to"] == "FARMER"

    new_draft = (await db.execute(
        select(Order).where(Order.id == result["new_draft_order_id"])
    )).scalar_one()
    assert new_draft.facilitator_user_id is None
    assert new_draft.dealer_user_id is None
    assert new_draft.status == OrderStatus.DRAFT
