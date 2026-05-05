"""BL-05 — integration test: start-date shift respects locked timelines.

Pure-function coverage lives in `tests/test_bl05.py` (16 tests). This
file verifies the live PUT /farmer/subscriptions/{id}/start-date route
end-to-end: a timeline with a frozen snapshot keeps its frozen content
across a `crop_start_date` change, while its calendar dates shift by
the delta — exactly the behaviour the spec requires
("ALL timelines shift dates by delta_days. ONLY content of UNLOCKED
timelines updates").
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.advisory.models import (
    Element, PracticeL0, TimelineFromType,
)
from app.modules.subscriptions.router import (
    get_today_advisory, set_start_date,
)
from app.services.snapshot import take_snapshot
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


@requires_docker
@pytest.mark.asyncio
async def test_start_date_shift_keeps_locked_content_but_shifts_dates(db):
    """Lock a timeline (snapshot taken), edit master, shift crop_start
    by -10 days, render today. Locked timeline must:
      • still render with the SNAPSHOT's content (master edit ignored)
      • have calendar dates shifted by -10 days
    """
    user = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=package,
    )
    # Crop started 10 days ago → today is day 10.
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    # Wide DAS window so the timeline stays active across the shift.
    tl = await make_timeline(
        db, package, name="TL_BL05",
        from_type=TimelineFromType.DAS, from_value=0, to_value=60,
    )
    p = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2="UREA",
    )
    el = await make_element(db, p, value="50", unit_cosh_id="kg_per_acre")
    await db.commit()

    # Lock the timeline by taking a snapshot (PO trigger semantically).
    snap = await take_snapshot(db, sub.id, tl.id, "PURCHASE_ORDER", "CCA")
    original_value = next(
        pr["elements"][0]["value"] for pr in snap.content["practices"]
        if pr["id"] == p.id
    )
    assert original_value == "50"   # sanity

    # Capture calendar dates BEFORE the shift.
    out_before = await get_today_advisory(db=db, current_user=user)
    rt_before = next(r for r in out_before[0]["timelines"] if r["id"] == tl.id)
    from_d_before = date.fromisoformat(rt_before["from_date"])
    to_d_before = date.fromisoformat(rt_before["to_date"])

    # SE edits master AFTER snapshot — must not bleed through.
    el.value = "999"
    el.unit_cosh_id = "ml_per_acre"
    await db.commit()

    # Farmer corrects start date by shifting -10 days (crop started earlier).
    new_start = sub.crop_start_date - timedelta(days=10)
    out = await set_start_date(
        subscription_id=sub.id,
        data={"crop_start_date": new_start.isoformat()},
        db=db, current_user=user,
    )
    assert out["delta_days"] == -10

    # Re-render today.
    out_after = await get_today_advisory(db=db, current_user=user)
    rt_after = next(r for r in out_after[0]["timelines"] if r["id"] == tl.id)
    from_d_after = date.fromisoformat(rt_after["from_date"])
    to_d_after = date.fromisoformat(rt_after["to_date"])

    # ── Assertion 1 — calendar dates shifted by -10 days ──────────────
    assert from_d_after == from_d_before - timedelta(days=10), (
        "Locked timeline's calendar dates must shift with crop_start"
    )
    assert to_d_after == to_d_before - timedelta(days=10)

    # ── Assertion 2 — frozen content (snapshot wins over master edit) ─
    locked_practice = next(pr for pr in rt_after["practices"] if pr["id"] == p.id)
    assert locked_practice["l1_type"] == "FERTILIZER"     # snapshot value
    el_after = locked_practice["elements"][0]
    assert el_after["value"] == "50"                       # snapshot, not 999
    assert el_after["unit_cosh_id"] == "kg_per_acre"      # snapshot, not ml_per_acre
