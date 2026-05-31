"""Orders V2 Batch 23 — CHA-derived practices become orderable via
the regular bundle path.

Critical demo scenario (2026-05-31): farmer triggers pest diagnosis
→ gets PG / SP / QA recommendation → recommendation timeline has a
practice (e.g. "Apply Spinosad") → farmer taps "Order" on that
card.

Before this batch: `compute_bundle` only loaded PACKAGE CCA
timelines. CHA-derived practices participated in dedup (Batch 20)
but were NOT included in the eligible-to-order set — so the
farmer's "Order" tap on a CHA recommendation silently did nothing.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.subscriptions.models import TriggeredCHAEntry
from app.services.order_bundle import (
    CATEGORY_PESTICIDE, compute_bundle,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_sp_practice,
    make_sp_recommendation, make_sp_timeline, make_subscription,
    make_user,
)


@requires_docker
@pytest.mark.asyncio
async def test_cha_sp_practice_is_orderable_via_bundle(db):
    """An SP-recommendation timeline triggered by the farmer must
    have its INPUT practices appear in `compute_bundle` so the
    Order CTA on the CHA card actually does something."""
    user = await make_user(db, name="Farmer Diag")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=2)
    await db.commit()

    sp = await make_sp_recommendation(
        db, client, specific_problem_cosh_id="sp:fruit_fly",
    )
    sp_tl = await make_sp_timeline(
        db, sp, name="SP-TL-FruitFly",
        from_type="DAYS_AFTER_DETECTION",
        from_value=0, to_value=5,
    )
    p_sp = await make_sp_practice(
        db, sp_tl, l0_type="INPUT", l1_type="PESTICIDE",
    )
    await make_element(
        db, p_sp, element_type="COMMON_NAME",
        value=None, unit_cosh_id=None, cosh_ref="cosh:spinosad",
    )
    # Set l2_type so it survives the category filter.
    p_sp.l2_type = "CHEMICAL_PESTICIDES"
    await db.commit()

    # Triggered today → window 0..5 days from today.
    cha = TriggeredCHAEntry(
        subscription_id=sub.id,
        farmer_user_id=user.id,
        client_id=client.id,
        recommendation_type="SP",
        recommendation_id=sp.id,
        triggered_by="DIAGNOSIS",
        triggered_at=datetime.now(timezone.utc),
        status="ACTIVE",
    )
    db.add(cha)
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=10), today=date.today(),
    )
    ids = {row["id"] for row in bundle["practices"]}
    assert p_sp.id in ids


@requires_docker
@pytest.mark.asyncio
async def test_cha_practice_out_of_window_is_excluded(db):
    """An old triggered SP whose window has already passed shouldn't
    appear in the bundle."""
    user = await make_user(db, name="Farmer Diag Old")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=2)
    await db.commit()

    sp = await make_sp_recommendation(db, client, specific_problem_cosh_id="sp:rust")
    sp_tl = await make_sp_timeline(
        db, sp, name="SP-TL-Rust",
        from_type="DAYS_AFTER_DETECTION",
        from_value=0, to_value=3,  # short window
    )
    p_sp = await make_sp_practice(
        db, sp_tl, l0_type="INPUT", l1_type="PESTICIDE",
    )
    await make_element(db, p_sp, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:mancozeb")
    p_sp.l2_type = "CHEMICAL_PESTICIDES"
    await db.commit()

    # Triggered 10 days ago → window 0..3 days from then closed 7 days ago.
    cha = TriggeredCHAEntry(
        subscription_id=sub.id,
        farmer_user_id=user.id,
        client_id=client.id,
        recommendation_type="SP",
        recommendation_id=sp.id,
        triggered_by="DIAGNOSIS",
        triggered_at=datetime.now(timezone.utc) - timedelta(days=10),
        status="ACTIVE",
    )
    db.add(cha)
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=5), today=date.today(),
    )
    ids = {row["id"] for row in bundle["practices"]}
    assert p_sp.id not in ids
