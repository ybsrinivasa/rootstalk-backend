"""CCA Step 1 / Batch 1B — one-shot backfill of ClientCrop attribute snapshots.

Walks every `client_crops` row whose snapshot fields are NULL and
populates `crop_name_en`, `crop_scientific_name`, `crop_area_or_plant`
from the canonical sources: `cosh_reference_cache(entity_type='crop')`
for the names and `crop_measures` for the area/plant typing.

Idempotent — re-running visits only still-NULL rows. Rows whose
source data is missing or incomplete are reported and left NULL;
SA must seed the missing reference data before the next run will
fill them.

Usage (per environment, AFTER `alembic upgrade head` lands
c0e2bf1da3a4):
    python scripts/backfill_clientcrop_snapshots.py            # apply
    python scripts/backfill_clientcrop_snapshots.py --dry-run  # report only
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import or_, select

from app.database import AsyncSessionLocal
from app.modules.clients.models import ClientCrop
from app.services.crop_snapshot import CropSnapshotError, fetch_snapshot

logger = logging.getLogger("backfill_clientcrop_snapshots")


async def backfill(db, *, dry_run: bool = False) -> dict:
    rows = (await db.execute(
        select(ClientCrop).where(
            or_(
                ClientCrop.crop_name_en.is_(None),
                ClientCrop.crop_area_or_plant.is_(None),
            )
        )
    )).scalars().all()

    examined = 0
    populated = 0
    skipped: list[dict] = []

    for row in rows:
        examined += 1
        try:
            snapshot = await fetch_snapshot(db, row.crop_cosh_id)
        except CropSnapshotError as e:
            skipped.append({
                "client_crop_id": row.id,
                "crop_cosh_id": row.crop_cosh_id,
                "code": e.code,
                "reason": e.message,
            })
            continue

        if not dry_run:
            row.crop_name_en = snapshot.name_en
            row.crop_scientific_name = snapshot.scientific_name
            row.crop_area_or_plant = snapshot.area_or_plant
        populated += 1

    if not dry_run:
        await db.commit()

    return {
        "examined": examined,
        "populated": populated,
        "skipped": len(skipped),
        "skipped_details": skipped,
        "dry_run": dry_run,
    }


async def _main(dry_run: bool):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    async with AsyncSessionLocal() as db:
        summary = await backfill(db, dry_run=dry_run)
    print(summary)
    return 0 if summary["skipped"] == 0 else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(_main(args.dry_run)))
