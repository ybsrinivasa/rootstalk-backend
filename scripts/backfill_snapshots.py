"""
Per-subscription versioning Phase 4.2 — one-shot backfill.

Walks every order_item row where snapshot_id IS NULL and:
  - takes a LockedTimelineSnapshot for (order.subscription_id, item.timeline_id,
    'CCA') if one does not already exist, with lock_trigger='BACKFILL'
  - sets order_item.snapshot_id to that snapshot's id

Pre-Phase-3 orders fall back to master rendering today; this script freezes
them retroactively so an SE edit AFTER the rollout cannot reach those farmers.

Idempotent — safe to re-run. `take_snapshot` short-circuits if a snapshot
already exists; the WHERE clause on snapshot_id IS NULL ensures only the
still-untouched items are visited each run.

Usage (per environment, AFTER 'alembic upgrade head' lands b2e5f8c1d473):
    python scripts/backfill_snapshots.py            # apply
    python scripts/backfill_snapshots.py --dry-run  # report only

Returns a summary dict (also printed) — useful for staging→prod sign-off.
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.modules.advisory.models import Timeline
from app.modules.orders.models import Order, OrderItem
from app.modules.subscriptions.snapshot_models import LockedTimelineSnapshot
from app.services.snapshot import take_snapshot

logger = logging.getLogger("backfill_snapshots")


async def backfill_snapshots(db, *, dry_run: bool = False) -> dict:
    """Backfill order_item.snapshot_id for every row currently NULL.

    Returns a summary dict:
      - items_examined: every NULL-snapshot_id row visited
      - items_linked: rows whose snapshot_id was set this run
      - snapshots_created: new BACKFILL rows written to the snapshot table
      - snapshots_reused: existing snapshots (PURCHASE_ORDER / VIEWED) that
        the backfill linked to instead of creating a new row
      - items_skipped_no_timeline: items with empty/missing timeline_id
      - items_skipped_master_deleted: items whose master Timeline row no
        longer exists (cannot serialise — defer to Phase 4 follow-up)
    """
    summary = {
        "items_examined": 0,
        "items_linked": 0,
        "snapshots_created": 0,
        "snapshots_reused": 0,
        "items_skipped_no_timeline": 0,
        "items_skipped_master_deleted": 0,
    }

    rows = (await db.execute(
        select(OrderItem, Order.subscription_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.snapshot_id.is_(None))
        .order_by(OrderItem.created_at.asc())
    )).all()

    # Cache: avoid re-checking master Timeline existence per item.
    timeline_exists: dict[str, bool] = {}
    # Cache: dedupe snapshots within this run by (subscription_id, timeline_id).
    snapshot_id_by_key: dict[tuple[str, str], str] = {}

    for item, subscription_id in rows:
        summary["items_examined"] += 1

        if not item.timeline_id:
            summary["items_skipped_no_timeline"] += 1
            continue

        key = (subscription_id, item.timeline_id)

        # Fast path — already resolved in this run.
        if key in snapshot_id_by_key:
            if not dry_run:
                item.snapshot_id = snapshot_id_by_key[key]
                summary["items_linked"] += 1
            continue

        # Look up existing snapshot first — prefer reuse to satisfy Rules 1-2.
        existing = (await db.execute(
            select(LockedTimelineSnapshot).where(
                LockedTimelineSnapshot.subscription_id == subscription_id,
                LockedTimelineSnapshot.timeline_id == item.timeline_id,
                LockedTimelineSnapshot.source == "CCA",
            )
        )).scalar_one_or_none()

        if existing is not None:
            snapshot_id_by_key[key] = existing.id
            summary["snapshots_reused"] += 1
            if not dry_run:
                item.snapshot_id = existing.id
                summary["items_linked"] += 1
            continue

        # Need to create — first verify the master Timeline still exists.
        if item.timeline_id not in timeline_exists:
            tl = (await db.execute(
                select(Timeline.id).where(Timeline.id == item.timeline_id)
            )).scalar_one_or_none()
            timeline_exists[item.timeline_id] = tl is not None
        if not timeline_exists[item.timeline_id]:
            summary["items_skipped_master_deleted"] += 1
            logger.warning(
                "skipping backfill: master timeline deleted sub=%s tl=%s item=%s",
                subscription_id, item.timeline_id, item.id,
            )
            continue

        if dry_run:
            # Pretend we'd create one; don't write.
            snapshot_id_by_key[key] = "<dry-run>"
            summary["snapshots_created"] += 1
            continue

        try:
            snap = await take_snapshot(
                db, subscription_id, item.timeline_id,
                lock_trigger="BACKFILL", source="CCA",
            )
        except Exception as exc:  # noqa: BLE001 — preserve other items
            logger.error(
                "take_snapshot failed for sub=%s tl=%s item=%s: %s",
                subscription_id, item.timeline_id, item.id, exc,
            )
            continue

        snapshot_id_by_key[key] = snap.id
        summary["snapshots_created"] += 1
        item.snapshot_id = snap.id
        summary["items_linked"] += 1

    if not dry_run:
        await db.commit()

    return summary


async def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change without writing.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    async with AsyncSessionLocal() as db:
        summary = await backfill_snapshots(db, dry_run=args.dry_run)

    mode = "DRY RUN — nothing written" if args.dry_run else "applied"
    print(f"\nBackfill {mode}.")
    for k, v in summary.items():
        print(f"  {k:35s} {v}")
    print()


if __name__ == "__main__":
    asyncio.run(_main())
