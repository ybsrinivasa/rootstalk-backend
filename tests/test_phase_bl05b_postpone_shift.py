"""Orders V2 Batch 16 — BL-05b step 7: postponed_until shifts on start_date change.

The 2026-05-31 audit caught a pre-existing gap: when the farmer
changes crop_start_date, BL-05b step 7 says any dealer-postponed
item whose timeline shifted must also have its `postponed_until`
shifted by the same delta_days. Without this, the postpone-expiry
sweep flips items too early / too late after a start-date change.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.subscriptions.router import set_start_date
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


async def _sub_order_with_postponed_item(db, *, days_to_postpone=4):
    user = await make_user(db, name="Farmer Shift")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) + timedelta(days=5)
    sub.crop_start_date_first_set_at = datetime.now(timezone.utc)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_shift",
        from_type=TimelineFromType.DAS, from_value=10, to_value=30,
    )
    p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()

    dealer = await make_onboarded_dealer(db, name="D-Shift")
    await db.commit()

    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=30),
        status=OrderStatus.PROCESSING,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=p.id, timeline_id=tl.id,
        status=OrderItemStatus.POSTPONED,
        postponed_until=datetime.now(timezone.utc) + timedelta(days=days_to_postpone),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return user, sub, order, item


@requires_docker
@pytest.mark.asyncio
async def test_postponed_until_shifts_when_start_date_advances(db):
    """Advancing start_date by 3 days should shift POSTPONED items'
    postponed_until forward by 3 days."""
    user, sub, _, item = await _sub_order_with_postponed_item(db)
    original_until = item.postponed_until

    new_start = sub.crop_start_date + timedelta(days=3)
    await set_start_date(
        subscription_id=sub.id,
        data={"crop_start_date": new_start.isoformat()},
        db=db, current_user=user,
    )

    await db.refresh(item)
    expected = original_until + timedelta(days=3)
    delta = abs((item.postponed_until - expected).total_seconds())
    assert delta < 60, f"postponed_until off by {delta}s — should shift by exactly +3 days"


@requires_docker
@pytest.mark.asyncio
async def test_postponed_until_shifts_when_start_date_retreats(db):
    """Pulling start_date back by 2 days should shift postpone back too."""
    user, sub, _, item = await _sub_order_with_postponed_item(db, days_to_postpone=8)
    original_until = item.postponed_until

    new_start = sub.crop_start_date - timedelta(days=2)
    await set_start_date(
        subscription_id=sub.id,
        data={"crop_start_date": new_start.isoformat()},
        db=db, current_user=user,
    )

    await db.refresh(item)
    expected = original_until - timedelta(days=2)
    delta = abs((item.postponed_until - expected).total_seconds())
    assert delta < 60
