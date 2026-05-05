"""BL-10 audit — DB-backed integration tests pinning the seven
endpoint fixes from batch 2.

Pure-function coverage of the state machine lives in
`tests/test_bl10.py` (19 tests). This file drives the FastAPI route
handlers directly, with seeded rows in the testcontainer DB, to
verify the ownership and transition guards land correctly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import (
    abort_order, approve_order_item, mark_item_available,
    mark_item_unavailable, postpone_item, reject_order_item,
    submit_for_approval,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


async def _seed_order_with_item(
    db, *, item_status: OrderItemStatus = OrderItemStatus.PENDING,
    order_status: OrderStatus = OrderStatus.PROCESSING,
):
    """Seed farmer + dealer + sub + practice + Order + one OrderItem in
    the requested statuses. Returns (order, item, farmer, dealer)."""
    farmer = await make_user(db, name="Farmer A")
    dealer = await make_user(db, name="Dealer A")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    tl = await make_timeline(db, package, name="TL_BL10")
    practice = await make_practice(db, tl, l1="PESTICIDE", l2="MANCOZEB")
    await make_element(db, practice, value="2", unit_cosh_id="kg_per_acre")

    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id,
        client_id=client.id, dealer_user_id=dealer.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        status=order_status,
    )
    db.add(order); await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        status=item_status,
    )
    db.add(item)
    await db.commit()
    return order, item, farmer, dealer


# ── Privilege gaps closed ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_approve_item_rejects_other_farmer(db):
    """Farmer B cannot approve an item on farmer A's order."""
    order, item, _, _ = await _seed_order_with_item(
        db,
        item_status=OrderItemStatus.SENT_FOR_APPROVAL,
        order_status=OrderStatus.SENT_FOR_APPROVAL,
    )
    farmer_b = await make_user(db, name="Outsider")
    with pytest.raises(HTTPException) as exc:
        await approve_order_item(
            order_id=order.id, item_id=item.id,
            db=db, current_user=farmer_b,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_reject_item_rejects_other_farmer(db):
    order, item, _, _ = await _seed_order_with_item(
        db,
        item_status=OrderItemStatus.SENT_FOR_APPROVAL,
        order_status=OrderStatus.SENT_FOR_APPROVAL,
    )
    farmer_b = await make_user(db, name="Outsider")
    with pytest.raises(HTTPException) as exc:
        await reject_order_item(
            order_id=order.id, item_id=item.id,
            db=db, current_user=farmer_b,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_mark_available_rejects_other_dealer(db):
    """Dealer B cannot mark items available on dealer A's order."""
    order, item, _, _ = await _seed_order_with_item(db)
    dealer_b = await make_user(db, name="Other Dealer")
    with pytest.raises(HTTPException) as exc:
        await mark_item_available(
            order_id=order.id, item_id=item.id,
            data={"brand_cosh_id": "anything", "given_volume": 5},
            db=db, current_user=dealer_b,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_postpone_rejects_other_dealer(db):
    order, item, _, _ = await _seed_order_with_item(db)
    dealer_b = await make_user(db, name="Other Dealer")
    with pytest.raises(HTTPException) as exc:
        await postpone_item(
            order_id=order.id, item_id=item.id,
            data={"postponed_until": None},
            db=db, current_user=dealer_b,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_mark_unavailable_rejects_other_dealer(db):
    order, item, _, _ = await _seed_order_with_item(db)
    dealer_b = await make_user(db, name="Other Dealer")
    with pytest.raises(HTTPException) as exc:
        await mark_item_unavailable(
            order_id=order.id, item_id=item.id,
            db=db, current_user=dealer_b,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_submit_for_approval_rejects_other_dealer(db):
    order, _, _, _ = await _seed_order_with_item(db)
    dealer_b = await make_user(db, name="Other Dealer")
    with pytest.raises(HTTPException) as exc:
        await submit_for_approval(
            order_id=order.id, data={"items": {}},
            db=db, current_user=dealer_b,
        )
    assert exc.value.status_code == 404


# ── State transition guards ───────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_reject_item_blocks_already_approved(db):
    """Pre-fix the live route accepted ANY current status. Now it must
    refuse APPROVED → REJECTED."""
    order, item, farmer, _ = await _seed_order_with_item(
        db,
        item_status=OrderItemStatus.APPROVED,
        order_status=OrderStatus.PARTIALLY_APPROVED,
    )
    with pytest.raises(HTTPException) as exc:
        await reject_order_item(
            order_id=order.id, item_id=item.id,
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "ILLEGAL_TRANSITION"


@requires_docker
@pytest.mark.asyncio
async def test_mark_available_blocks_already_removed_item(db):
    """A REMOVED item must not be flippable back to AVAILABLE."""
    order, item, _, dealer = await _seed_order_with_item(
        db, item_status=OrderItemStatus.REMOVED,
    )
    with pytest.raises(HTTPException) as exc:
        await mark_item_available(
            order_id=order.id, item_id=item.id,
            data={"brand_cosh_id": "anything", "given_volume": 5},
            db=db, current_user=dealer,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "ILLEGAL_TRANSITION"


# ── Abort policy ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_abort_rejects_cancelled_order(db):
    """Pre-fix abort rewrote any order's status to SENT regardless of
    current value, so a CANCELLED order could be resurrected. Now
    blocked with a stable error_code."""
    order, _, _, dealer = await _seed_order_with_item(
        db, order_status=OrderStatus.CANCELLED,
    )
    with pytest.raises(HTTPException) as exc:
        await abort_order(
            order_id=order.id, db=db, current_user=dealer,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "ORDER_NOT_ABORTABLE"


@requires_docker
@pytest.mark.asyncio
async def test_abort_preserves_approved_items_and_clears_stale_fields(db):
    """The most consequential fix: an abort on a PARTIALLY_APPROVED
    order must NOT erase the farmer's prior approvals. APPROVED stays
    APPROVED; an AVAILABLE item is reset to PENDING with all of its
    fulfilment fields nulled (brand + volume + price + postponed +
    scan_verified, not just brand)."""
    order, item_available, _, dealer = await _seed_order_with_item(
        db,
        item_status=OrderItemStatus.AVAILABLE,
        order_status=OrderStatus.PARTIALLY_APPROVED,
    )
    # Stale fulfilment values that the abort must clear.
    item_available.brand_cosh_id = "brand:something"
    item_available.brand_name = "Stale Brand"
    item_available.given_volume = 5
    item_available.volume_unit = "kg"
    item_available.price = 800
    item_available.scan_verified = True
    # Also seed an APPROVED sibling item that abort must NOT touch.
    item_approved = OrderItem(
        order_id=order.id, practice_id=item_available.practice_id,
        timeline_id=item_available.timeline_id,
        brand_cosh_id="brand:approved", brand_name="Approved Brand",
        given_volume=3, volume_unit="L", price=500,
        status=OrderItemStatus.APPROVED,
    )
    db.add(item_approved)
    await db.commit()

    out = await abort_order(
        order_id=order.id, db=db, current_user=dealer,
    )
    assert out["status"] == OrderStatus.SENT

    # Refresh from DB.
    refreshed_avail = (await db.execute(
        select(OrderItem).where(OrderItem.id == item_available.id)
    )).scalar_one()
    refreshed_appr = (await db.execute(
        select(OrderItem).where(OrderItem.id == item_approved.id)
    )).scalar_one()

    # AVAILABLE → PENDING with every fulfilment field cleared.
    assert refreshed_avail.status == OrderItemStatus.PENDING
    assert refreshed_avail.brand_cosh_id is None
    assert refreshed_avail.brand_name is None
    assert refreshed_avail.given_volume is None
    assert refreshed_avail.volume_unit is None
    assert refreshed_avail.price is None
    assert refreshed_avail.scan_verified is False

    # APPROVED item untouched — neither status nor any field.
    assert refreshed_appr.status == OrderItemStatus.APPROVED
    assert refreshed_appr.brand_cosh_id == "brand:approved"
    assert float(refreshed_appr.given_volume) == 3.0
    assert float(refreshed_appr.price) == 500
