"""Crop → Measure (Area-wise vs Plant-wise) service.

Wraps the `crop_measures` table so the rest of the codebase doesn't have
to know about validation rules or the eventual Cosh-sync placeholder.

Today the values are seeded manually by SA via the admin endpoints in
`app/modules/sync/router.py`. When Cosh integration ships, those
endpoints will be supplemented (or replaced) by a sync flow that writes
`synced_from_cosh_at` on each row.

Design notes:
- Validation lives here (not in the schema) so we can extend with a
  third Measure type later without an enum migration.
- `set_measure` is upsert semantics — a CA-side change overwrites the
  same crop_cosh_id row instead of creating duplicates. Idempotent.
- Reads are cheap (one row per crop); no caching layer for now.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CropMeasure


AREA_WISE = "AREA_WISE"
PLANT_WISE = "PLANT_WISE"
VALID_MEASURES = {AREA_WISE, PLANT_WISE}


async def get_measure(db: AsyncSession, crop_cosh_id: str) -> Optional[str]:
    """Return the Measure for a crop, or None if no row exists.

    BL-06 callers should treat None as a configuration error (refuse to
    estimate) rather than silently defaulting — silent fallback would
    mask a missing seed and the SA gets no signal.
    """
    row = (await db.execute(
        select(CropMeasure).where(CropMeasure.crop_cosh_id == crop_cosh_id)
    )).scalar_one_or_none()
    return row.measure if row is not None else None


async def set_measure(
    db: AsyncSession, *, crop_cosh_id: str, measure: str,
    user_id: Optional[str] = None,
) -> CropMeasure:
    """Upsert: create or update the row for `crop_cosh_id`. Caller commits."""
    if measure not in VALID_MEASURES:
        raise ValueError(
            f"measure must be one of {sorted(VALID_MEASURES)}, got {measure!r}"
        )

    row = (await db.execute(
        select(CropMeasure).where(CropMeasure.crop_cosh_id == crop_cosh_id)
    )).scalar_one_or_none()

    if row is None:
        row = CropMeasure(
            crop_cosh_id=crop_cosh_id,
            measure=measure,
            updated_by_user_id=user_id,
        )
        db.add(row)
    else:
        row.measure = measure
        row.updated_by_user_id = user_id

    await db.flush()
    return row


async def list_measures(db: AsyncSession) -> list[CropMeasure]:
    """Return every crop_measure row, ordered by crop_cosh_id."""
    return list((await db.execute(
        select(CropMeasure).order_by(CropMeasure.crop_cosh_id)
    )).scalars().all())
