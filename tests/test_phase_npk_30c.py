"""Orders V2 Batch 30C — three-group brand picker, fertigation
frequency multiplier, brand consolidation.

Reuses the seed helpers from `test_phase_npk_select` to stay
focused on the new behaviour.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    DealerProfile, DealerRelationship,
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import (
    get_dealer_order, get_item_npk_options, get_npk_trade_names,
    npk_select,
)
from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_onboarded_dealer, make_package,
    make_practice, make_subscription, make_timeline, make_user,
)
from tests.test_phase_npk_select import (
    _add_concentration, _add_fertiliser, _add_trade_name,
    _certify_for_fertigation, _seed_lookup_cores,
    COMPLEX, STRAIGHT,
)


async def _seed_basic_fixture(db, *, fertigation_certify=False):
    await _seed_lookup_cores(db)
    seen: set = set()
    _add_fertiliser(db, cn_cosh_id="cosh:cn-10-26-26", en_name="NPK 10:26:26", n=10, p=26, k=26, seen_conc=seen)
    _add_fertiliser(db, cn_cosh_id="cosh:cn-urea", en_name="Urea", n=46, sc=STRAIGHT, seen_conc=seen)
    _add_fertiliser(db, cn_cosh_id="cosh:cn-ssp",  en_name="SSP",  p=16, sc=STRAIGHT, seen_conc=seen)
    _add_fertiliser(db, cn_cosh_id="cosh:cn-mop",  en_name="MOP",  k=60, sc=STRAIGHT, seen_conc=seen)

    # Two brands for 10:26:26, one each for the rest. Mfr names match
    # the dealer's onboarded relationships for grouping tests below.
    _add_trade_name(db, tn_cosh_id="cosh:tn-iffco-npk", en_name="Iffco NPK 10:26:26",
                    mfr_cosh_id="cosh:mfr-iffco", common_name_cosh_id="cosh:cn-10-26-26")
    _add_trade_name(db, tn_cosh_id="cosh:tn-acme-npk", en_name="Acme NPK 10:26:26",
                    mfr_cosh_id="cosh:mfr-acme",  common_name_cosh_id="cosh:cn-10-26-26")
    _add_trade_name(db, tn_cosh_id="cosh:tn-gsfc-urea", en_name="GSFC Urea",
                    mfr_cosh_id="cosh:mfr-gsfc", common_name_cosh_id="cosh:cn-urea")

    # Manufacturer Cores so group_trade_names_for_dealer can resolve names.
    for mid, en in (("cosh:mfr-iffco", "Iffco"),
                    ("cosh:mfr-acme", "AcmeCo"),
                    ("cosh:mfr-gsfc", "GSFC")):
        db.add(CoshCoreItem(
            cosh_id=mid, core_type="input_manufacturers", parent_cosh_id=None,
            translations={"en": en}, status="active",
        ))

    if fertigation_certify:
        _certify_for_fertigation(db, common_name_cosh_id="cosh:cn-10-26-26",
                                  trade_name_cosh_id="cosh:tn-iffco-npk")
        _certify_for_fertigation(db, common_name_cosh_id="cosh:cn-urea",
                                  trade_name_cosh_id="cosh:tn-gsfc-urea")
    await db.commit()


async def _seed_order_with_npk(
    db, *, l2_type, dose, applications=None, fertigation_certify=False,
    dealer_mfrs: list[str] | None = None,
):
    await _seed_basic_fixture(db, fertigation_certify=fertigation_certify)

    farmer = await make_user(db, name="F-30c")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.farm_area_acres = 1.0
    await db.commit()
    tl = await make_timeline(
        db, pkg, name="TL_30c",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2=l2_type,
    )
    await make_element(db, p, element_type="N_DOSAGE", value=str(dose[0]), unit_cosh_id=None, cosh_ref=None)
    await make_element(db, p, element_type="P_DOSAGE", value=str(dose[1]), unit_cosh_id=None, cosh_ref=None)
    await make_element(db, p, element_type="K_DOSAGE", value=str(dose[2]), unit_cosh_id=None, cosh_ref=None)
    if applications:
        await make_element(db, p, element_type="applications", value=str(applications), unit_cosh_id=None, cosh_ref=None)
    await db.commit()

    dealer = await make_onboarded_dealer(db, name="D-30c")
    # Profile required for DealerRelationship to be meaningful.
    db.add(DealerProfile(
        user_id=dealer.id, shop_name="S", sell_categories=["FERTILIZERS"],
        shop_gps_lat=12.0, shop_gps_lng=77.0,
    ))
    for mfr in dealer_mfrs or []:
        db.add(DealerRelationship(
            dealer_user_id=dealer.id, manufacturer_name=mfr, status="ACTIVE",
        ))
    await db.commit()

    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id, client_id=client.id,
        category="FERTILIZER",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.PROCESSING, dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=p.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    )
    db.add(item)
    await db.commit()
    return dealer, order, item


# ── (3) three-group brand picker ──────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_trade_names_grouped_my_vs_other_for_chemical(db):
    """Dealer onboarded with AcmeCo → Acme brand in My Brands,
    Iffco in Other. Recommended stays empty for NPK."""
    dealer, order, item = await _seed_order_with_npk(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 80, 30),
        dealer_mfrs=["AcmeCo"],
    )
    res = await get_npk_trade_names(
        order_id=order.id, item_id=item.id,
        common_name_cosh_id="cosh:cn-10-26-26",
        db=db, current_user=dealer,
    )
    assert res["group_recommended"] == []
    my_ids = {b["cosh_id"] for b in res["group_my"]}
    other_ids = {b["cosh_id"] for b in res["group_other"]}
    assert my_ids == {"cosh:tn-acme-npk"}
    assert other_ids == {"cosh:tn-iffco-npk"}
    # Original flat list still present for backwards compat.
    assert len(res["trade_names"]) == 2


@requires_docker
@pytest.mark.asyncio
async def test_groups_with_no_onboarding_all_in_other(db):
    """Dealer with no DealerRelationship → every brand falls into Other."""
    dealer, order, item = await _seed_order_with_npk(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 80, 30),
    )
    res = await get_npk_trade_names(
        order_id=order.id, item_id=item.id,
        common_name_cosh_id="cosh:cn-10-26-26",
        db=db, current_user=dealer,
    )
    assert res["group_my"] == []
    assert len(res["group_other"]) == 2


# ── (2) fertigation frequency multiplier ──────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_options_exposes_applications_multiplier(db):
    dealer, order, item = await _seed_order_with_npk(
        db, l2_type="FERTIGATION_NPK_DOSAGES", dose=(5, 5, 5),
        applications=12, fertigation_certify=True,
    )
    res = await get_item_npk_options(
        order_id=order.id, item_id=item.id,
        db=db, current_user=dealer,
    )
    assert res["fertigation"] is True
    assert res["applications_multiplier"] == 12


@requires_docker
@pytest.mark.asyncio
async def test_select_multiplies_kg_by_applications(db):
    """Pure Straight pick × 12 apps. Urea 46:0:0 → 5 kg N target =
    10.87 kg per app → 130.43 kg over 12 apps."""
    dealer, order, item = await _seed_order_with_npk(
        db, l2_type="FERTIGATION_NPK_DOSAGES", dose=(5, 0, 0),
        applications=12, fertigation_certify=True,
    )
    res = await npk_select(
        order_id=order.id, item_id=item.id,
        data={
            "mixed": None,
            "straights": [
                {"target_nutrient": "N",
                 "common_name_cosh_id": "cosh:cn-urea",
                 "trade_name_cosh_id": "cosh:tn-gsfc-urea"},
            ],
        },
        db=db, current_user=dealer,
    )
    # 100*5/46 ≈ 10.8696 per app; × 12 = 130.4348; rounded to 130.43.
    assert abs(res["picks"][0]["kg_product"] - 130.43) < 0.05


@requires_docker
@pytest.mark.asyncio
async def test_chemical_npk_does_not_multiply(db):
    """Chemical NPK with applications element set must NOT multiply —
    only Fertigation does, per spec §5.2 (non-fertigation total is
    a single basal/top-dressing application)."""
    dealer, order, item = await _seed_order_with_npk(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 0, 0),
        applications=12,
    )
    res = await npk_select(
        order_id=order.id, item_id=item.id,
        data={
            "mixed": None,
            "straights": [
                {"target_nutrient": "N",
                 "common_name_cosh_id": "cosh:cn-urea",
                 "trade_name_cosh_id": "cosh:tn-gsfc-urea"},
            ],
        },
        db=db, current_user=dealer,
    )
    # 100*50/46 = 108.69 — NOT × 12.
    assert abs(res["picks"][0]["kg_product"] - 108.69) < 0.05


# ── (1) brand consolidation ───────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_consolidation_groups_by_brand_across_items(db):
    """Two NPK practices on different timelines, both pick Urea → the
    consolidated_brands block sums kg_product."""
    dealer, order, item = await _seed_order_with_npk(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 0, 0),
    )
    # First pick on the seeded item.
    await npk_select(
        order_id=order.id, item_id=item.id,
        data={
            "mixed": None,
            "straights": [
                {"target_nutrient": "N",
                 "common_name_cosh_id": "cosh:cn-urea",
                 "trade_name_cosh_id": "cosh:tn-gsfc-urea"},
            ],
        },
        db=db, current_user=dealer,
    )

    # Add a second NPK practice on a separate timeline & seed another
    # PENDING item, then run /npk-select for that one too.
    pkg_id = (await db.execute(
        select(OrderItem).where(OrderItem.id == item.id)
    )).scalar_one().practice_id
    practice_row = (await db.execute(
        select(__import__("app.modules.advisory.models", fromlist=["Practice"]).Practice).where(
            __import__("app.modules.advisory.models", fromlist=["Practice"]).Practice.id == pkg_id,
        )
    )).scalar_one()
    timeline_row = (await db.execute(
        select(__import__("app.modules.advisory.models", fromlist=["Timeline"]).Timeline).where(
            __import__("app.modules.advisory.models", fromlist=["Timeline"]).Timeline.id == practice_row.timeline_id,
        )
    )).scalar_one()
    pkg_pkg = (await db.execute(
        select(__import__("app.modules.advisory.models", fromlist=["Package"]).Package).where(
            __import__("app.modules.advisory.models", fromlist=["Package"]).Package.id == timeline_row.package_id,
        )
    )).scalar_one()

    tl2 = await make_timeline(
        db, pkg_pkg, name="TL_30c_b",
        from_type=TimelineFromType.DAS, from_value=30, to_value=60,
    )
    p2 = await make_practice(
        db, tl2, l0=PracticeL0.INPUT, l1="FERTILIZER",
        l2="CHEMICAL_FERTILIZERS_NPK_DOSAGES",
    )
    await make_element(db, p2, element_type="N_DOSAGE", value="50", unit_cosh_id=None, cosh_ref=None)
    await make_element(db, p2, element_type="P_DOSAGE", value="0",  unit_cosh_id=None, cosh_ref=None)
    await make_element(db, p2, element_type="K_DOSAGE", value="0",  unit_cosh_id=None, cosh_ref=None)
    await db.commit()
    item2 = OrderItem(
        order_id=order.id, practice_id=p2.id, timeline_id=tl2.id,
        status=OrderItemStatus.PENDING,
    )
    db.add(item2)
    await db.commit()

    await npk_select(
        order_id=order.id, item_id=item2.id,
        data={
            "mixed": None,
            "straights": [
                {"target_nutrient": "N",
                 "common_name_cosh_id": "cosh:cn-urea",
                 "trade_name_cosh_id": "cosh:tn-gsfc-urea"},
            ],
        },
        db=db, current_user=dealer,
    )

    res = await get_dealer_order(
        order_id=order.id, db=db, current_user=dealer,
    )
    blocks = res["consolidated_brands"]
    assert len(blocks) == 1
    assert blocks[0]["brand_cosh_id"] == "cosh:tn-gsfc-urea"
    assert blocks[0]["line_count"] == 2
    # 100*50/46 = 108.6957 × 2 ≈ 217.39
    assert abs(blocks[0]["total_volume"] - 217.39) < 0.05
    assert blocks[0]["volume_unit"] == "kg"


@requires_docker
@pytest.mark.asyncio
async def test_consolidation_empty_when_no_brand_committed(db):
    """A PENDING item with no brand_cosh_id is excluded — the block
    should be an empty list, not absent."""
    dealer, order, item = await _seed_order_with_npk(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 0, 0),
    )
    res = await get_dealer_order(
        order_id=order.id, db=db, current_user=dealer,
    )
    assert res["consolidated_brands"] == []
