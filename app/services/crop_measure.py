"""Crop → Measure (Area-wise vs Plant-wise) — Cosh-sourced (Round 3, 2026-05-09).

Pre-Round-3 this file wrapped the local `crop_measures` table with `get_measure`
/ `set_measure` / `list_measures`. Cosh now owns this data: the
`crop_area_plant_wise` Connect links each Crop biological_name to one of two
Core items (Area-wise / Plant-wise), and RootsTalk reads through.

Public API surface (callers continue to import these names):
  • `get_measure(db, cosh_id)` — None when Cosh hasn't classified yet.
  • `AREA_WISE` / `PLANT_WISE` / `VALID_MEASURES` — string tokens for
    downstream comparisons (BL-06, plant-wise additional elements, etc.).

`set_measure` and `list_measures` were removed. Cosh is the writer; the
SA admin endpoints under `/admin/crop-measures` either read through to
Cosh or were dropped (see `app/modules/sync/router.py`).

The local `crop_measures` table is no longer read or written by
production code. Schema cleanup is a separate ticket.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.cosh_crop_view import get_measure_for_biological_name


AREA_WISE = "AREA_WISE"
PLANT_WISE = "PLANT_WISE"
VALID_MEASURES = {AREA_WISE, PLANT_WISE}


async def get_measure(db: AsyncSession, crop_cosh_id: str) -> Optional[str]:
    """Return the Measure for a crop, or None if Cosh hasn't classified it yet.

    BL-06 callers should treat None as a configuration error (refuse to
    estimate) rather than silently defaulting — silent fallback would
    mask a missing classification on the Cosh side.
    """
    return await get_measure_for_biological_name(db, crop_cosh_id)
