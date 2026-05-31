"""Orders V2 Batch 20 — BL-03 deduplication on the order bundle path.

The 2026-05-31 BL audit caught: `compute_bundle` (DAS) and
`resolve_dbs_practices_for_category` (DBS) both bypassed
`deduplicate_advisory`. The farmer's advisory view de-dupes
overlapping inputs (BL-03), but their order didn't — so the
farmer would buy the same input twice when two timelines from
different sources (CCA / CHA) recommended it.

This file locks the fix end-to-end.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import OrderItem, OrderItemStatus
from app.services.order_bundle import (
    CATEGORY_PESTICIDE, compute_bundle, resolve_dbs_practices_for_category,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


@requires_docker
@pytest.mark.asyncio
async def test_bundle_drops_duplicate_practice_from_overlapping_timeline(db):
    """Two CCA timelines with identical Mancozeb input; only the
    earlier-start one survives in the bundle."""
    user = await make_user(db, name="Farmer Dedup")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) + timedelta(days=2)
    await db.commit()

    # TL_A: day 5..12 — owns the input
    tl_a = await make_timeline(
        db, pkg, name="TL_A",
        from_type=TimelineFromType.DAS, from_value=5, to_value=12,
    )
    p_a = await make_practice(db, tl_a, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(
        db, p_a, element_type="COMMON_NAME",
        value=None, unit_cosh_id=None, cosh_ref="cosh:mancozeb",
    )

    # TL_B: day 10..20 — overlaps TL_A and recommends the SAME input
    tl_b = await make_timeline(
        db, pkg, name="TL_B",
        from_type=TimelineFromType.DAS, from_value=10, to_value=20,
    )
    p_b = await make_practice(db, tl_b, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(
        db, p_b, element_type="COMMON_NAME",
        value=None, unit_cosh_id=None, cosh_ref="cosh:mancozeb",
    )
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=30), today=date.today(),
    )
    ids = {p["id"] for p in bundle["practices"]}
    # p_a survives (earlier start). p_b is suppressed by BL-03.
    assert p_a.id in ids
    assert p_b.id not in ids


@requires_docker
@pytest.mark.asyncio
async def test_bundle_keeps_distinct_inputs_in_overlapping_timeline(db):
    """Two overlapping timelines with DIFFERENT inputs → both survive."""
    user = await make_user(db, name="Farmer Distinct")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) + timedelta(days=2)
    await db.commit()

    tl_a = await make_timeline(
        db, pkg, name="TL_A",
        from_type=TimelineFromType.DAS, from_value=5, to_value=12,
    )
    p_a = await make_practice(db, tl_a, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_a, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:mancozeb")

    tl_b = await make_timeline(
        db, pkg, name="TL_B",
        from_type=TimelineFromType.DAS, from_value=10, to_value=20,
    )
    p_b = await make_practice(db, tl_b, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_b, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:carbendazim")
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=30), today=date.today(),
    )
    ids = {p["id"] for p in bundle["practices"]}
    assert p_a.id in ids
    assert p_b.id in ids


@requires_docker
@pytest.mark.asyncio
async def test_special_input_never_suppressed_in_bundle(db):
    """BL-03 exception: is_special_input practices always survive."""
    user = await make_user(db, name="Farmer SI")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) + timedelta(days=2)
    await db.commit()

    tl_a = await make_timeline(
        db, pkg, name="TL_A",
        from_type=TimelineFromType.DAS, from_value=5, to_value=12,
    )
    p_a = await make_practice(db, tl_a, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_a, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:mancozeb")

    tl_b = await make_timeline(
        db, pkg, name="TL_B",
        from_type=TimelineFromType.DAS, from_value=10, to_value=20,
    )
    p_b = await make_practice(
        db, tl_b, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES",
        is_special_input=True,
    )
    await make_element(db, p_b, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:mancozeb")
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=30), today=date.today(),
    )
    ids = {p["id"] for p in bundle["practices"]}
    # Both survive — special input is never suppressed.
    assert p_a.id in ids
    assert p_b.id in ids


@requires_docker
@pytest.mark.asyncio
async def test_dbs_resolver_dedupes_within_package(db):
    """Two DBS timelines with identical pesticide input — DBS resolver
    drops the suppressed copy."""
    from app.modules.advisory.models import PackageType
    user = await make_user(db, name="Farmer DBS Dedup")
    client = await make_client(db)
    pkg = await make_package(db, client)
    pkg.package_type = PackageType.ANNUAL
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) + timedelta(days=10)
    await db.commit()

    tl_a = await make_timeline(
        db, pkg, name="TL_DBS_A",
        from_type=TimelineFromType.DBS, from_value=15, to_value=8,
    )
    p_a = await make_practice(db, tl_a, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_a, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:mancozeb")

    tl_b = await make_timeline(
        db, pkg, name="TL_DBS_B",
        from_type=TimelineFromType.DBS, from_value=10, to_value=2,
    )
    p_b = await make_practice(db, tl_b, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_b, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:mancozeb")
    await db.commit()

    ids = await resolve_dbs_practices_for_category(
        db, subscription=sub, category="PESTICIDE",
    )
    # tl_a starts earlier (15 days before sowing vs 10 days before),
    # so its practice governs; tl_b's identical copy is suppressed.
    assert p_a.id in ids
    assert p_b.id not in ids


@requires_docker
@pytest.mark.asyncio
async def test_approved_input_stays_suppressed_in_bundle(db):
    """BL-03 step 12: once APPROVED in TL_A, identical practice in
    TL_B is suppressed even if TL_A has since closed."""
    user = await make_user(db, name="Farmer Approved")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    # crop sown 20 days ago. TL_A (day 5..15) is closed; TL_B (day 12..25)
    # is in window AND overlaps TL_A on days 12..15.
    # BL-03 step 12 requires direct overlap for the purchased-input
    # rule to bite.
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=20)
    await db.commit()

    tl_a = await make_timeline(
        db, pkg, name="TL_A",
        from_type=TimelineFromType.DAS, from_value=5, to_value=15,
    )
    p_a = await make_practice(db, tl_a, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_a, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:mancozeb")

    tl_b = await make_timeline(
        db, pkg, name="TL_B",
        from_type=TimelineFromType.DAS, from_value=12, to_value=25,
    )
    p_b = await make_practice(db, tl_b, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_b, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:mancozeb")
    await db.commit()

    # Simulate an APPROVED purchase against p_a.
    from app.modules.orders.models import Order, OrderStatus
    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc) - timedelta(days=30),
        date_to=datetime.now(timezone.utc) - timedelta(days=20),
        status=OrderStatus.COMPLETED,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=p_a.id, timeline_id=tl_a.id,
        status=OrderItemStatus.APPROVED,
    ))
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=10), today=date.today(),
    )
    ids = {p["id"] for p in bundle["practices"]}
    # p_b suppressed by purchased-input rule (BL-03 step 12).
    assert p_b.id not in ids
