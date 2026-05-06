"""CCA Step 1 / Batch 1B — crop attribute snapshot.

When the CA puts a crop on the conveyor belt, ClientCrop captures a
per-client snapshot of attributes that are otherwise live-fetched
from the Cosh reference cache. The snapshot freezes the company's
CCA configuration against any future Cosh-side drift, and gives a
clear audit trail of what the CA agreed to at add time.

System-level data (area/plant typing in particular) remains
canonical in `CropMeasure`. The per-client snapshot duplicates it
deliberately as defense-in-depth — if the system row ever drifts,
the per-client copy preserves the original.

Lookups: `CoshReferenceCache(entity_type='crop', cosh_id=...)` for
name + scientific name, `CropMeasure(crop_cosh_id=...)` for
area/plant. Either missing is a configuration error the CA can't
fix from the portal — `CropSnapshotError` carries a stable
`code` so the route can map to a 422 with a clear reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshReferenceCache, CropMeasure


@dataclass(frozen=True)
class CropSnapshot:
    name_en: str
    scientific_name: Optional[str]
    area_or_plant: str


class CropSnapshotError(Exception):
    """Raised when snapshot fields can't be extracted. `code` is a
    stable identifier for the route layer to map to error responses."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def build_snapshot_from_rows(
    cosh_row: Optional[CoshReferenceCache],
    measure_row: Optional[CropMeasure],
) -> CropSnapshot:
    """Pure: assemble a snapshot from already-loaded ORM rows.

    Raises `CropSnapshotError` for the four ways this can go wrong:
    cosh row missing, cosh row inactive, English name missing,
    measure row missing. The router maps each to a 422 with the
    `code` carried on the exception.
    """
    if cosh_row is None:
        raise CropSnapshotError(
            "crop_not_in_cosh",
            "This crop is not in the Cosh reference cache. Ask SA to sync it first.",
        )
    if cosh_row.status != "active":
        raise CropSnapshotError(
            "crop_inactive_in_cosh",
            "This crop is marked inactive in Cosh and cannot be added to a company.",
        )

    translations = cosh_row.translations or {}
    name_en = translations.get("en")
    if not name_en:
        raise CropSnapshotError(
            "crop_missing_english_name",
            "This crop has no English translation in Cosh. Ask SA to fix the entry.",
        )

    if measure_row is None:
        raise CropSnapshotError(
            "crop_missing_measure",
            "This crop has no AREA-wise / PLANT-wise mapping. "
            "Ask SA to seed crop_measures before adding the crop.",
        )

    metadata = cosh_row.metadata_ or {}
    scientific_name = metadata.get("scientific_name") or None

    return CropSnapshot(
        name_en=name_en,
        scientific_name=scientific_name,
        area_or_plant=measure_row.measure,
    )


async def fetch_snapshot(db: AsyncSession, crop_cosh_id: str) -> CropSnapshot:
    """Async wrapper: load the two source rows and delegate to the
    pure builder. Used on CA add and CA re-add (fresh snapshot in
    both cases — the user explicitly chose this on 2026-05-06)."""
    cosh_row = (await db.execute(
        select(CoshReferenceCache).where(
            CoshReferenceCache.cosh_id == crop_cosh_id,
            CoshReferenceCache.entity_type == "crop",
        )
    )).scalar_one_or_none()
    measure_row = (await db.execute(
        select(CropMeasure).where(CropMeasure.crop_cosh_id == crop_cosh_id)
    )).scalar_one_or_none()
    return build_snapshot_from_rows(cosh_row, measure_row)
