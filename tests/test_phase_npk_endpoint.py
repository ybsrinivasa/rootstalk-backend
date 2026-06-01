"""Orders V2 Batch 30 — NPK endpoint integration.

End-to-end through GET /dealer/orders/{id}/items/{id}/npk-options
using the real Cosh data shape (synced 2026-06-01):

  - fert_nutrients Core: 3 rows (Nitrogen / Phosphorus / Potassium)
  - straight_complex Core: 2 rows (Straight / Complex)
  - fert_nutrient_concentration_core Core: numeric % in translations.en
  - fert_nutrient_concentration Connect: 4-endpoint glue
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import get_item_npk_options
from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_onboarded_dealer, make_package,
    make_practice, make_subscription, make_timeline, make_user,
)


# Reusable cosh_ids — match the prod payload shape (UUID-like strings).
# Concentration cores get a `conc:<value>` prefix so the same value can
# be reused without cosh_id collisions across tests.
NUT_N = "cosh:nut-n"
NUT_P = "cosh:nut-p"
NUT_K = "cosh:nut-k"
STRAIGHT = "cosh:sc-straight"
COMPLEX = "cosh:sc-complex"


def _conc_cosh_id(value: float) -> str:
    return f"cosh:conc-{value}"


async def _ensure_npk_core_seeds(db):
    """Insert the 5 lookup rows + any concentration cores already
    referenced. Idempotent — safe to call from each test."""
    existing = {
        c.cosh_id
        for c in (await db.execute(select(CoshCoreItem))).scalars().all()
    } if False else set()  # branchless flag — we always do the insert pattern below

    seeds = [
        (NUT_N, "fert_nutrients", "Nitrogen"),
        (NUT_P, "fert_nutrients", "Phosphorus"),
        (NUT_K, "fert_nutrients", "Potassium"),
        (STRAIGHT, "straight_complex", "Straight fertilizer"),
        (COMPLEX, "straight_complex", "Complex fertilizer"),
    ]
    for cid, ct, en in seeds:
        if cid in existing:
            continue
        db.add(CoshCoreItem(
            cosh_id=cid, core_type=ct, parent_cosh_id=None,
            translations={"en": en}, status="active",
        ))
    await db.commit()


def _add_concentration_core(db, value: float, seen: set):
    """Add the numeric concentration core if not already seen.
    `seen` is the per-fixture dedupe set (concentration values get
    reused across fertilisers — 10:26:26 alone uses 26 twice)."""
    cid = _conc_cosh_id(value)
    if cid in seen:
        return cid
    seen.add(cid)
    db.add(CoshCoreItem(
        cosh_id=cid, core_type="fert_nutrient_concentration_core",
        parent_cosh_id=None,
        translations={"en": str(value)},
        status="active",
    ))
    return cid


def _add_fertiliser(
    db, *, cn_cosh_id: str, en_name: str,
    n: float = 0, p: float = 0, k: float = 0,
    straight_or_complex: str = COMPLEX,
    seen_conc: set,
):
    """Seed a fertiliser end-to-end: common_name Core + concentration
    Cores + Connect rows (one per non-zero nutrient)."""
    db.add(CoshCoreItem(
        cosh_id=cn_cosh_id, core_type="common_names_of_inputs",
        parent_cosh_id=None, translations={"en": en_name},
        status="active",
    ))
    for letter, nut_id, val in (("N", NUT_N, n), ("P", NUT_P, p), ("K", NUT_K, k)):
        if val <= 0:
            continue
        conc_id = _add_concentration_core(db, val, seen_conc)
        db.add(CoshConnectRow(
            connect_id=f"connect:{cn_cosh_id}:{letter}",
            connect_type="fert_nutrient_concentration",
            endpoints=[
                {"role": "common_names_of_inputs",            "cosh_id": cn_cosh_id, "position": 1},
                {"role": "straight_complex",                  "cosh_id": straight_or_complex, "position": 2},
                {"role": "fert_nutrients",                    "cosh_id": nut_id, "position": 3},
                {"role": "fert_nutrient_concentration_core",  "cosh_id": conc_id, "position": 4},
            ],
            status="active",
        ))


# pull select into module scope for the helper above
from sqlalchemy import select  # noqa: E402


async def _seed_fixture(db):
    """Three fertilisers covering Mixed + Straight N + Straight P + Straight K."""
    await _ensure_npk_core_seeds(db)
    seen: set = set()
    _add_fertiliser(
        db, cn_cosh_id="cosh:cn-10-26-26", en_name="NPK 10:26:26",
        n=10, p=26, k=26, straight_or_complex=COMPLEX, seen_conc=seen,
    )
    _add_fertiliser(
        db, cn_cosh_id="cosh:cn-urea", en_name="Urea",
        n=46, straight_or_complex=STRAIGHT, seen_conc=seen,
    )
    _add_fertiliser(
        db, cn_cosh_id="cosh:cn-ssp", en_name="SSP",
        p=16, straight_or_complex=STRAIGHT, seen_conc=seen,
    )
    _add_fertiliser(
        db, cn_cosh_id="cosh:cn-mop", en_name="MOP",
        k=60, straight_or_complex=STRAIGHT, seen_conc=seen,
    )
    await db.commit()


async def _seed_npk_order(db, *, l2_type, dose):
    await _seed_fixture(db)
    farmer = await make_user(db, name="F-npk")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.farm_area_acres = 1.0
    await db.commit()
    tl = await make_timeline(
        db, pkg, name="TL_npk",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2=l2_type,
    )
    await make_element(db, p, element_type="N_DOSAGE", value=str(dose[0]), unit_cosh_id=None, cosh_ref=None)
    await make_element(db, p, element_type="P_DOSAGE", value=str(dose[1]), unit_cosh_id=None, cosh_ref=None)
    await make_element(db, p, element_type="K_DOSAGE", value=str(dose[2]), unit_cosh_id=None, cosh_ref=None)
    await db.commit()
    dealer = await make_onboarded_dealer(db, name="D-npk")
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


@requires_docker
@pytest.mark.asyncio
async def test_non_npk_practice_returns_flag_false(db):
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
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE",
        l2="CHEMICAL_PESTICIDES",
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

    res = await get_item_npk_options(
        order_id=order.id, item_id=item.id,
        db=db, current_user=dealer,
    )
    assert res["is_npk_practice"] is False
    assert res["ranked_mixed"] == []


@requires_docker
@pytest.mark.asyncio
async def test_chemical_npk_ranks_mixed_and_lists_straights(db):
    dealer, order, item = await _seed_npk_order(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 80, 30),
    )
    res = await get_item_npk_options(
        order_id=order.id, item_id=item.id,
        db=db, current_user=dealer,
    )
    assert res["is_npk_practice"] is True
    assert res["fertigation"] is False
    assert res["required_dose"] == {"n": 50, "p": 80, "k": 30}
    mixed_ids = [r["cosh_id"] for r in res["ranked_mixed"]]
    assert "cosh:cn-10-26-26" in mixed_ids
    straight_ids = {s["cosh_id"] for s in res["enabled_straights"]}
    assert straight_ids == {"cosh:cn-urea", "cosh:cn-ssp", "cosh:cn-mop"}


@requires_docker
@pytest.mark.asyncio
async def test_picked_mixed_narrows_straights_to_gap(db):
    dealer, order, item = await _seed_npk_order(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES", dose=(50, 80, 30),
    )
    res = await get_item_npk_options(
        order_id=order.id, item_id=item.id,
        picked_mixed_cosh_id="cosh:cn-10-26-26",
        db=db, current_user=dealer,
    )
    # 10:26:26 K-match: 115.38 kg → N=11.54, P=30, K=30.
    # Gap: N=38.46, P=50, K=0 → Urea + SSP enabled, MOP suppressed.
    straight_ids = {s["cosh_id"] for s in res["enabled_straights"]}
    assert straight_ids == {"cosh:cn-urea", "cosh:cn-ssp"}


@requires_docker
@pytest.mark.asyncio
async def test_fertigation_runs_same_ranking_pending_water_soluble_flag(db):
    """Until Cosh ships a water-soluble flag, fertigation runs over the
    full pool. This test pins the current (defer-filtered) behaviour so
    we notice when the flag arrives and the test needs updating."""
    dealer, order, item = await _seed_npk_order(
        db, l2_type="FERTIGATION_NPK_DOSAGES", dose=(50, 80, 30),
    )
    res = await get_item_npk_options(
        order_id=order.id, item_id=item.id,
        db=db, current_user=dealer,
    )
    assert res["fertigation"] is True
    # Same Mixeds + Straights as the chemical flow above.
    assert "cosh:cn-10-26-26" in [r["cosh_id"] for r in res["ranked_mixed"]]
    assert {s["cosh_id"] for s in res["enabled_straights"]} == {
        "cosh:cn-urea", "cosh:cn-ssp", "cosh:cn-mop",
    }
