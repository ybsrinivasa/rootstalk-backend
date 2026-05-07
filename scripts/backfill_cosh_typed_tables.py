"""Schema migration — one-shot backfill of cosh_core_items + cosh_connect_rows
from the legacy cosh_reference_cache.

Walks every row in `cosh_reference_cache`, classifies it as Core or
Connect via the same logic the live sync handler uses, and upserts
into the new typed tables. Idempotent — re-runs only re-upsert; the
unique constraints (cosh_id, core_type) / (connect_id, connect_type)
make subsequent runs no-ops.

Usage (per environment, AFTER `alembic upgrade head` lands the new
tables):
    python scripts/backfill_cosh_typed_tables.py            # apply
    python scripts/backfill_cosh_typed_tables.py --dry-run  # report only

Production note: the live sync handler dual-writes since commit
0952ffb (Schema migration: sync handler dual-writes…). So new data
flows into the typed tables natively. This script only matters for
data that was synced before that commit.
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.modules.sync.models import CoshReferenceCache
from app.modules.sync.service import (
    _connect_metadata_clean, _extract_endpoints, _is_connect,
    upsert_connect_row, upsert_core_item,
)


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill_cosh")


async def backfill_one(db, row: CoshReferenceCache) -> str:
    """Upsert one legacy row into the appropriate typed table.
    Returns 'core', 'connect', or 'skip' + reason."""
    if _is_connect(row.entity_type):
        # Build endpoints from the legacy metadata-keyed shape.
        endpoints = _extract_endpoints(
            row.entity_type, {"metadata": row.metadata_},
        )
        if not endpoints:
            log.warning(
                "skip connect %s/%s: no extractable endpoints",
                row.entity_type, row.cosh_id,
            )
            return "skip:no_endpoints"
        await upsert_connect_row(
            db,
            connect_id=row.cosh_id,
            connect_type=row.entity_type,
            endpoints=endpoints,
            status=row.status,
            metadata=_connect_metadata_clean(
                row.entity_type, {"metadata": row.metadata_},
            ),
        )
        return "connect"

    # Core
    if not row.translations.get("en"):
        log.warning(
            "skip core %s/%s: missing English translation",
            row.entity_type, row.cosh_id,
        )
        return "skip:no_en"
    await upsert_core_item(
        db,
        cosh_id=row.cosh_id,
        core_type=row.entity_type,
        parent_cosh_id=row.parent_cosh_id,
        status=row.status,
        translations=row.translations,
        metadata=row.metadata_,
    )
    return "core"


async def run(dry_run: bool) -> None:
    counts = {"core": 0, "connect": 0, "skip": 0}
    skips: dict[str, int] = {}

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(CoshReferenceCache))).scalars().all()
        log.info("found %d legacy cosh_reference_cache rows", len(rows))

        for row in rows:
            if dry_run:
                tag = "connect" if _is_connect(row.entity_type) else "core"
                counts[tag] += 1
                log.info("would migrate: %s/%s → %s", row.entity_type, row.cosh_id, tag)
                continue

            outcome = await backfill_one(db, row)
            head = outcome.split(":", 1)[0]
            counts[head] = counts.get(head, 0) + 1
            if head == "skip":
                reason = outcome.split(":", 1)[1] if ":" in outcome else "unknown"
                skips[reason] = skips.get(reason, 0) + 1

        if not dry_run:
            await db.commit()

    log.info(
        "done: cores=%d connects=%d skips=%d (%s)",
        counts["core"], counts["connect"], counts["skip"],
        ", ".join(f"{k}={v}" for k, v in skips.items()) or "none",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip())
    parser.add_argument("--dry-run", action="store_true", help="Report only")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
