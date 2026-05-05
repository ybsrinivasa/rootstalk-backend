"""Phase D.2 — BL-06 5-key lookup integration tests.

Seeds a real Subscription + Order + OrderItem + Practice + Element +
Timeline + Package + crop_measures + volume_formulas, then calls the
live `get_volume_estimate` route. Verifies:
  - Crop measure missing → CROP_MEASURE_MISSING error code, no estimate.
  - All 5 keys filter correctly: a matching formula → numeric estimate.
  - Caller can override brand_unit/dosage_unit via query params.
  - Multiple matching formulas → FORMULA_DUPLICATE error code.
  - No matching formula → FORMULA_NOT_FOUND error code with diagnostics.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.modules.advisory.models import (
    Element, Package, Practice, PracticeL0, PackageStatus, PackageType,
    Timeline, TimelineFromType,
)
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import get_volume_estimate
from app.modules.sync.models import VolumeFormula
from app.services.crop_measure import AREA_WISE, PLANT_WISE, set_measure
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)


CROP = "crop:tomato"


async def _seed_volume_estimate_scenario(
    db, *,
    measure: str,
    formula_text: str,
    application_method: str = "Foliar Spray",
    brand_unit: str = "kg",
    dosage_unit: str = "g/L",
    dosage_value: str = "2",
    seed_crop_measure: bool = True,
):
    """Build the full chain Subscription → Order → OrderItem → Practice
    → Timeline → Package + crop_measure + a single matching VolumeFormula.
    """
    user = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    package.crop_cosh_id = CROP
    sub = await make_subscription(
        db, farmer=user, client=client, package=package,
    )
    sub.farm_area_acres = 5.0
    await db.commit()

    tl = Timeline(
        package_id=package.id, name="TL_volcalc",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
        display_order=0,
    )
    db.add(tl)
    await db.flush()

    practice = Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="FERTILIZER", l2_type="Chemical Pesticides",
        display_order=0,
    )
    db.add(practice)
    await db.flush()

    db.add(Element(
        practice_id=practice.id,
        element_type="dosage", value=dosage_value, unit_cosh_id=dosage_unit,
    ))
    db.add(Element(
        practice_id=practice.id,
        element_type="application_method", value=application_method,
    ))

    order = Order(
        subscription_id=sub.id,
        farmer_user_id=user.id,
        client_id=client.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        status=OrderStatus.SENT,
    )
    db.add(order)
    await db.flush()

    item = OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING, volume_unit=brand_unit,
    )
    db.add(item)

    # Seed crop measure (the gate).
    if seed_crop_measure:
        await set_measure(db, crop_cosh_id=CROP, measure=measure)

    # Seed a matching volume_formula row.
    db.add(VolumeFormula(
        measure=measure,
        l2_practice="Chemical Pesticides",
        application_method=application_method,
        brand_unit=brand_unit, dosage_unit=dosage_unit,
        formula=formula_text,
        status="ACTIVE",
    ))
    await db.commit()

    return user, order, item


@requires_docker
@pytest.mark.asyncio
async def test_estimate_returns_value_with_5key_lookup(db):
    """Seeded Foliar Spray formula `(Dosage × 150 × Total_area)/1000` —
    Dosage 2 g/L, 5 acres → (2 × 150 × 5)/1000 = 1.5 kg."""
    user, order, item = await _seed_volume_estimate_scenario(
        db, measure=AREA_WISE,
        formula_text="(Dosage × 150 × Total_area)/1000",
    )
    out = await get_volume_estimate(
        order_id=order.id, item_id=item.id,
        db=db, current_user=user,
    )
    assert out["estimated_volume"] == 1.5
    assert out["volume_unit"] == "kg"
    assert out["lookup_key"]["measure"] == AREA_WISE
    assert out["lookup_key"]["application_method"] == "Foliar Spray"
    assert out["lookup_key"]["brand_unit"] == "kg"
    assert out["lookup_key"]["dosage_unit"] == "g/L"


@requires_docker
@pytest.mark.asyncio
async def test_estimate_blocks_when_crop_measure_missing(db):
    user, order, item = await _seed_volume_estimate_scenario(
        db, measure=AREA_WISE,
        formula_text="Dosage × Total_area",
        seed_crop_measure=False,
    )
    out = await get_volume_estimate(
        order_id=order.id, item_id=item.id,
        db=db, current_user=user,
    )
    assert out["estimated_volume"] is None
    assert out.get("error_code") == "CROP_MEASURE_MISSING"


@requires_docker
@pytest.mark.asyncio
async def test_estimate_404_on_unknown_formula(db):
    """Seed measure but no matching VolumeFormula row → FORMULA_NOT_FOUND."""
    user, order, item = await _seed_volume_estimate_scenario(
        db, measure=AREA_WISE,
        formula_text="Dosage × Total_area",
        application_method="Soil Drenching",   # formula seeded for this method
    )
    # Caller asks for a different brand_unit that no formula matches.
    out = await get_volume_estimate(
        order_id=order.id, item_id=item.id,
        brand_unit="numbers",  # no matching row
        db=db, current_user=user,
    )
    assert out["estimated_volume"] is None
    assert out.get("error_code") == "FORMULA_NOT_FOUND"
    assert "brand_unit=numbers" in out["message"]


@requires_docker
@pytest.mark.asyncio
async def test_estimate_caller_override_for_brand_unit(db):
    """Same scenario but caller overrides brand_unit to switch which
    formula matches. Both rows present, override picks one."""
    user, order, item = await _seed_volume_estimate_scenario(
        db, measure=AREA_WISE,
        formula_text="Dosage × Total_area",     # kg+g/L row, but using kg result
        application_method="Soil Drenching",
        brand_unit="kg", dosage_unit="g/L",
    )
    # Add a second formula row with a different brand_unit so override is meaningful.
    db.add(VolumeFormula(
        measure=AREA_WISE, l2_practice="Chemical Pesticides",
        application_method="Soil Drenching",
        brand_unit="L", dosage_unit="g/L",
        formula="Dosage × 999 × Total_area",  # distinguishably different
        status="ACTIVE",
    ))
    await db.commit()

    out = await get_volume_estimate(
        order_id=order.id, item_id=item.id,
        brand_unit="L",   # override
        db=db, current_user=user,
    )
    assert out["estimated_volume"] is not None
    assert "999" in out["formula_used"]
    assert out["volume_unit"] == "L"


@requires_docker
@pytest.mark.asyncio
async def test_estimate_blocks_on_duplicate_formulas(db):
    """Two ACTIVE rows with identical 5-key combination → FORMULA_DUPLICATE."""
    user, order, item = await _seed_volume_estimate_scenario(
        db, measure=AREA_WISE,
        formula_text="Dosage × Total_area",
    )
    # Insert a second row with the same key.
    db.add(VolumeFormula(
        measure=AREA_WISE, l2_practice="Chemical Pesticides",
        application_method="Foliar Spray",
        brand_unit="kg", dosage_unit="g/L",
        formula="Dosage × Total_area",
        status="ACTIVE",
    ))
    await db.commit()

    out = await get_volume_estimate(
        order_id=order.id, item_id=item.id,
        db=db, current_user=user,
    )
    assert out["estimated_volume"] is None
    assert out.get("error_code") == "FORMULA_DUPLICATE"


@requires_docker
@pytest.mark.asyncio
async def test_estimate_uses_applications_element_when_present(db):
    """When the practice has an `applications` element (Phase D.3), its
    value drives the formula's Applications variable — not a runtime
    re-compute from frequency_days. Element wins."""
    user, order, item = await _seed_volume_estimate_scenario(
        db, measure=AREA_WISE,
        formula_text="Dosage × Total_area × Applications",
    )
    # The seeded practice has dosage + application_method elements only.
    # Add an applications element with value=3 and tag the practice as
    # frequency-based (would have computed to a different number).
    from app.modules.advisory.models import Element, Practice
    from sqlalchemy import select as _sel
    practice = (await db.execute(
        _sel(Practice).where(Practice.id == item.practice_id)
    )).scalar_one()
    practice.frequency_days = 2  # would compute to ceil(31/2) = 16
    db.add(Element(
        practice_id=practice.id, element_type="applications", value="3",
    ))
    await db.commit()

    out = await get_volume_estimate(
        order_id=order.id, item_id=item.id,
        db=db, current_user=user,
    )
    # 2 (dosage) × 5 (acres) × 3 (applications from element) = 30
    assert out["estimated_volume"] == 30.0
    assert out["volume_unit"] == "kg"


@requires_docker
@pytest.mark.asyncio
async def test_estimate_blocks_when_application_method_missing(db):
    """Practice has no application_method element → APPLICATION_METHOD_MISSING."""
    user = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    package.crop_cosh_id = CROP
    sub = await make_subscription(db, farmer=user, client=client, package=package)
    sub.farm_area_acres = 5.0
    await db.commit()

    tl = Timeline(
        package_id=package.id, name="TL", from_type=TimelineFromType.DAS,
        from_value=0, to_value=30,
    )
    db.add(tl); await db.flush()
    practice = Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="FERTILIZER", l2_type="Chemical Pesticides",
    )
    db.add(practice); await db.flush()
    # ONLY a dosage element — no application_method element.
    db.add(Element(
        practice_id=practice.id, element_type="dosage",
        value="2", unit_cosh_id="g/L",
    ))
    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        status=OrderStatus.SENT,
    )
    db.add(order); await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING, volume_unit="kg",
    )
    db.add(item)
    await set_measure(db, crop_cosh_id=CROP, measure=AREA_WISE)
    await db.commit()

    out = await get_volume_estimate(
        order_id=order.id, item_id=item.id, db=db, current_user=user,
    )
    assert out["estimated_volume"] is None
    assert out.get("error_code") == "APPLICATION_METHOD_MISSING"
