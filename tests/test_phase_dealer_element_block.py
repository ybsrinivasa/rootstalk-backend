"""Orders V2 Batch 26 — post-brand-selection element block.

User spec (2026-05-31): after the dealer picks a brand, show
Recommended Dosage + Unit, Application Method, and (plant-wise
only) Volume per Plant + Unit. All four come from the SE's
practice elements; the dealer reads them as guidance for the
volume entry below.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import get_dealer_order
from app.modules.sync.models import CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_onboarded_dealer, make_package,
    make_practice, make_subscription, make_timeline, make_user,
)


async def _seed_order_with_practice_elements(db, *, with_vol_per_plant=False):
    # Cosh cores for dosage unit + application method
    db.add(CoshCoreItem(
        cosh_id="cosh:du-mlperL", core_type="dosage_unit",
        parent_cosh_id=None, translations={"en": "ml/L"},
        status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="cosh:am-foliar", core_type="application_method",
        parent_cosh_id=None, translations={"en": "Foliar Spray"},
        status="active",
    ))
    if with_vol_per_plant:
        db.add(CoshCoreItem(
            cosh_id="cosh:vu-litres", core_type="volume_unit",
            parent_cosh_id=None, translations={"en": "Litres"},
            status="active",
        ))
    await db.commit()

    farmer = await make_user(db, name="Farmer EB")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=5)
    if with_vol_per_plant:
        sub.number_of_plants = 50
        sub.planting_year = 2020
    else:
        sub.farm_area_acres = 1.5
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_EB",
        from_type=TimelineFromType.DAS, from_value=0, to_value=20,
    )
    p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p, element_type="DOSAGE", value="2", unit_cosh_id=None, cosh_ref=None)
    await make_element(db, p, element_type="DOSAGE_UNIT", value=None, unit_cosh_id=None, cosh_ref="cosh:du-mlperL")
    await make_element(db, p, element_type="APPLICATION_METHOD", value=None, unit_cosh_id=None, cosh_ref="cosh:am-foliar")
    if with_vol_per_plant:
        await make_element(db, p, element_type="VOLUME_PER_PLANT", value="0.25", unit_cosh_id=None, cosh_ref=None)
        await make_element(db, p, element_type="VOLUME_PER_PLANT_UNIT", value=None, unit_cosh_id=None, cosh_ref="cosh:vu-litres")
    await db.commit()

    dealer = await make_onboarded_dealer(db, name="D-EB")
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
    return dealer, order


@requires_docker
@pytest.mark.asyncio
async def test_element_block_dosage_and_app_method(db):
    dealer, order = await _seed_order_with_practice_elements(db)
    res = await get_dealer_order(order_id=order.id, db=db, current_user=dealer)
    item = res["items"][0]
    block = item["element_block"]
    assert block["dosage_value"] == 2.0
    assert block["dosage_unit_name"] == "ml/L"
    assert block["application_method_name"] == "Foliar Spray"
    # No plant-wise extras — vol_per_plant fields are null.
    assert block["vol_per_plant_value"] is None
    assert block["vol_per_plant_unit_name"] is None


@requires_docker
@pytest.mark.asyncio
async def test_element_block_vol_per_plant_plantwise(db):
    dealer, order = await _seed_order_with_practice_elements(db, with_vol_per_plant=True)
    res = await get_dealer_order(order_id=order.id, db=db, current_user=dealer)
    block = res["items"][0]["element_block"]
    assert block["vol_per_plant_value"] == 0.25
    assert block["vol_per_plant_unit_name"] == "Litres"
