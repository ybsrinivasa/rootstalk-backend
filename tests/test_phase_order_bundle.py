"""Date-range bundling for farmer purchase orders.

Locks the rule the user described 2026-05-21:
  - PESTICIDE category bundles L1=PESTICIDE + L1=SPECIAL_INPUT
  - FERTILIZER category bundles L1=FERTILIZER (excluding NPK dosages)
  - A practice goes in if its timeline window overlaps [today, to_date]
    by at least one day
  - A practice goes in at most once over the subscription's lifetime
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import create_order, OrderCreate, order_preview
from app.services.order_bundle import (
    CATEGORY_FERTILIZER, CATEGORY_PESTICIDE,
    already_ordered_practice_ids, compute_bundle, conflicts_with_existing_orders,
    windows_overlap,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


# ── Pure-function checks ────────────────────────────────────────────────────

def test_windows_overlap_one_shared_day_is_enough():
    assert windows_overlap(
        date(2026, 5, 10), date(2026, 5, 15),
        date(2026, 5, 15), date(2026, 5, 30),
    )


def test_windows_overlap_adjacent_dates_do_not_overlap():
    assert not windows_overlap(
        date(2026, 5, 10), date(2026, 5, 14),
        date(2026, 5, 15), date(2026, 5, 20),
    )


def test_conflicts_returns_intersection_sorted():
    assert conflicts_with_existing_orders(
        ["c", "a", "b"], {"x", "a", "c"},
    ) == ["a", "c"]


# ── DB-backed bundle tests ──────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pesticide_bundle_includes_overlapping_pesticides_and_adjuvants(db):
    user = await make_user(db, name="Farmer Bundle P")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=5)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_window_0_30",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    pest = await make_practice(db, tl, l0=PracticeL0.INPUT,
        l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    adjv = await make_practice(db, tl, l0=PracticeL0.INPUT,
        l1="SPECIAL_INPUT", l2="ADJUVANTS")
    fert = await make_practice(db, tl, l0=PracticeL0.INPUT,
        l1="FERTILIZER", l2="BIOFERTILIZERS")
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=10), today=date.today(),
    )
    ids = {p["id"] for p in bundle["practices"]}
    assert pest.id in ids, "pesticide should bundle into PESTICIDE basket"
    assert adjv.id in ids, "adjuvant (SPECIAL_INPUT) should bundle with pesticides"
    assert fert.id not in ids, "fertilizer must not leak into pesticide basket"


@requires_docker
@pytest.mark.asyncio
async def test_fertilizer_bundle_excludes_npk_dosages(db):
    user = await make_user(db, name="Farmer Bundle F")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=5)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_fert_window",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    chem_fert = await make_practice(db, tl, l0=PracticeL0.INPUT,
        l1="FERTILIZER", l2="CHEMICAL_FERTILIZER_PRODUCTS")
    npk = await make_practice(db, tl, l0=PracticeL0.INPUT,
        l1="FERTILIZER", l2="CHEMICAL_FERTILIZERS_NPK_DOSAGES")
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_FERTILIZER,
        to_date=date.today() + timedelta(days=10), today=date.today(),
    )
    ids = {p["id"] for p in bundle["practices"]}
    assert chem_fert.id in ids
    assert npk.id not in ids, "NPK dosage L2 must be excluded (no trade names → no dealer products)"


@requires_docker
@pytest.mark.asyncio
async def test_one_day_overlap_includes_the_practice(db):
    """Practice timeline 30-45 DAS, today is day 0, farmer picks
    today+30 (= day 30). Overlap is exactly 1 day; practice IS in."""
    user = await make_user(db, name="Farmer Overlap")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_30_45", from_type=TimelineFromType.DAS,
        from_value=30, to_value=45,
    )
    pest = await make_practice(db, tl, l0=PracticeL0.INPUT,
        l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()

    today = date.today()
    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=today + timedelta(days=30),  # = day 30 of crop
        today=today,
    )
    assert {p["id"] for p in bundle["practices"]} == {pest.id}


@requires_docker
@pytest.mark.asyncio
async def test_practice_outside_window_is_excluded(db):
    user = await make_user(db, name="Farmer NoOverlap")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    await db.commit()

    tl_far = await make_timeline(
        db, pkg, name="TL_60_75", from_type=TimelineFromType.DAS,
        from_value=60, to_value=75,
    )
    far = await make_practice(db, tl_far, l0=PracticeL0.INPUT,
        l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=30),  # stops at day 30; far is 60+
        today=date.today(),
    )
    assert all(p["id"] != far.id for p in bundle["practices"])


@requires_docker
@pytest.mark.asyncio
async def test_already_ordered_practice_is_excluded(db):
    """The one-practice-per-order rule. Once a pesticide is in any
    non-CANCELLED order, it doesn't reappear in any future bundle."""
    user = await make_user(db, name="Farmer Dedup")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_dedup",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    pest = await make_practice(db, tl, l0=PracticeL0.INPUT,
        l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()

    # Place a prior order containing this pesticide.
    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.SENT,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=pest.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    ))
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=15), today=date.today(),
    )
    assert {p["id"] for p in bundle["practices"]} == set()
    assert bundle["excluded_already_ordered"] == 1


@requires_docker
@pytest.mark.asyncio
async def test_cancelled_order_releases_practice_back_into_pool(db):
    """Cancelled orders return their items to "purchase required"
    (BL-10). A subsequent bundle should re-include them."""
    user = await make_user(db, name="Farmer Cancel")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_cancel",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    pest = await make_practice(db, tl, l0=PracticeL0.INPUT,
        l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()

    cancelled = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.CANCELLED,
    )
    db.add(cancelled)
    await db.flush()
    db.add(OrderItem(
        order_id=cancelled.id, practice_id=pest.id, timeline_id=tl.id,
        status=OrderItemStatus.REMOVED,
    ))
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=15), today=date.today(),
    )
    assert {p["id"] for p in bundle["practices"]} == {pest.id}


# ── POST /farmer/orders dedup guard ─────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_order_rejects_already_ordered_practice(db):
    """The safety net — even if a race lets a duplicate practice_id
    reach the POST, server refuses with 409 + clear code."""
    user = await make_user(db, name="Farmer Conflict")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    sub.farm_area_acres = 1.0
    sub.area_unit = "acres"
    sub.farm_area_confirmed_at = datetime.now(timezone.utc)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_conflict",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    pest = await make_practice(db, tl, l0=PracticeL0.INPUT,
        l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()

    prior = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.SENT,
    )
    db.add(prior)
    await db.flush()
    db.add(OrderItem(
        order_id=prior.id, practice_id=pest.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    ))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_order(
            request=OrderCreate(
                subscription_id=sub.id, client_id=client.id,
                date_from=datetime.now(timezone.utc),
                date_to=datetime.now(timezone.utc) + timedelta(days=10),
                practice_ids=[pest.id],
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "practices_already_ordered"
    assert pest.id in exc.value.detail["practice_ids"]
