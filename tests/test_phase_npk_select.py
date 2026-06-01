"""Orders V2 Batch 30B — NPK trade-name picker + select-commit.

Covers GET /npk-trade-names and POST /npk-select. Seeds full Cosh
shape: common names, concentrations, trade names, tradename_commonname
links, and (for fertigation) npk_fertigation_products certifications.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import get_npk_trade_names, npk_select
from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_onboarded_dealer, make_package,
    make_practice, make_subscription, make_timeline, make_user,
)


# Reused IDs across the fixture — match the prod cosh_id style.
NUT_N, NUT_P, NUT_K = "cosh:nut-n", "cosh:nut-p", "cosh:nut-k"
STRAIGHT, COMPLEX = "cosh:sc-straight", "cosh:sc-complex"


async def _seed_lookup_cores(db):
    seeds = [
        (NUT_N, "fert_nutrients", "Nitrogen"),
        (NUT_P, "fert_nutrients", "Phosphorus"),
        (NUT_K, "fert_nutrients", "Potassium"),
        (STRAIGHT, "straight_complex", "Straight fertilizer"),
        (COMPLEX, "straight_complex", "Complex fertilizer"),
    ]
    for cid, ct, en in seeds:
        db.add(CoshCoreItem(
            cosh_id=cid, core_type=ct, parent_cosh_id=None,
            translations={"en": en}, status="active",
        ))
    await db.commit()


def _add_concentration(db, value: float, seen: set) -> str:
    cid = f"cosh:conc-{value}"
    if cid in seen:
        return cid
    seen.add(cid)
    db.add(CoshCoreItem(
        cosh_id=cid, core_type="fert_nutrient_concentration_core",
        parent_cosh_id=None,
        translations={"en": str(value)}, status="active",
    ))
    return cid


def _add_fertiliser(
    db, *, cn_cosh_id, en_name,
    n=0, p=0, k=0, sc=COMPLEX, seen_conc: set,
):
    db.add(CoshCoreItem(
        cosh_id=cn_cosh_id, core_type="common_names_of_inputs",
        parent_cosh_id=None, translations={"en": en_name}, status="active",
    ))
    for letter, nut_id, val in (("N", NUT_N, n), ("P", NUT_P, p), ("K", NUT_K, k)):
        if val <= 0:
            continue
        conc_id = _add_concentration(db, val, seen_conc)
        db.add(CoshConnectRow(
            connect_id=f"connect:fnc:{cn_cosh_id}:{letter}",
            connect_type="fert_nutrient_concentration",
            endpoints=[
                {"role": "common_names_of_inputs", "cosh_id": cn_cosh_id, "position": 1},
                {"role": "straight_complex", "cosh_id": sc, "position": 2},
                {"role": "fert_nutrients", "cosh_id": nut_id, "position": 3},
                {"role": "fert_nutrient_concentration_core", "cosh_id": conc_id, "position": 4},
            ],
            status="active",
        ))


def _add_trade_name(
    db, *, tn_cosh_id, en_name, mfr_cosh_id,
    common_name_cosh_id,
):
    db.add(CoshCoreItem(
        cosh_id=tn_cosh_id, core_type="trade_names", parent_cosh_id=None,
        translations={"en": en_name}, status="active",
    ))
    db.add(CoshConnectRow(
        connect_id=f"connect:tncn:{tn_cosh_id}",
        connect_type="tradename_commonname",
        endpoints=[
            {"role": "trade_names", "cosh_id": tn_cosh_id, "position": 1},
            {"role": "common_names_of_inputs", "cosh_id": common_name_cosh_id, "position": 2},
        ],
        status="active",
    ))
    db.add(CoshConnectRow(
        connect_id=f"connect:tnm:{tn_cosh_id}",
        connect_type="tradename_manufacturer",
        endpoints=[
            {"role": "trade_names", "cosh_id": tn_cosh_id, "position": 1},
            {"role": "input_manufacturers", "cosh_id": mfr_cosh_id, "position": 2},
        ],
        status="active",
    ))


def _certify_for_fertigation(db, *, common_name_cosh_id, trade_name_cosh_id):
    """Mirror npk_fertigation_products shape: pos 1 = commonnames_l2
    connect_id, pos 2 = tradename_manufacturer connect_id."""
    cnl2_id = f"connect:cnl2:{common_name_cosh_id}"
    # Insert once per common_name.
    existing_cnl2 = db.sync_session.query(CoshConnectRow).filter_by(connect_id=cnl2_id).first() if False else None
    db.add(CoshConnectRow(
        connect_id=cnl2_id, connect_type="commonnames_l2",
        endpoints=[
            {"role": "common_names_of_inputs", "cosh_id": common_name_cosh_id, "position": 1},
            {"role": "l2_data", "cosh_id": "cosh:l2-fert", "position": 2},
        ],
        status="active",
    ))
    db.add(CoshConnectRow(
        connect_id=f"connect:fert:{common_name_cosh_id}:{trade_name_cosh_id}",
        connect_type="npk_fertigation_products",
        endpoints=[
            {"role": "connect_8695043c", "cosh_id": cnl2_id, "position": 1},
            {"role": "connect_f26fbec8", "cosh_id": f"connect:tnm:{trade_name_cosh_id}", "position": 2},
            {"role": "formulations", "cosh_id": "cosh:fmt-solid", "position": 3},
        ],
        status="active",
    ))


async def _seed_order(db, *, l2_type, dose, fertigation_certify=False):
    await _seed_lookup_cores(db)
    seen: set = set()
    _add_fertiliser(db, cn_cosh_id="cosh:cn-10-26-26", en_name="NPK 10:26:26", n=10, p=26, k=26, seen_conc=seen)
    _add_fertiliser(db, cn_cosh_id="cosh:cn-urea", en_name="Urea", n=46, sc=STRAIGHT, seen_conc=seen)
    _add_fertiliser(db, cn_cosh_id="cosh:cn-ssp",  en_name="SSP",  p=16, sc=STRAIGHT, seen_conc=seen)
    _add_fertiliser(db, cn_cosh_id="cosh:cn-mop",  en_name="MOP",  k=60, sc=STRAIGHT, seen_conc=seen)

    # Two brands per common name to exercise the alphabetical sort
    # and the manufacturer surface.
    _add_trade_name(db, tn_cosh_id="cosh:tn-iffco-npk", en_name="Iffco NPK 10:26:26",
                    mfr_cosh_id="cosh:mfr-iffco", common_name_cosh_id="cosh:cn-10-26-26")
    _add_trade_name(db, tn_cosh_id="cosh:tn-acme-npk", en_name="Acme NPK 10:26:26",
                    mfr_cosh_id="cosh:mfr-acme",  common_name_cosh_id="cosh:cn-10-26-26")
    _add_trade_name(db, tn_cosh_id="cosh:tn-gsfc-urea", en_name="GSFC Urea",
                    mfr_cosh_id="cosh:mfr-gsfc", common_name_cosh_id="cosh:cn-urea")
    _add_trade_name(db, tn_cosh_id="cosh:tn-iffco-ssp", en_name="Iffco SSP",
                    mfr_cosh_id="cosh:mfr-iffco", common_name_cosh_id="cosh:cn-ssp")
    _add_trade_name(db, tn_cosh_id="cosh:tn-iffco-mop", en_name="Iffco MOP",
                    mfr_cosh_id="cosh:mfr-iffco", common_name_cosh_id="cosh:cn-mop")

    if fertigation_certify:
        _certify_for_fertigation(db, common_name_cosh_id="cosh:cn-10-26-26",
                                  trade_name_cosh_id="cosh:tn-iffco-npk")
        _certify_for_fertigation(db, common_name_cosh_id="cosh:cn-urea",
                                  trade_name_cosh_id="cosh:tn-gsfc-urea")
    await db.commit()

    farmer = await make_user(db, name="F-30b")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.farm_area_acres = 1.0
    await db.commit()
    tl = await make_timeline(
        db, pkg, name="TL_30b",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2=l2_type,
    )
    await make_element(db, p, element_type="N_DOSAGE", value=str(dose[0]), unit_cosh_id=None, cosh_ref=None)
    await make_element(db, p, element_type="P_DOSAGE", value=str(dose[1]), unit_cosh_id=None, cosh_ref=None)
    await make_element(db, p, element_type="K_DOSAGE", value=str(dose[2]), unit_cosh_id=None, cosh_ref=None)
    await db.commit()
    dealer = await make_onboarded_dealer(db, name="D-30b")
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


# ── /npk-trade-names ─────────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_trade_names_chemical_returns_alpha_sorted(db):
    dealer, order, item = await _seed_order(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 80, 30),
    )
    res = await get_npk_trade_names(
        order_id=order.id, item_id=item.id,
        common_name_cosh_id="cosh:cn-10-26-26",
        db=db, current_user=dealer,
    )
    assert res["fertigation"] is False
    # Alphabetical per spec §3.1: Acme before Iffco.
    assert [t["cosh_id"] for t in res["trade_names"]] == [
        "cosh:tn-acme-npk", "cosh:tn-iffco-npk",
    ]
    assert res["trade_names"][0]["manufacturer_cosh_id"] == "cosh:mfr-acme"


@requires_docker
@pytest.mark.asyncio
async def test_trade_names_fertigation_filters_via_approved_set(db):
    dealer, order, item = await _seed_order(
        db, l2_type="FERTIGATION_NPK_DOSAGES", dose=(50, 80, 30),
        fertigation_certify=True,
    )
    res = await get_npk_trade_names(
        order_id=order.id, item_id=item.id,
        common_name_cosh_id="cosh:cn-10-26-26",
        db=db, current_user=dealer,
    )
    assert res["fertigation"] is True
    # Only Iffco was certified; Acme dropped.
    assert [t["cosh_id"] for t in res["trade_names"]] == ["cosh:tn-iffco-npk"]


@requires_docker
@pytest.mark.asyncio
async def test_trade_names_refuses_non_npk_practice(db):
    """Hitting the endpoint for a non-NPK practice is a client bug —
    should error loudly instead of returning empty list (silent failure)."""
    farmer = await make_user(db, name="F-other")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    await db.commit()
    tl = await make_timeline(
        db, pkg, name="TL", from_type=TimelineFromType.DAS,
        from_value=0, to_value=20,
    )
    p = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES",
    )
    await db.commit()
    dealer = await make_onboarded_dealer(db, name="D")
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
    item = OrderItem(
        order_id=order.id, practice_id=p.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    )
    db.add(item)
    await db.commit()

    with pytest.raises(Exception) as exc:
        await get_npk_trade_names(
            order_id=order.id, item_id=item.id,
            common_name_cosh_id="cosh:cn-10-26-26",
            db=db, current_user=dealer,
        )
    assert "NOT_AN_NPK_PRACTICE" in str(exc.value.__dict__)


# ── /npk-select ──────────────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_select_mixed_only_reuses_original_item(db):
    """Single pick (Mixed) → no siblings, original PENDING item flips
    to AVAILABLE with brand + estimated volume."""
    dealer, order, item = await _seed_order(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 80, 30),
    )
    res = await npk_select(
        order_id=order.id, item_id=item.id,
        data={
            "mixed": {
                "common_name_cosh_id": "cosh:cn-10-26-26",
                "trade_name_cosh_id": "cosh:tn-iffco-npk",
            },
            "straights": [],
        },
        db=db, current_user=dealer,
    )
    assert len(res["item_ids"]) == 1
    assert res["item_ids"][0] == item.id
    # 10:26:26 K-match: 100*30/26 = 115.38 kg.
    assert abs(res["picks"][0]["kg_product"] - 115.38) < 0.05

    # Verify the item state via DB.
    items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order.id)
    )).scalars().all()
    assert len(items) == 1
    item_db = items[0]
    assert item_db.status == OrderItemStatus.AVAILABLE
    assert item_db.brand_cosh_id == "cosh:tn-iffco-npk"
    assert item_db.brand_name == "Iffco NPK 10:26:26"
    assert item_db.volume_unit == "kg"
    assert item_db.relation_type == "AND"


@requires_docker
@pytest.mark.asyncio
async def test_select_mixed_plus_two_straights_creates_siblings(db):
    """1 Mixed + 2 Straights → 3 OrderItem rows, same practice_id,
    shared AND relation."""
    dealer, order, item = await _seed_order(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 80, 30),
    )
    res = await npk_select(
        order_id=order.id, item_id=item.id,
        data={
            "mixed": {
                "common_name_cosh_id": "cosh:cn-10-26-26",
                "trade_name_cosh_id": "cosh:tn-iffco-npk",
            },
            "straights": [
                {"target_nutrient": "N",
                 "common_name_cosh_id": "cosh:cn-urea",
                 "trade_name_cosh_id": "cosh:tn-gsfc-urea"},
                {"target_nutrient": "P",
                 "common_name_cosh_id": "cosh:cn-ssp",
                 "trade_name_cosh_id": "cosh:tn-iffco-ssp"},
            ],
        },
        db=db, current_user=dealer,
    )
    assert len(res["item_ids"]) == 3

    items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order.id)
    )).scalars().all()
    assert len(items) == 3
    relation_ids = {i.relation_id for i in items}
    assert len(relation_ids) == 1 and next(iter(relation_ids)) == res["relation_id"]
    # All AVAILABLE, all kg, all positions 1..3.
    for i in items:
        assert i.status == OrderItemStatus.AVAILABLE
        assert i.volume_unit == "kg"
        assert i.relation_type == "AND"
    positions = sorted(int(i.relation_role.rsplit("_", 1)[-1]) for i in items)
    assert positions == [1, 2, 3]


@requires_docker
@pytest.mark.asyncio
async def test_select_rejects_unranked_mixed(db):
    """An obviously-bogus common_name_cosh_id should 422, not silently
    accept (we re-rank server-side and refuse unknowns)."""
    dealer, order, item = await _seed_order(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 80, 30),
    )
    with pytest.raises(Exception) as exc:
        await npk_select(
            order_id=order.id, item_id=item.id,
            data={
                "mixed": {
                    "common_name_cosh_id": "cosh:does-not-exist",
                    "trade_name_cosh_id": "cosh:tn-iffco-npk",
                },
                "straights": [],
            },
            db=db, current_user=dealer,
        )
    assert "NPK_MIXED_NOT_RANKED" in str(exc.value.__dict__)


@requires_docker
@pytest.mark.asyncio
async def test_select_rejects_trade_name_not_in_pool(db):
    """A correct common name but a trade name that doesn't actually map
    to it should be refused — guards against client-supplied junk."""
    dealer, order, item = await _seed_order(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 80, 30),
    )
    with pytest.raises(Exception) as exc:
        await npk_select(
            order_id=order.id, item_id=item.id,
            data={
                "mixed": {
                    "common_name_cosh_id": "cosh:cn-10-26-26",
                    "trade_name_cosh_id": "cosh:tn-gsfc-urea",  # wrong CN
                },
                "straights": [],
            },
            db=db, current_user=dealer,
        )
    assert "NPK_TRADE_NAME_NOT_IN_POOL" in str(exc.value.__dict__)


@requires_docker
@pytest.mark.asyncio
async def test_select_rejects_straight_with_no_remaining_gap(db):
    """After a Mixed fully covers K, picking a K-Straight should 422."""
    dealer, order, item = await _seed_order(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 80, 30),
    )
    # 10:26:26 K-match leaves K gap = 0 → MOP pick is invalid.
    with pytest.raises(Exception) as exc:
        await npk_select(
            order_id=order.id, item_id=item.id,
            data={
                "mixed": {
                    "common_name_cosh_id": "cosh:cn-10-26-26",
                    "trade_name_cosh_id": "cosh:tn-iffco-npk",
                },
                "straights": [
                    {"target_nutrient": "K",
                     "common_name_cosh_id": "cosh:cn-mop",
                     "trade_name_cosh_id": "cosh:tn-iffco-mop"},
                ],
            },
            db=db, current_user=dealer,
        )
    assert "NPK_STRAIGHT_NO_GAP" in str(exc.value.__dict__)
