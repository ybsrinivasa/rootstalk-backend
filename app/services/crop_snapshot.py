"""CCA Step 1 / Batch 1B — crop attribute snapshot.

When the CA puts a crop on the conveyor belt, ClientCrop captures a
per-client snapshot of attributes that are otherwise live-fetched
from the Cosh reference tables. The snapshot freezes the company's
CCA configuration against any future Cosh-side drift, and gives a
clear audit trail of what the CA agreed to at add time.

System-level data (area/plant typing in particular) remains
canonical in `CropMeasure`. The per-client snapshot duplicates it
deliberately as defense-in-depth — if the system row ever drifts,
the per-client copy preserves the original.

Lookups (post 2026-05-09 live Cosh sync):
  • `CoshCoreItem(core_type='biological_names', cosh_id=...)` for
    the name. Cosh has consolidated crops/pests/biocontrol agents
    under one Core; the Crop classification comes from a separate
    Connect — see `cosh_crop_view.is_crop_in_cosh`.
  • `CropMeasure(crop_cosh_id=...)` for area/plant typing. Comes via
    a separate Cosh Connect that hasn't shipped yet; until it does
    `crop_missing_measure` is the expected 422.

Scientific name is a separate Cosh Core that will be linked via its
own future Connect; nothing in V1 sources it, so it's left None on
fresh snapshots. The field is preserved on `CropSnapshot` to keep
existing ClientCrop columns / API shapes stable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshCoreItem, CropMeasure
from app.services.cosh_constants import COSH_BIOLOGICAL_NAMES_CORE
from app.services.cosh_crop_view import is_crop_in_cosh


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
    cosh_row: Optional[CoshCoreItem],
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

    # scientific_name will be sourced from a separate Cosh Core +
    # Connect when those ship. Left None on V1 snapshots.
    scientific_name = None

    return CropSnapshot(
        name_en=name_en,
        scientific_name=scientific_name,
        area_or_plant=measure_row.measure,
    )


async def fetch_snapshot(db: AsyncSession, crop_cosh_id: str) -> CropSnapshot:
    """Async wrapper: load the two source rows and delegate to the
    pure builder. Used on CA add and CA re-add (fresh snapshot in
    both cases — the user explicitly chose this on 2026-05-06).

    Verifies the cosh_id is **classified as Crop** in Cosh — a Pest
    or Bio Control Agent UUID would otherwise pass the row-fetch but
    has no business being added as a crop. The classification check
    walks the `biological_names_and_roles` Connect.
    """
    cosh_row = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id == crop_cosh_id,
            CoshCoreItem.core_type == COSH_BIOLOGICAL_NAMES_CORE,
        )
    )).scalar_one_or_none()

    if cosh_row is not None and not await is_crop_in_cosh(db, crop_cosh_id):
        raise CropSnapshotError(
            "biological_name_not_classified_as_crop",
            "This biological name exists in Cosh but is not classified "
            "as a Crop (likely a Pest or Bio Control Agent). Pick from "
            "the Crops list.",
        )

    measure_row = (await db.execute(
        select(CropMeasure).where(CropMeasure.crop_cosh_id == crop_cosh_id)
    )).scalar_one_or_none()
    return build_snapshot_from_rows(cosh_row, measure_row)
