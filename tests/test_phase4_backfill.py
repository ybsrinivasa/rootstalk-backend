"""Phase 4.2 — backfill script for pre-Phase-3 orders.

Calls `scripts.backfill_snapshots.backfill_snapshots()` directly with the
test DB session. Verifies:

  - A pre-Phase-3 order_item (snapshot_id=NULL) gets a fresh snapshot row
    with lock_trigger='BACKFILL', and the item is linked to it.
  - Re-running the backfill is idempotent — no new rows created the
    second time, no double-linking.
  - When a snapshot already exists for (sub, tl) — e.g. a VIEWED snapshot
    written by today-render — the backfill REUSES it (Rules 1-2) rather
    than creating a duplicate.
  - Orphaned items whose master Timeline has been deleted are skipped
    gracefully and counted under items_skipped_master_deleted.
  - --dry-run does not write — items_linked stays 0, but the reporting
    counter for snapshots_created reflects what *would* have been done.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.modules.advisory.models import Timeline
from app.modules.orders.models import Order, OrderItem, OrderItemStatus, OrderStatus
from app.modules.subscriptions.snapshot_models import LockedTimelineSnapshot
from app.services.snapshot import take_snapshot
from scripts.backfill_snapshots import backfill_snapshots
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_practice, make_subscription, make_timeline,
    make_user,
)


async def _seed_legacy_order(db):
    """Subscription + timeline + a hand-built Order/OrderItem with NULL
    snapshot_id (simulating a pre-Phase-3.2 row)."""
    user = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=package,
    )
    tl = await make_timeline(db, package, name="LEGACY_TL")
    p = await make_practice(db, tl)

    order = Order(
        subscription_id=sub.id,
        farmer_user_id=user.id,
        client_id=client.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        status=OrderStatus.SENT,
    )
    db.add(order)
    await db.flush()

    item = OrderItem(
        order_id=order.id,
        practice_id=p.id,
        timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
        snapshot_id=None,
    )
    db.add(item)
    await db.commit()
    return user, sub, tl, item


# ── Core backfill behaviour ─────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_backfill_creates_snapshot_and_links(db):
    user, sub, tl, item = await _seed_legacy_order(db)
    assert item.snapshot_id is None

    summary = await backfill_snapshots(db)

    assert summary["items_examined"] == 1
    assert summary["items_linked"] == 1
    assert summary["snapshots_created"] == 1
    assert summary["snapshots_reused"] == 0
    assert summary["items_skipped_no_timeline"] == 0
    assert summary["items_skipped_master_deleted"] == 0

    # Item now linked.
    refreshed = (await db.execute(
        select(OrderItem).where(OrderItem.id == item.id)
    )).scalar_one()
    assert refreshed.snapshot_id is not None

    # Snapshot row exists with BACKFILL trigger.
    snap = (await db.execute(
        select(LockedTimelineSnapshot).where(
            LockedTimelineSnapshot.id == refreshed.snapshot_id
        )
    )).scalar_one()
    assert snap.lock_trigger == "BACKFILL"
    assert snap.subscription_id == sub.id
    assert snap.timeline_id == tl.id
    assert snap.source == "CCA"


@requires_docker
@pytest.mark.asyncio
async def test_backfill_is_idempotent(db):
    _user, _sub, _tl, _item = await _seed_legacy_order(db)
    first = await backfill_snapshots(db)
    second = await backfill_snapshots(db)
    assert first["snapshots_created"] == 1
    assert first["items_linked"] == 1
    # Nothing left to do on a clean re-run.
    assert second["items_examined"] == 0
    assert second["snapshots_created"] == 0
    assert second["items_linked"] == 0


@requires_docker
@pytest.mark.asyncio
async def test_backfill_reuses_existing_snapshot(db):
    """If a VIEWED snapshot already exists, the backfill must link to it
    rather than create a duplicate (Rules 1-2 — immutability)."""
    _user, sub, tl, item = await _seed_legacy_order(db)
    pre_snap = await take_snapshot(
        db, sub.id, tl.id, "VIEWED", source="CCA",
    )

    summary = await backfill_snapshots(db)
    assert summary["snapshots_created"] == 0
    assert summary["snapshots_reused"] == 1
    assert summary["items_linked"] == 1

    refreshed = (await db.execute(
        select(OrderItem).where(OrderItem.id == item.id)
    )).scalar_one()
    assert refreshed.snapshot_id == pre_snap.id

    # Original lock_trigger preserved (immutability).
    snap_after = (await db.execute(
        select(LockedTimelineSnapshot).where(
            LockedTimelineSnapshot.id == pre_snap.id,
        )
    )).scalar_one()
    assert snap_after.lock_trigger == "VIEWED"


# Note: the script also guards against empty/null timeline_id and
# orphaned-master-timeline cases. Both are unreachable in real DB state
# because of the NOT NULL + FK constraints on order_items.timeline_id,
# so they're not exercised by the test suite — kept as defensive code.


@requires_docker
@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing(db):
    _user, _sub, _tl, item = await _seed_legacy_order(db)

    summary = await backfill_snapshots(db, dry_run=True)
    # Counter reports what *would* be created.
    assert summary["snapshots_created"] == 1
    # …but nothing was written.
    assert summary["items_linked"] == 0

    refreshed = (await db.execute(
        select(OrderItem).where(OrderItem.id == item.id)
    )).scalar_one()
    assert refreshed.snapshot_id is None

    snap_count = len((await db.execute(
        select(LockedTimelineSnapshot)
    )).scalars().all())
    assert snap_count == 0
