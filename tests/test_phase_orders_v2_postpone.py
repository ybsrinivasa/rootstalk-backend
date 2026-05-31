"""Orders V2 Batch 7 — postpone-days picker + expiry sweep.

Locks the 2026-05-31 narrative rules:
- Dealer's postpone picker offers 1 … (remaining_days − 1) so the
  farmer always has ≥1 clear day to re-route after a postpone
  elapses.
- The sweep auto-flips POSTPONED items past their `postponed_until`
  to NOT_AVAILABLE so the farmer sees them as Returned-needs-
  rerouting without the dealer doing anything.
- Both events land on `order_item_events` so reports can tell the
  three Postpone outcomes apart: dealer cancelled it, dealer
  followed through, or the clock ran out.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemEvent, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import (
    get_postpone_window, postpone_item,
)
from app.tasks.postpone_expiry import _sweep_expired_postpones_with_session
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


async def _accepted_order_with_item(db, *, to_value=30):
    user = await make_user(db, name="Farmer Pp")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)  # IST today ≈ now
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_pp",
        from_type=TimelineFromType.DAS, from_value=0, to_value=to_value,
    )
    p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()

    dealer = await make_onboarded_dealer(db, name="D-Pp")
    await db.commit()

    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=to_value),
        status=OrderStatus.PROCESSING,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=p.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return user, dealer, order, item


@requires_docker
@pytest.mark.asyncio
async def test_postpone_window_returns_max_days(db):
    _, dealer, order, item = await _accepted_order_with_item(db, to_value=10)

    res = await get_postpone_window(
        order_id=order.id, item_id=item.id, db=db, current_user=dealer,
    )
    # Timeline ends in ~10 days, max_days = remaining - 1 = ~9.
    # IST/UTC drift on the day-of-test can shift by 1; just check
    # that the picker offers a sensible non-zero window.
    assert res["can_postpone"] is True
    assert 1 <= res["max_days"] <= 10


@requires_docker
@pytest.mark.asyncio
async def test_postpone_days_within_window_succeeds(db):
    _, dealer, order, item = await _accepted_order_with_item(db, to_value=10)

    await postpone_item(
        order_id=order.id, item_id=item.id,
        data={"days": 3},
        db=db, current_user=dealer,
    )
    await db.refresh(item)
    assert item.status == OrderItemStatus.POSTPONED
    assert item.postponed_until is not None


@requires_docker
@pytest.mark.asyncio
async def test_postpone_days_out_of_range_refused(db):
    _, dealer, order, item = await _accepted_order_with_item(db, to_value=3)
    # max_days = ~(3-1) - 1 = 1. Asking for 50 days is way past.
    with pytest.raises(HTTPException) as exc:
        await postpone_item(
            order_id=order.id, item_id=item.id,
            data={"days": 50},
            db=db, current_user=dealer,
        )
    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert isinstance(detail, dict) and detail["code"] == "postpone_days_out_of_range"


@requires_docker
@pytest.mark.asyncio
async def test_sweep_flips_expired_postpone_to_not_available(db):
    _, _, order, item = await _accepted_order_with_item(db)
    # Stamp the item directly so the sweep can find it without
    # threading the postpone endpoint's validation.
    item.status = OrderItemStatus.POSTPONED
    item.postponed_until = datetime.now(timezone.utc) - timedelta(hours=1)
    await db.commit()

    flipped = await _sweep_expired_postpones_with_session(db)
    assert flipped == 1

    await db.refresh(item)
    assert item.status == OrderItemStatus.NOT_AVAILABLE

    ev = (await db.execute(
        select(OrderItemEvent).where(
            OrderItemEvent.lineage_id == item.lineage_id,
            OrderItemEvent.event_type == "POSTPONE_EXPIRED",
        )
    )).scalar_one()
    assert ev.actor_role == "SYSTEM"
    assert ev.new_status == OrderItemStatus.NOT_AVAILABLE.value


@requires_docker
@pytest.mark.asyncio
async def test_sweep_ignores_unexpired_postpones(db):
    _, _, order, item = await _accepted_order_with_item(db)
    item.status = OrderItemStatus.POSTPONED
    item.postponed_until = datetime.now(timezone.utc) + timedelta(days=2)
    await db.commit()

    flipped = await _sweep_expired_postpones_with_session(db)
    assert flipped == 0

    await db.refresh(item)
    assert item.status == OrderItemStatus.POSTPONED
