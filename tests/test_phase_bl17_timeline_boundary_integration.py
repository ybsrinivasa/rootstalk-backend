"""BL-17 audit — DB-backed integration tests for the conflicts endpoint
and the refactored list_purchased_items date arithmetic.

Pure-function coverage of `compute_window` /
`find_timeline_conflicts` lives in `tests/test_bl17.py` (17 tests).
This file drives `list_timeline_conflicts` and `list_purchased_items`
directly with seeded rows in the testcontainer DB.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.advisory.models import (
    Practice, PracticeL0, Timeline, TimelineFromType,
)
from app.modules.advisory.router import list_timeline_conflicts
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import list_purchased_items
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


# ── Conflicts endpoint ────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_conflicts_endpoint_clean_package_returns_empty(db):
    """Adjacent DAS timelines (5-10, 11-20) — spec-clean, no gap, no
    overlap. Endpoint returns empty list."""
    sa = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    await make_timeline(db, package, name="TL_A",
                        from_type=TimelineFromType.DAS, from_value=5, to_value=10)
    await make_timeline(db, package, name="TL_B",
                        from_type=TimelineFromType.DAS, from_value=11, to_value=20)
    await db.commit()

    out = await list_timeline_conflicts(
        client_id=client.id, package_id=package.id,
        db=db, current_user=sa,
    )
    assert out["package_id"] == package.id
    assert out["conflict_count"] == 0
    assert out["conflicts"] == []


@requires_docker
@pytest.mark.asyncio
async def test_conflicts_endpoint_detects_gap_between_das_timelines(db):
    """(0-10) and (15-30) — days 11/12/13/14 silently uncovered.
    Pre-audit the live router didn't validate this; now surfaced as
    a GAP warning."""
    sa = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    await make_timeline(db, package, name="TL_A",
                        from_type=TimelineFromType.DAS, from_value=0, to_value=10)
    await make_timeline(db, package, name="TL_B",
                        from_type=TimelineFromType.DAS, from_value=15, to_value=30)
    await db.commit()

    out = await list_timeline_conflicts(
        client_id=client.id, package_id=package.id,
        db=db, current_user=sa,
    )
    assert out["conflict_count"] == 1
    conflict = out["conflicts"][0]
    assert conflict["kind"] == "GAP"
    assert "4-day gap" in conflict["detail"]


@requires_docker
@pytest.mark.asyncio
async def test_conflicts_endpoint_detects_overlap_between_das_timelines(db):
    """(5-10) and (8-15) overlap on 8/9/10 — flagged as OVERLAP.
    Pre-audit silently allowed, leading to duplicate-coverage days
    where both timelines fire their practices."""
    sa = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    await make_timeline(db, package, name="TL_A",
                        from_type=TimelineFromType.DAS, from_value=5, to_value=10)
    await make_timeline(db, package, name="TL_B",
                        from_type=TimelineFromType.DAS, from_value=8, to_value=15)
    await db.commit()

    out = await list_timeline_conflicts(
        client_id=client.id, package_id=package.id,
        db=db, current_user=sa,
    )
    assert out["conflict_count"] == 1
    conflict = out["conflicts"][0]
    assert conflict["kind"] == "OVERLAP"
    assert "[8, 10]" in conflict["detail"]


@requires_docker
@pytest.mark.asyncio
async def test_conflicts_endpoint_skips_calendar_timelines(db):
    """A package with two DAS timelines (clean) + one CALENDAR
    timeline (no anchor): the CALENDAR timeline doesn't appear in
    the conflict walk; the DAS pair is reported clean. Matches the
    cca_window_active convention of deferring CALENDAR."""
    sa = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    await make_timeline(db, package, name="TL_DAS1",
                        from_type=TimelineFromType.DAS, from_value=0, to_value=10)
    await make_timeline(db, package, name="TL_CAL",
                        from_type=TimelineFromType.CALENDAR, from_value=6, to_value=8)
    await make_timeline(db, package, name="TL_DAS2",
                        from_type=TimelineFromType.DAS, from_value=11, to_value=20)
    await db.commit()

    out = await list_timeline_conflicts(
        client_id=client.id, package_id=package.id,
        db=db, current_user=sa,
    )
    assert out["conflict_count"] == 0


@requires_docker
@pytest.mark.asyncio
async def test_conflicts_endpoint_detects_dbs_to_das_gap_across_sowing(db):
    """Production-style mixed config: DBS 15→8 (pre-sowing) + DAS
    0→30. Days -7 to -1 (the week before sowing) silently uncovered.
    Real-world example: a CA who configured pesticide DBS for early
    treatment and DAS for later but missed the immediate-pre-sowing
    week."""
    sa = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    await make_timeline(db, package, name="TL_DBS",
                        from_type=TimelineFromType.DBS, from_value=15, to_value=8)
    await make_timeline(db, package, name="TL_DAS",
                        from_type=TimelineFromType.DAS, from_value=0, to_value=30)
    await db.commit()

    out = await list_timeline_conflicts(
        client_id=client.id, package_id=package.id,
        db=db, current_user=sa,
    )
    assert out["conflict_count"] == 1
    conflict = out["conflicts"][0]
    assert conflict["kind"] == "GAP"
    assert "7-day gap" in conflict["detail"]


# ── list_purchased_items refactor ─────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_purchased_items_application_dates_match_cca_calendar_dates(db):
    """The refactor in batch 2 swapped inline DAS/DBS arithmetic for
    cca_calendar_dates. Pin that the route's response payload still
    produces the same dates by construction — DAS opens at
    crop_start + from_value, closes at crop_start + to_value."""
    farmer = await make_user(db, name="Farmer 17")
    dealer = await make_user(db, name="Dealer 17")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.crop_start_date = datetime(2026, 5, 1, tzinfo=timezone.utc)
    await db.commit()

    tl = await make_timeline(
        db, package, name="TL_DAS",
        from_type=TimelineFromType.DAS, from_value=10, to_value=20,
    )
    practice = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2="UREA")
    await make_element(db, practice, value="50", unit_cosh_id="kg_per_acre")

    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id,
        client_id=client.id, dealer_user_id=dealer.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        status=OrderStatus.COMPLETED,
    )
    db.add(order); await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        brand_cosh_id="brand:dithane-m45", brand_name="Dithane M-45",
        given_volume=5, volume_unit="kg", price=800,
        status=OrderItemStatus.APPROVED,
    )
    db.add(item)
    await db.commit()

    out = await list_purchased_items(db=db, current_user=farmer)
    assert len(out) == 1
    payload = out[0]
    # crop_start = 2026-05-01, from=10 → 2026-05-11. to=20 → 2026-05-21.
    assert payload["application_date_from"] == "2026-05-11"
    assert payload["application_date_to"] == "2026-05-21"


@requires_docker
@pytest.mark.asyncio
async def test_purchased_items_dbs_application_dates_use_canonical_helper(db):
    """DBS path: pin that the canonical helper handles the
    production from > to convention. crop_start=2026-05-01 with
    DBS 15→8 → from_date=2026-04-16 (15 days before),
    to_date=2026-04-23 (8 days before)."""
    farmer = await make_user(db, name="Farmer DBS 17")
    dealer = await make_user(db, name="Dealer DBS 17")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.crop_start_date = datetime(2026, 5, 1, tzinfo=timezone.utc)
    await db.commit()

    tl = await make_timeline(
        db, package, name="TL_DBS",
        from_type=TimelineFromType.DBS, from_value=15, to_value=8,
    )
    practice = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="SOIL_TREATMENT", l2="LIME")
    await make_element(db, practice, value="100", unit_cosh_id="kg_per_acre")

    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id,
        client_id=client.id, dealer_user_id=dealer.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        status=OrderStatus.COMPLETED,
    )
    db.add(order); await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        brand_cosh_id="brand:lime-x", brand_name="Lime X",
        given_volume=10, volume_unit="kg", price=500,
        status=OrderItemStatus.APPROVED,
    )
    db.add(item)
    await db.commit()

    out = await list_purchased_items(db=db, current_user=farmer)
    assert len(out) == 1
    payload = out[0]
    assert payload["application_date_from"] == "2026-04-16"  # 15 days before
    assert payload["application_date_to"] == "2026-04-23"    # 8 days before
