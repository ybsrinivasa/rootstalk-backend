"""Vertical-slice tests for the Client Reports dashboard (Phase 1).

Covers the ``subs_active`` query end-to-end — the first metric wired
through queries.py + dashboard_router.py + access.py.

The core assertion is the FILTER CONTRACT from
`project_rootstalk_client_reports_phase_1.md`:

- Client scoping (nothing cross-client leaks).
- Training exclusion (Client.is_training=True rows must not count).
- Soft-delete cascade (Subscription.deleted_at IS NULL, auto via the
  session listener).
- Status filter (only SubscriptionStatus.ACTIVE counts).
- DISTINCT farmers (a farmer with 2 active subs on the same client
  counts once in the ``farmers`` number, twice in ``subscriptions``).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)

from app.modules.clients.models import Client
from app.modules.reports.queries import ReportFilters, subs_active
from app.modules.subscriptions.models import Subscription, SubscriptionStatus


pytestmark = [pytest.mark.asyncio, requires_docker]


async def test_subs_active_filter_contract(db):
    """Exercises every filter in the contract in one setup so a
    regression in any single filter shows up loud.
    """
    # Parent (real) client under audit.
    parent = await make_client(db, full_name="Parent Client")

    # Second real client — sanity that client_id scoping keeps its
    # subscriptions out of parent's report.
    other = await make_client(db, full_name="Other Client")

    # Training shadow child of parent. Its subs must NOT count.
    training_child = Client(
        full_name="Parent Training",
        short_name="pt-" + parent.short_name[:8],
        ca_name="T", ca_phone="+919", ca_email="t@t.local",
        payment_model=parent.payment_model,
        is_training=True,
        parent_client_id=parent.id,
    )
    db.add(training_child)
    await db.flush()

    # Farmers. farmer_a has TWO active subs on parent (dedup test).
    farmer_a = await make_user(db, name="Farmer A")
    farmer_b = await make_user(db, name="Farmer B")
    farmer_c = await make_user(db, name="Farmer C")  # training-only
    farmer_d = await make_user(db, name="Farmer D")  # other-client-only

    pkg_parent_1 = await make_package(db, parent, name="PoP Parent 1")
    pkg_parent_2 = await make_package(db, parent, name="PoP Parent 2")
    pkg_training = await make_package(db, training_child, name="PoP Training")
    pkg_other = await make_package(db, other, name="PoP Other")

    # SHOULD COUNT (parent, ACTIVE): farmer_a x2, farmer_b x1  → subs=3, farmers=2
    await make_subscription(db, farmer=farmer_a, client=parent, package=pkg_parent_1)
    await make_subscription(db, farmer=farmer_a, client=parent, package=pkg_parent_2)
    await make_subscription(db, farmer=farmer_b, client=parent, package=pkg_parent_1)

    # SHOULD NOT COUNT — status != ACTIVE.
    lapsed = await make_subscription(
        db, farmer=farmer_b, client=parent, package=pkg_parent_2,
    )
    lapsed.status = SubscriptionStatus.LAPSED
    cancelled = await make_subscription(
        db, farmer=farmer_b, client=parent, package=pkg_parent_1,
    )
    cancelled.status = SubscriptionStatus.CANCELLED

    # SHOULD NOT COUNT — soft-deleted.
    soft_deleted = await make_subscription(
        db, farmer=farmer_a, client=parent, package=pkg_parent_1,
    )
    soft_deleted.deleted_at = datetime.now(timezone.utc)

    # SHOULD NOT COUNT — training client.
    await make_subscription(
        db, farmer=farmer_c, client=training_child, package=pkg_training,
    )

    # SHOULD NOT COUNT — different client.
    await make_subscription(
        db, farmer=farmer_d, client=other, package=pkg_other,
    )

    await db.flush()

    result = await subs_active(db, parent.id, ReportFilters())

    assert result == {"subscriptions": 3, "farmers": 2}


async def test_subs_active_empty_client_returns_zero(db):
    client = await make_client(db, full_name="Empty Client")

    result = await subs_active(db, client.id, ReportFilters())

    assert result == {"subscriptions": 0, "farmers": 0}


async def test_subs_active_narrows_by_each_filter(db):
    """Each of the four chip filters must scope the result independently.

    The unfiltered baseline is 3 active subs across 2 farmers on 2
    packages / 2 crops / 2 districts. Every filter should carve out
    exactly the expected subset.
    """
    client = await make_client(db, full_name="Filter Test Client")

    farmer_north = await make_user(db, name="Farmer North")
    farmer_north.state_cosh_id = "state:north"
    farmer_north.district_cosh_id = "district:north-a"

    farmer_south = await make_user(db, name="Farmer South")
    farmer_south.state_cosh_id = "state:south"
    farmer_south.district_cosh_id = "district:south-b"

    pkg_maize = await make_package(
        db, client, name="Maize PoP", crop_cosh_id="crop:maize",
    )
    pkg_wheat = await make_package(
        db, client, name="Wheat PoP", crop_cosh_id="crop:wheat",
    )

    # 3 active subs on 2 farmers:
    #   north-a → Maize x1
    #   north-a → Wheat x1     (same farmer, second crop)
    #   south-b → Maize x1
    await make_subscription(db, farmer=farmer_north, client=client, package=pkg_maize)
    await make_subscription(db, farmer=farmer_north, client=client, package=pkg_wheat)
    await make_subscription(db, farmer=farmer_south, client=client, package=pkg_maize)

    await db.flush()

    # Baseline.
    assert await subs_active(db, client.id, ReportFilters()) == {
        "subscriptions": 3, "farmers": 2,
    }

    # Crop chip.
    assert await subs_active(
        db, client.id, ReportFilters(crop_cosh_id="crop:maize"),
    ) == {"subscriptions": 2, "farmers": 2}

    # State chip.
    assert await subs_active(
        db, client.id, ReportFilters(state_cosh_id="state:north"),
    ) == {"subscriptions": 2, "farmers": 1}

    # District chip.
    assert await subs_active(
        db, client.id, ReportFilters(district_cosh_id="district:south-b"),
    ) == {"subscriptions": 1, "farmers": 1}

    # Package chip.
    assert await subs_active(
        db, client.id, ReportFilters(package_id=pkg_wheat.id),
    ) == {"subscriptions": 1, "farmers": 1}

    # Combination — Crop AND State (AND semantics; not OR).
    assert await subs_active(
        db, client.id,
        ReportFilters(crop_cosh_id="crop:maize", state_cosh_id="state:north"),
    ) == {"subscriptions": 1, "farmers": 1}
