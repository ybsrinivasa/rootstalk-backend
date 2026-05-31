"""Orders V2 Batch 24 — farmer-context block on dealer order detail.

User narrative (2026-05-31): the dealer needs farmer name, phone,
crop name, crop age, and acreage/plant count to decide how to
process the order. Crop-age semantics:
- plant-wise (planting_year set): age in YEARS = today.year - planting_year
- area-wise (crop_start_date set): age in DAYS = today - crop_start_date
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import get_dealer_order
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


async def _order_with_dealer(db, *, plant_wise=False):
    farmer = await make_user(db, name="Sanjay")
    farmer.phone = "+919900112233"
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    if plant_wise:
        sub.number_of_plants = 120
        sub.planting_year = 2019
        sub.crop_start_date = datetime.now(timezone.utc)  # plant-wise can still have one
    else:
        sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=25)
        sub.farm_area_acres = 2.5
    await db.commit()
    tl = await make_timeline(
        db, pkg, name="TL_ctx",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()
    dealer = await make_onboarded_dealer(db, name="D-Ctx")
    await db.commit()
    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.PROCESSING, dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=p.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    ))
    await db.commit()
    return farmer, dealer, order


@requires_docker
@pytest.mark.asyncio
async def test_farmer_context_area_wise(db):
    farmer, dealer, order = await _order_with_dealer(db, plant_wise=False)
    res = await get_dealer_order(order_id=order.id, db=db, current_user=dealer)
    ctx = res["farmer_context"]
    assert ctx["farmer_name"] == "Sanjay"
    assert ctx["farmer_phone"] == "+919900112233"
    assert ctx["measure"] == "AREA_WISE"
    assert ctx["age_unit"] == "days"
    assert 20 <= ctx["age_value"] <= 30
    assert ctx["farm_area_acres"] == 2.5
    assert ctx["number_of_plants"] is None


@requires_docker
@pytest.mark.asyncio
async def test_farmer_context_plant_wise(db):
    farmer, dealer, order = await _order_with_dealer(db, plant_wise=True)
    res = await get_dealer_order(order_id=order.id, db=db, current_user=dealer)
    ctx = res["farmer_context"]
    assert ctx["measure"] == "PLANT_WISE"
    assert ctx["age_unit"] == "years"
    # 2026 (test year) - 2019 = 7. Test is year-stable as long as we're past 2019.
    assert ctx["age_value"] >= 7
    assert ctx["number_of_plants"] == 120
    assert ctx["farm_area_acres"] is None
