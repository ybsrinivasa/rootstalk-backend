"""Orders V2 Batch 25 — BL-07 brand options: three groups
(Recommended / My / Other) + formulation-class unit family.

User narrative (2026-05-31): "Recommended Brands, My Brands, and
Other Brands" — Recommended sits above My, which sits above Other.
Empty groups hidden. Same brand never repeated. Unit constrained
per brand's formulation class.
"""
from __future__ import annotations

import pytest

from app.modules.advisory.models import PracticeL0
from app.modules.orders.models import DealerProfile, DealerRelationship
from app.modules.sync.models import CoshCoreItem
from app.services.bl07_brand_options import (
    _classify_formulation_to_unit_family, get_brand_options,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_onboarded_dealer, make_package,
    make_practice, make_subscription, make_timeline, make_user,
)


# ── Pure formulation classifier ──────────────────────────────────


def test_classify_solid_formulations():
    assert _classify_formulation_to_unit_family("Granular Powder") == "solid"
    assert _classify_formulation_to_unit_family("Water Soluble Granule") == "solid"
    assert _classify_formulation_to_unit_family("Crystal") == "solid"
    assert _classify_formulation_to_unit_family(None) == "solid"


def test_classify_liquid_formulations():
    assert _classify_formulation_to_unit_family("Suspension Concentrate (SC)") == "liquid"
    assert _classify_formulation_to_unit_family("Emulsifiable Concentrate (EC)") == "liquid"
    assert _classify_formulation_to_unit_family("Liquid") == "liquid"
    assert _classify_formulation_to_unit_family("Soluble Liquid (SL)") == "liquid"


def test_classify_discrete_formulations():
    assert _classify_formulation_to_unit_family("Pheromone Trap") == "discrete"
    assert _classify_formulation_to_unit_family("Sachet") == "discrete"


# ── End-to-end through get_brand_options ──────────────────────────


async def _seed_practice_with_brands(db, *, recommended_cosh_id=None):
    """Build a practice with a COMMON_NAME element pointing at a Cosh
    common name, then three brands under it. Two of the brands share a
    manufacturer the dealer is onboarded with → that's the My-Brands
    group. The third is from a different manufacturer → Other Brands.
    """
    common_cosh = "cosh:cn-mancozeb"
    db.add(CoshCoreItem(
        cosh_id=common_cosh, core_type="common_name",
        parent_cosh_id=None, translations={"en": "Mancozeb"},
        status="active",
    ))
    fmt_cosh = "cosh:fmt-wp"
    db.add(CoshCoreItem(
        cosh_id=fmt_cosh, core_type="formulation",
        parent_cosh_id=None, translations={"en": "Wettable Powder"},
        status="active",
    ))
    # Fix 2026-06-01 — brands now live in the brand_lookup_cache,
    # populated from `trade_names` Core + tradename_commonname /
    # tradename_manufacturer / tradename_formulation Connects. Seed
    # the cache directly here so the test stays focused on grouping
    # logic; rebuild_brand_cache is exercised in its own test.
    from app.modules.orders.models import BrandLookupCache
    db.add(BrandLookupCache(
        common_name_cosh_id=common_cosh,
        trade_name_cosh_id="cosh:brand-acme",
        trade_name="Acme Mancozeb",
        manufacturer_cosh_id="cosh:mfr-acme",
        manufacturer_name="AcmeCo",
        formulation_cosh_id=fmt_cosh,
        formulation_name="Wettable Powder",
    ))
    db.add(BrandLookupCache(
        common_name_cosh_id=common_cosh,
        trade_name_cosh_id="cosh:brand-zeta",
        trade_name="Zeta Mancozeb",
        manufacturer_cosh_id="cosh:mfr-acme",
        manufacturer_name="AcmeCo",
        formulation_cosh_id=fmt_cosh,
        formulation_name="Wettable Powder",
    ))
    db.add(BrandLookupCache(
        common_name_cosh_id=common_cosh,
        trade_name_cosh_id="cosh:brand-other",
        trade_name="Other Mancozeb",
        manufacturer_cosh_id="cosh:mfr-other",
        manufacturer_name="OtherCo",
        formulation_cosh_id=fmt_cosh,
        formulation_name="Wettable Powder",
    ))
    await db.commit()

    user = await make_user(db, name="Farmer 3G")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    await db.commit()

    from datetime import datetime, timezone
    from app.modules.advisory.models import TimelineFromType
    tl = await make_timeline(
        db, pkg, name="TL_brands",
        from_type=TimelineFromType.DAS, from_value=0, to_value=20,
    )
    practice = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES",
    )
    await make_element(
        db, practice, element_type="COMMON_NAME",
        value=None, unit_cosh_id=None, cosh_ref=common_cosh,
    )
    if recommended_cosh_id:
        await make_element(
            db, practice, element_type="BRAND_NAME",
            value=None, unit_cosh_id=None, cosh_ref=recommended_cosh_id,
        )
    practice.is_brand_locked = False
    await db.commit()

    # Onboard the dealer with AcmeCo so the My-Brands grouping triggers.
    dealer = await make_onboarded_dealer(db, name="D-3G")
    db.add(DealerProfile(
        user_id=dealer.id, shop_name="S", sell_categories=["PESTICIDES"],
        shop_gps_lat=12.0, shop_gps_lng=77.0,
    ))
    db.add(DealerRelationship(
        dealer_user_id=dealer.id,
        manufacturer_name="AcmeCo",
        status="ACTIVE",
    ))
    await db.commit()

    return user, dealer, practice


@requires_docker
@pytest.mark.asyncio
async def test_three_groups_without_recommendation(db):
    """No BRAND_NAME element on the practice → Recommended group is empty
    (and hidden in the response)."""
    _, dealer, practice = await _seed_practice_with_brands(db, recommended_cosh_id=None)

    result = await get_brand_options(db, practice.id, dealer.id)
    payload = result.to_dict()
    labels = [g["label"] for g in payload["groups"]]
    assert "Recommended Brands" not in labels  # hidden when empty
    assert "My Brands" in labels
    assert "Other Brands" in labels


@requires_docker
@pytest.mark.asyncio
async def test_three_groups_with_recommendation(db):
    """When BRAND_NAME points at a brand, it goes into Recommended group
    even if the brand's manufacturer matches the dealer's onboarding."""
    _, dealer, practice = await _seed_practice_with_brands(
        db, recommended_cosh_id="cosh:brand-acme",
    )

    result = await get_brand_options(db, practice.id, dealer.id)
    payload = result.to_dict()

    rec_group = next(g for g in payload["groups"] if g["label"] == "Recommended Brands")
    my_group = next(g for g in payload["groups"] if g["label"] == "My Brands")

    rec_ids = {b["cosh_id"] for b in rec_group["brands"]}
    my_ids = {b["cosh_id"] for b in my_group["brands"]}

    # Acme is recommended → only in Recommended, not in My.
    assert "cosh:brand-acme" in rec_ids
    assert "cosh:brand-acme" not in my_ids
    # Zeta also onboarded with AcmeCo but not recommended → My only.
    assert "cosh:brand-zeta" in my_ids


@requires_docker
@pytest.mark.asyncio
async def test_brand_unit_family_in_payload(db):
    _, dealer, practice = await _seed_practice_with_brands(db, recommended_cosh_id=None)
    result = await get_brand_options(db, practice.id, dealer.id)
    payload = result.to_dict()

    # All three brands use formulation = Wettable Powder → solid → kg/g
    assert payload["brand_unit_family"]["cosh:brand-acme"] == "solid"
    assert payload["unit_options_by_family"]["solid"] == ["kg", "g"]
    assert payload["unit_options_by_family"]["liquid"] == ["L", "ml"]
