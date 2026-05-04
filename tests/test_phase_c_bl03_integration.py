"""BL-03 Phase C.1 — integration tests against /farmer/advisory/today.

The pure-function suite at tests/test_bl03.py covers the algorithm
(11 tests, all 10 spec rules). This file verifies that the live route
correctly assembles inputs for it: real Timeline + Practice + Element +
Order + TriggeredCHAEntry rows flow through the snapshot-render path
into deduplicate_advisory(), and the response payload reflects the
suppression as the spec requires.

Three scenarios mirror the most load-bearing rules:
  - Overlapping CCA dedup with a shared common-name input
  - Purchased rule: suppression survives the earlier timeline's closure
  - Cross-source dedup: CCA governs an overlapping CHA SP timeline
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.modules.advisory.models import (
    PracticeL0, TimelineFromType,
)
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.subscriptions.models import TriggeredCHAEntry
from app.modules.subscriptions.router import get_today_advisory
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_practice,
    make_sp_element, make_sp_practice, make_sp_recommendation,
    make_sp_timeline, make_subscription, make_timeline, make_user,
)


COMMON_NAME_UREA = "cosh:input:urea"


async def _input_practice_with_identity(
    db, timeline, *, common_name_cosh: str = COMMON_NAME_UREA,
    l1: str = "FERTILIZER", l2: str = "UREA",
):
    """Create an INPUT practice with a common_name element so
    primary_identity_ref() returns the cosh ref. BL-03 dedupes inputs by
    that identity."""
    p = await make_practice(
        db, timeline, l0=PracticeL0.INPUT, l1=l1, l2=l2,
    )
    await make_element(
        db, p, element_type="common_name", value=None,
        unit_cosh_id=None, cosh_ref=common_name_cosh,
    )
    return p


# ── Scenario 1: overlapping CCA dedup ───────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_overlapping_cca_dedup_earlier_governs(db):
    """Two CCA timelines, both DAS-active today, both contain Urea.
    Earlier from_date governs; later timeline shows the input as
    suppressed (excluded from `practices`, counted in suppressed_count).
    """
    user = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=package,
    )
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=15)
    await db.commit()

    # TL_A: DAS 0..30. Active today (day 15). Earlier from_date.
    tl_a = await make_timeline(
        db, package, name="TL_A",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
        display_order=0,
    )
    await _input_practice_with_identity(db, tl_a)

    # TL_B: DAS 10..40. Active today, overlaps TL_A on days 10..30.
    tl_b = await make_timeline(
        db, package, name="TL_B",
        from_type=TimelineFromType.DAS, from_value=10, to_value=40,
        display_order=1,
    )
    await _input_practice_with_identity(db, tl_b)
    await db.commit()

    out = await get_today_advisory(db=db, current_user=user)
    timelines_by_id = {t["id"]: t for t in out[0]["timelines"]}
    assert tl_a.id in timelines_by_id
    assert tl_b.id in timelines_by_id

    rt_a = timelines_by_id[tl_a.id]
    rt_b = timelines_by_id[tl_b.id]

    # TL_A retains its Urea practice; TL_B has it suppressed.
    assert any(p["l2_type"] == "UREA" for p in rt_a["practices"])
    assert not any(p["l2_type"] == "UREA" for p in rt_b["practices"])
    assert rt_b["suppressed_count"] == 1
    assert rt_a["suppressed_count"] == 0


# ── Scenario 2: purchased rule survives earlier timeline closure ────────────

@requires_docker
@pytest.mark.asyncio
async def test_purchased_input_stays_suppressed_after_earlier_closes(db):
    """TL_A and TL_B overlap. Farmer's order in TL_A's Urea practice is
    APPROVED. TL_A's window then closes (today moves past it). TL_B is
    still active. Spec rule: TL_B's Urea remains suppressed because the
    farmer already bought it — even though the governing window is gone.
    Without the purchased rule, the farmer would see Urea pop back into
    visibility on TL_B and be told to buy it again.
    """
    user = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=package,
    )
    # day_offset = 20: TL_A (0..15) closed; TL_B (10..30) active.
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=20)
    await db.commit()

    tl_a = await make_timeline(
        db, package, name="TL_A_closed",
        from_type=TimelineFromType.DAS, from_value=0, to_value=15,
        display_order=0,
    )
    p_a = await _input_practice_with_identity(db, tl_a)

    tl_b = await make_timeline(
        db, package, name="TL_B_active",
        from_type=TimelineFromType.DAS, from_value=10, to_value=30,
        display_order=1,
    )
    await _input_practice_with_identity(db, tl_b)
    await db.commit()

    # Order with APPROVED item against tl_a.p_a (the Urea purchase).
    order = Order(
        subscription_id=sub.id,
        farmer_user_id=user.id,
        client_id=client.id,
        date_from=datetime.now(timezone.utc) - timedelta(days=18),
        date_to=datetime.now(timezone.utc) - timedelta(days=15),
        status=OrderStatus.COMPLETED,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=p_a.id, timeline_id=tl_a.id,
        status=OrderItemStatus.APPROVED, snapshot_id=None,
    ))
    await db.commit()

    out = await get_today_advisory(db=db, current_user=user)
    timelines_by_id = {t["id"]: t for t in out[0]["timelines"]}

    # TL_A is closed → not in active_timelines → not in response.
    assert tl_a.id not in timelines_by_id
    # TL_B is active and Urea is suppressed (purchased rule).
    assert tl_b.id in timelines_by_id
    rt_b = timelines_by_id[tl_b.id]
    assert not any(p["l2_type"] == "UREA" for p in rt_b["practices"]), (
        "Urea should stay suppressed in TL_B because it was purchased "
        "in (now-closed) TL_A — Purchased rule"
    )
    assert rt_b["suppressed_count"] == 1


# ── Scenario 3: CCA governs an overlapping CHA SP timeline ──────────────────

@requires_docker
@pytest.mark.asyncio
async def test_cca_governs_overlapping_cha_sp_input(db):
    """A CCA timeline and a CHA SP timeline both contain Mancozeb today.
    CCA's from_date is earlier (set by crop_start), so CCA governs and
    the CHA SP entry's Mancozeb is suppressed. Cross-source dedup —
    not covered by the pure-function suite's same-source fixtures."""
    MANCOZEB = "cosh:input:mancozeb"

    user = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=package,
    )
    # day_offset = 20 → CCA from_date is today-20 (anchored to crop_start)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=20)
    await db.commit()

    # CCA timeline DAS 0..30, active.
    tl_cca = await make_timeline(
        db, package, name="CCA_TL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    await _input_practice_with_identity(
        db, tl_cca, common_name_cosh=MANCOZEB,
        l1="PESTICIDE", l2="MANCOZEB",
    )

    # CHA SP triggered 5 days ago, window 0..14 → active today.
    sp_rec = await make_sp_recommendation(db, client)
    sp_tl = await make_sp_timeline(
        db, sp_rec, name="CHA_SP", from_value=0, to_value=14,
    )
    sp_p = await make_sp_practice(db, sp_tl, l1_type="PESTICIDE")
    await make_sp_element(
        db, sp_p, element_type="common_name", value=None, cosh_ref=MANCOZEB,
    )

    triggered_at_dt = datetime.now(timezone.utc) - timedelta(days=5)
    db.add(TriggeredCHAEntry(
        subscription_id=sub.id,
        farmer_user_id=user.id,
        client_id=client.id,
        problem_cosh_id="problem:test",
        recommendation_type="SP",
        recommendation_id=sp_rec.id,
        triggered_by="DIAGNOSIS",
        triggered_at=triggered_at_dt,
        status="ACTIVE",
        problem_name="Test Problem",
    ))
    await db.commit()

    out = await get_today_advisory(db=db, current_user=user)
    timelines_by_id = {t["id"]: t for t in out[0]["timelines"]}

    cha_tl_id = f"cha-sp-{sp_tl.id}"
    assert tl_cca.id in timelines_by_id, "CCA must render"
    assert cha_tl_id in timelines_by_id, "CHA SP must render"

    rt_cca = timelines_by_id[tl_cca.id]
    rt_cha = timelines_by_id[cha_tl_id]

    # CCA owns Mancozeb; CHA's Mancozeb is suppressed.
    assert any(p["l2_type"] == "MANCOZEB" for p in rt_cca["practices"])
    assert not any(p["l2_type"] == "MANCOZEB" for p in rt_cha["practices"]), (
        "CHA's Mancozeb should be suppressed by the earlier-from_date CCA"
    )
    assert rt_cha["suppressed_count"] == 1
