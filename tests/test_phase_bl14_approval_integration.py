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


@requires_docker
@pytest.mark.asyncio
async def test_approve_all_items_runs_through_transition_validator(db):
    """Bulk approval still works end-to-end after the BL-14 batch 2
    consistency cleanup that wired validate_item_transition through
    approve_all_items. Pre-fix the route flipped status inline; now
    each item runs through the same per-item validator that
    approve_order_item uses, so the two approval paths can't drift."""
    from sqlalchemy import select
    from app.modules.orders.router import approve_all_items

    farmer, order, item = await _seed_farmer_order_with_item(
        db, OrderItemStatus.SENT_FOR_APPROVAL,
    )
    out = await approve_all_items(
        order_id=order.id, db=db, current_user=farmer,
    )
    assert out["approved_count"] == 1

    refreshed = (await db.execute(
        select(OrderItem).where(OrderItem.id == item.id)
    )).scalar_one()
    assert refreshed.status == OrderItemStatus.APPROVED


# ── FCM Batch 4: facilitator alert on submit_for_approval ────────────────────

async def _seed_pending_order_for_submit(db, *, with_facilitator: bool = True):
    """Seed an order in PROCESSING with one AVAILABLE item ready for
    submit_for_approval. Returns (farmer, dealer, facilitator, order)
    — facilitator may be None when with_facilitator=False."""
    farmer = await make_user(db, name="Farmer S")
    dealer = await make_user(db, name="Dealer S")
    facilitator = await make_user(db, name="Facilitator S") if with_facilitator else None
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    tl = await make_timeline(db, package, name="TL_S")
    practice = await make_practice(db, tl, l1="PESTICIDE", l2="MANCOZEB")
    await make_element(db, practice, value="2", unit_cosh_id="kg_per_acre")

    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id,
        client_id=client.id, dealer_user_id=dealer.id,
        facilitator_user_id=facilitator.id if facilitator else None,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        status=OrderStatus.PROCESSING,
    )
    db.add(order); await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        brand_cosh_id="brand:dithane-m45", brand_name="Dithane M-45",
        given_volume=5, volume_unit="kg", price=800,
        status=OrderItemStatus.AVAILABLE,
    )
    db.add(item)
    await db.commit()
    return farmer, dealer, facilitator, order


@requires_docker
@pytest.mark.asyncio
async def test_submit_for_approval_pushes_fcm_to_facilitator(db, monkeypatch):
    """BL-14 spec: when the dealer submits volumes/prices for farmer
    approval, the facilitator gets an FCM push with 'Your farmer needs
    to approve' so they can nudge the farmer if needed. The farmer
    drives the actual approve/reject through the PWA — FCM goes to
    the facilitator only."""
    from app.modules.orders import router as orders_router
    from app.modules.orders.router import submit_for_approval

    sent: list[tuple[str, str, str, dict]] = []

    async def fake_send_fcm(token, title, body, data=None):
        sent.append((token, title, body, data or {}))
        return True

    monkeypatch.setattr(orders_router, "send_fcm", fake_send_fcm)

    _, dealer, facilitator, order = await _seed_pending_order_for_submit(db)
    facilitator.fcm_token = "facilitator-token-xyz"
    await db.commit()

    await submit_for_approval(
        order_id=order.id, data={"items": {}},
        db=db, current_user=dealer,
    )

    assert len(sent) == 1
    token, title, body, data = sent[0]
    assert token == "facilitator-token-xyz"
    assert title == orders_router.SUBMIT_FOR_APPROVAL_FCM_TITLE
    assert "approve" in title.lower()  # spec phrasing
    assert "nudge" in body.lower()      # facilitator's role per the body
    assert data["type"] == "ORDER_AWAITING_FARMER_APPROVAL"
    assert data["order_id"] == order.id
    assert data["farmer_user_id"] == order.farmer_user_id


@requires_docker
@pytest.mark.asyncio
async def test_submit_for_approval_skips_fcm_when_no_facilitator(db, monkeypatch):
    """Direct dealer ↔ farmer flow with no facilitator routing the
    order: no FCM is fired (nothing to send to). The farmer-side
    approval path still works through the PWA."""
    from app.modules.orders import router as orders_router
    from app.modules.orders.router import submit_for_approval

    sent: list = []

    async def fake_send_fcm(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    monkeypatch.setattr(orders_router, "send_fcm", fake_send_fcm)

    _, dealer, _, order = await _seed_pending_order_for_submit(db, with_facilitator=False)

    await submit_for_approval(
        order_id=order.id, data={"items": {}},
        db=db, current_user=dealer,
    )
    assert sent == []


@requires_docker
@pytest.mark.asyncio
async def test_submit_for_approval_skips_fcm_when_facilitator_has_no_token(
    db, monkeypatch,
):
    """Facilitator is assigned but hasn't registered an FCM token
    (most facilitators in V1 until the PWA wires registration).
    Submit still succeeds; no FCM call attempted."""
    from app.modules.orders import router as orders_router
    from app.modules.orders.router import submit_for_approval

    sent: list = []

    async def fake_send_fcm(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    monkeypatch.setattr(orders_router, "send_fcm", fake_send_fcm)

    _, dealer, facilitator, order = await _seed_pending_order_for_submit(db)
    # facilitator.fcm_token defaults to None — leave unset.
    assert facilitator.fcm_token is None

    out = await submit_for_approval(
        order_id=order.id, data={"items": {}},
        db=db, current_user=dealer,
    )
    assert out["status"] == OrderStatus.SENT_FOR_APPROVAL
    assert sent == []
