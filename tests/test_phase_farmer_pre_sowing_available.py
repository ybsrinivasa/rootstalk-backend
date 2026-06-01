"""Fix 2026-06-01 — `/farmer/pre-sowing-available` aggregator.

Reported bug: tapping Pre-sowing inside /orders routed to
/crops-and-companies even when the farmer had nothing actionable.
The new endpoint returns a single boolean so the PWA can hide the
button when the answer is no.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.router import farmer_pre_sowing_available
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


@requires_docker
@pytest.mark.asyncio
async def test_returns_false_when_farmer_has_no_subscriptions(db):
    farmer = await make_user(db, name="F-no-subs")
    res = await farmer_pre_sowing_available(db=db, current_user=farmer)
    assert res == {"available": False, "reason": "no_subscriptions"}


@requires_docker
@pytest.mark.asyncio
async def test_returns_false_when_no_dbs_practices(db):
    """Annual subscription, window open, but the package has no DBS
    practices at all → nothing actionable."""
    farmer = await make_user(db, name="F-no-dbs")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.crop_start_date = None  # window open
    await db.commit()
    # Add a DAS practice — the aggregator should skip it.
    tl = await make_timeline(
        db, pkg, name="TL_das",
        from_type=TimelineFromType.DAS, from_value=0, to_value=20,
    )
    await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES",
    )
    await db.commit()

    res = await farmer_pre_sowing_available(db=db, current_user=farmer)
    assert res["available"] is False
    assert res.get("reason") == "nothing_remaining"


@requires_docker
@pytest.mark.asyncio
async def test_returns_true_when_at_least_one_dbs_practice_unbooked(db):
    """Annual subscription + window open + one DBS practice → True."""
    farmer = await make_user(db, name="F-has-dbs")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.crop_start_date = None
    await db.commit()
    tl = await make_timeline(
        db, pkg, name="TL_dbs",
        from_type=TimelineFromType.DBS, from_value=14, to_value=7,
    )
    await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES",
    )
    await db.commit()

    res = await farmer_pre_sowing_available(db=db, current_user=farmer)
    assert res == {"available": True}


@requires_docker
@pytest.mark.asyncio
async def test_returns_false_when_window_closed(db):
    """Crop start date in the past → DBS window has closed."""
    farmer = await make_user(db, name="F-closed")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()
    tl = await make_timeline(
        db, pkg, name="TL_dbs",
        from_type=TimelineFromType.DBS, from_value=14, to_value=7,
    )
    await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES",
    )
    await db.commit()

    res = await farmer_pre_sowing_available(db=db, current_user=farmer)
    assert res["available"] is False
