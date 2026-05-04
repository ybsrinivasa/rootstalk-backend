"""Phase 3.1 — today route reads from snapshot when present.

Calls `get_today_advisory` directly (the route function) rather than going
through HTTP — simpler test surface, same code. Verifies:

  - First view takes a snapshot for every in-window CCA timeline.
  - Repeat view does NOT create a duplicate snapshot.
  - SE edits to master Practice rows AFTER the snapshot do NOT bleed into
    the farmer's response (Rules 1 & 2).
  - SE shrinking a master window does NOT remove a timeline from the
    farmer's view if the snapshot's frozen window still includes today
    (Rule 3 — frozen-window).
  - Timelines whose effective window does NOT include today are not
    snapshotted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.advisory.models import (
    PracticeL0, Timeline, TimelineFromType,
)
from app.modules.subscriptions.snapshot_models import LockedTimelineSnapshot
from app.modules.subscriptions.router import get_today_advisory
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


async def _seed_today_active(db, *, day_offset: int, das_to: int = 30):
    """Seed user/client/package/sub/timeline so that today has the given
    day_offset relative to crop_start_date and falls inside [0, das_to].
    """
    user = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=package,
    )
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=day_offset)
    await db.commit()

    tl = await make_timeline(
        db, package, name="TL_active",
        from_type=TimelineFromType.DAS, from_value=0, to_value=das_to,
    )
    p = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2="UREA",
    )
    await make_element(db, p, value="50", unit_cosh_id="kg_per_acre")
    return user, sub, tl, p


async def _count_snapshots(db, sub_id: str, tl_id: str, source: str = "CCA") -> int:
    rows = (await db.execute(
        select(LockedTimelineSnapshot).where(
            LockedTimelineSnapshot.subscription_id == sub_id,
            LockedTimelineSnapshot.timeline_id == tl_id,
            LockedTimelineSnapshot.source == source,
        )
    )).scalars().all()
    return len(rows)


# ── Tests ───────────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_today_first_view_takes_snapshot(db):
    user, sub, tl, _p = await _seed_today_active(db, day_offset=10)
    assert await _count_snapshots(db, sub.id, tl.id) == 0

    out = await get_today_advisory(db=db, current_user=user)

    assert len(out) == 1
    assert out[0]["subscription_id"] == sub.id
    rendered_tls = out[0]["timelines"]
    assert any(rt["id"] == tl.id for rt in rendered_tls), (
        "active timeline must appear in response"
    )
    assert await _count_snapshots(db, sub.id, tl.id) == 1


@requires_docker
@pytest.mark.asyncio
async def test_today_repeat_view_idempotent(db):
    user, sub, tl, _p = await _seed_today_active(db, day_offset=10)
    await get_today_advisory(db=db, current_user=user)
    await get_today_advisory(db=db, current_user=user)
    await get_today_advisory(db=db, current_user=user)
    assert await _count_snapshots(db, sub.id, tl.id) == 1


@requires_docker
@pytest.mark.asyncio
async def test_today_renders_snapshot_after_master_practice_edit(db):
    """SE edits master practice AFTER snapshot — farmer still sees frozen value."""
    user, sub, tl, p = await _seed_today_active(db, day_offset=10)
    out1 = await get_today_advisory(db=db, current_user=user)
    rt1 = next(rt for rt in out1[0]["timelines"] if rt["id"] == tl.id)
    practices1 = rt1["practices"]
    assert any(pr["l1_type"] == "FERTILIZER" for pr in practices1)

    # SE edits master. The snapshot must NOT pick this up.
    p.l1_type = "PESTICIDE"
    p.l2_type = "MANCOZEB"
    await db.commit()

    out2 = await get_today_advisory(db=db, current_user=user)
    rt2 = next(rt for rt in out2[0]["timelines"] if rt["id"] == tl.id)
    practices2 = rt2["practices"]
    assert any(pr["l1_type"] == "FERTILIZER" for pr in practices2), (
        "frozen snapshot must override master edit (Rules 1 & 2)"
    )
    assert not any(pr["l1_type"] == "PESTICIDE" for pr in practices2), (
        "master edit must not bleed through to a locked farmer"
    )


@requires_docker
@pytest.mark.asyncio
async def test_today_renders_snapshot_after_master_window_shrinks(db):
    """SE shrinks master window — locked farmer's frozen window still applies (Rule 3)."""
    user, sub, tl, _p = await _seed_today_active(db, day_offset=10, das_to=30)
    # First view → snapshot frozen with window 0..30.
    await get_today_advisory(db=db, current_user=user)

    # SE shrinks master so today (day 10) is outside the master window.
    tl_row = (await db.execute(
        select(Timeline).where(Timeline.id == tl.id)
    )).scalar_one()
    tl_row.to_value = 5
    await db.commit()

    out = await get_today_advisory(db=db, current_user=user)
    rt = next(
        (rt for rt in out[0]["timelines"] if rt["id"] == tl.id), None,
    )
    assert rt is not None, (
        "frozen window 0..30 still includes day 10; timeline must remain visible"
    )


@requires_docker
@pytest.mark.asyncio
async def test_today_renders_snapshot_after_master_element_edit(db):
    """Element-level isolation — SE changing dosage value AFTER snapshot must
    not bleed into the locked farmer's response."""
    from app.modules.advisory.models import Element

    user, _sub, tl, p = await _seed_today_active(db, day_offset=10)
    out1 = await get_today_advisory(db=db, current_user=user)
    rt1 = next(rt for rt in out1[0]["timelines"] if rt["id"] == tl.id)
    snap_practice = next(pr for pr in rt1["practices"] if pr["id"] == p.id)
    assert snap_practice["elements"][0]["value"] == "50"

    # SE bumps the dosage in master.
    el = (await db.execute(
        select(Element).where(Element.practice_id == p.id)
    )).scalar_one()
    el.value = "999"
    el.unit_cosh_id = "ml_per_acre"
    await db.commit()

    out2 = await get_today_advisory(db=db, current_user=user)
    rt2 = next(rt for rt in out2[0]["timelines"] if rt["id"] == tl.id)
    snap_practice2 = next(pr for pr in rt2["practices"] if pr["id"] == p.id)
    assert snap_practice2["elements"][0]["value"] == "50"
    assert snap_practice2["elements"][0]["unit_cosh_id"] == "kg_per_acre"


@requires_docker
@pytest.mark.asyncio
async def test_today_outside_window_takes_no_snapshot(db):
    """Window 0..5; subscription day_offset = 50 → no snapshot."""
    user, sub, tl, _p = await _seed_today_active(db, day_offset=50, das_to=5)
    out = await get_today_advisory(db=db, current_user=user)
    rt_ids = [rt["id"] for rt in out[0]["timelines"]]
    assert tl.id not in rt_ids, "timeline must not render — out of window"
    assert await _count_snapshots(db, sub.id, tl.id) == 0
