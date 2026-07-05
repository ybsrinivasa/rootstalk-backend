"""Backfill translations for existing Package.description +
SeedVariety.description_points rows.

Run once after Phase T-2 deploys. Iterates rows with non-empty source,
enqueues a translate_field Celery task per row. Idempotent: the task
hash-checks source before calling Claude.

Usage inside the api container:
    PYTHONPATH=/app python /app/scripts/backfill_content_translations.py

Options:
    --dry-run       print counts, don't enqueue
    --entity TYPE   limit to package|seed_variety (default both)
    --limit N       cap total enqueued rows (default no cap)
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.modules.advisory.models import Package, PackageStatus
from app.modules.seed_mgmt.models import SeedVariety
from app.modules.translations.models import EntityType


async def _enqueue_packages(dry_run: bool, limit: int | None) -> int:
    from app.tasks.translate_content import translate_field
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Package.id, Package.description).where(
                Package.description.is_not(None),
                Package.description != "",
                Package.status == PackageStatus.ACTIVE,
            )
        )).all()
    if limit:
        rows = rows[:limit]
    for pkg_id, _desc in rows:
        if not dry_run:
            translate_field.delay(EntityType.PACKAGE_DESCRIPTION, pkg_id, "")
    return len(rows)


async def _enqueue_varieties(dry_run: bool, limit: int | None) -> int:
    from app.tasks.translate_content import translate_field
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(SeedVariety.id, SeedVariety.description_points).where(
                SeedVariety.status == "ACTIVE",
            )
        )).all()
    filtered = [(vid, pts) for vid, pts in rows if pts]
    if limit:
        filtered = filtered[:limit]
    for vid, _pts in filtered:
        if not dry_run:
            translate_field.delay(
                EntityType.SEED_VARIETY_DESCRIPTION_POINTS, vid, "",
            )
    return len(filtered)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--entity", choices=("package", "seed_variety", "both"), default="both",
    )
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    total = 0
    if args.entity in ("package", "both"):
        n = await _enqueue_packages(args.dry_run, args.limit)
        print(f"Package.description: {n} rows {'(dry-run)' if args.dry_run else 'enqueued'}")
        total += n
    if args.entity in ("seed_variety", "both"):
        n = await _enqueue_varieties(args.dry_run, args.limit)
        print(f"SeedVariety.description_points: {n} rows {'(dry-run)' if args.dry_run else 'enqueued'}")
        total += n
    print(f"Total: {total} rows")


if __name__ == "__main__":
    asyncio.run(main())
