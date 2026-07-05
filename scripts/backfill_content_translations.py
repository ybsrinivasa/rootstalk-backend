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
from app.modules.advisory.models import Package, PackageStatus, Element
from app.modules.seed_mgmt.models import SeedVariety
from app.modules.farmpundit.models import StandardResponse
from app.modules.translations.models import EntityType
from app.services.translation_ancestry import TRANSLATABLE_ELEMENT_TYPES


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


async def _enqueue_elements(dry_run: bool, limit: int | None) -> int:
    from app.tasks.translate_content import translate_field
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Element.id, Element.value).where(
                Element.element_type.in_(TRANSLATABLE_ELEMENT_TYPES),
                Element.value.is_not(None),
                Element.value != "",
            )
        )).all()
    if limit:
        rows = rows[:limit]
    for eid, _val in rows:
        if not dry_run:
            translate_field.delay(EntityType.ELEMENT_VALUE, eid, "")
    return len(rows)


async def _enqueue_standard_responses(dry_run: bool, limit: int | None) -> int:
    from app.tasks.translate_content import translate_field
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(StandardResponse.id, StandardResponse.question_text).where(
                StandardResponse.question_text.is_not(None),
                StandardResponse.question_text != "",
            )
        )).all()
    if limit:
        rows = rows[:limit]
    for srid, _q in rows:
        if not dry_run:
            translate_field.delay(
                EntityType.STANDARD_RESPONSE_QUESTION, srid, "",
            )
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
        "--entity",
        choices=("package", "seed_variety", "element", "standard_response", "all"),
        default="all",
    )
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    total = 0
    if args.entity in ("package", "all"):
        n = await _enqueue_packages(args.dry_run, args.limit)
        print(f"Package.description: {n} rows {'(dry-run)' if args.dry_run else 'enqueued'}")
        total += n
    if args.entity in ("element", "all"):
        n = await _enqueue_elements(args.dry_run, args.limit)
        print(f"Element.value: {n} rows {'(dry-run)' if args.dry_run else 'enqueued'}")
        total += n
    if args.entity in ("standard_response", "all"):
        n = await _enqueue_standard_responses(args.dry_run, args.limit)
        print(f"StandardResponse.question_text: {n} rows {'(dry-run)' if args.dry_run else 'enqueued'}")
        total += n
    if args.entity in ("seed_variety", "all"):
        n = await _enqueue_varieties(args.dry_run, args.limit)
        print(f"SeedVariety.description_points: {n} rows {'(dry-run)' if args.dry_run else 'enqueued'}")
        total += n
    print(f"Total: {total} rows")


if __name__ == "__main__":
    asyncio.run(main())
