"""Cosh crop derivation — Round 1 of the live data sync (2026-05-09).

Cosh consolidates all biological identifiers under one Core
(`biological_names`); semantic role (Crop / Pest / Bio Control Agent)
is expressed via the `biological_names_and_roles` Connect linking each
name to one of the items in `roles_of_biological_names`. RootsTalk's
"list available crops" view derives the crop subset by walking that
Connect and keeping only names tagged with the Crop role's UUID.

This service is the single boundary that interprets Cosh's polymorphic
shape; everything downstream (SA-portal CM picker, CA-portal "Add
Crop" picker, `crop_snapshot.fetch_snapshot`) consumes the resolved
output.

Performance: the Connect carries ~2,200 rows in the V1 universe and
this service is hit only on CM/CA browse + CA add. Whole-table scan in
Python (~5–10 ms) beats the cost of a JSONB cast on every CA add. If
scale demands later, switch to `endpoints::jsonb @> '...'` predicates.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_AREA_PLANT_WISE_CORE,
    COSH_BIOLOGICAL_NAMES_CORE,
    COSH_CROP_AREA_PLANT_CONNECT,
    COSH_NAME_ROLE_CONNECT,
    COSH_ROLE_CROP_UUID,
    COSH_UUID_TO_MEASURE,
    ENDPOINT_ROLE_BIOLOGICAL_NAME,
    ENDPOINT_ROLE_OF_NAME,
)


async def _crop_classified_biological_name_ids(db: AsyncSession) -> set[str]:
    """Walk every active `biological_names_and_roles` Connect row and
    return the set of biological_name cosh_ids whose role endpoint
    points at the Crop UUID. Inactive rows are skipped — Cosh marks
    a Connect inactive when the curator unlinks the classification."""
    rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_NAME_ROLE_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()

    crop_ids: set[str] = set()
    for row in rows:
        endpoints = row.endpoints or []
        # A row classifies a name as Crop iff one of its endpoints
        # is the Crop role UUID with role == roles_of_biological_names.
        is_crop_row = any(
            ep.get("role") == ENDPOINT_ROLE_OF_NAME
            and ep.get("cosh_id") == COSH_ROLE_CROP_UUID
            for ep in endpoints
        )
        if not is_crop_row:
            continue
        for ep in endpoints:
            if ep.get("role") == ENDPOINT_ROLE_BIOLOGICAL_NAME:
                cosh_id = ep.get("cosh_id")
                if cosh_id:
                    crop_ids.add(cosh_id)
    return crop_ids


async def list_crops(db: AsyncSession) -> list[dict]:
    """Public-facing list of crops, sorted alphabetically by English
    name. Used by both SA-portal CM browse and CA-portal "Add Crop"
    picker. Returns `[{cosh_id, name_en, status}]`.

    Inactive biological_name rows are filtered out — a curator may
    deactivate a name without removing the Connect row, and we don't
    want a CA picking a deactivated entry.
    """
    crop_ids = await _crop_classified_biological_name_ids(db)
    if not crop_ids:
        return []

    rows = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == COSH_BIOLOGICAL_NAMES_CORE,
            CoshCoreItem.cosh_id.in_(crop_ids),
            CoshCoreItem.status == "active",
        )
    )).scalars().all()

    out = []
    for row in rows:
        translations = row.translations or {}
        name_en = translations.get("en") or row.cosh_id
        out.append({
            "cosh_id": row.cosh_id,
            "name_en": name_en,
            "status": row.status,
        })
    out.sort(key=lambda r: r["name_en"].lower())
    return out


async def is_crop_in_cosh(db: AsyncSession, crop_cosh_id: str) -> bool:
    """Defensive check: returns True only if `crop_cosh_id` is a
    biological_name classified as Crop. Used by `crop_snapshot.fetch_
    snapshot` to refuse adding a Pest or Bio Control Agent UUID as a
    crop. Distinct from list_crops in that it doesn't hydrate the
    name rows — pure UUID membership."""
    crop_ids = await _crop_classified_biological_name_ids(db)
    return crop_cosh_id in crop_ids


# ── Area/Plant-wise typing (Round 3, 2026-05-09) ───────────────────────────

async def get_measure_for_biological_name(
    db: AsyncSession, cosh_id: str,
) -> str | None:
    """Walk the `crop_area_plant_wise` Connect to derive AREA_WISE /
    PLANT_WISE for a biological_name. Returns None if no active Connect
    row classifies this name (e.g. one of the 27 V1 crops still
    awaiting Cosh-side classification at first sync).

    Replaces the legacy `crop_measures` table read. Cosh is now the
    canonical source — RootsTalk does not mirror the value locally.
    """
    rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_CROP_AREA_PLANT_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()

    for row in rows:
        endpoints = row.endpoints or []
        # Connect row matches when one endpoint is our biological_name.
        names_in_row = [
            ep.get("cosh_id") for ep in endpoints
            if ep.get("role") == COSH_BIOLOGICAL_NAMES_CORE
        ]
        if cosh_id not in names_in_row:
            continue
        # Read the area_plant_wise endpoint and map UUID → token.
        for ep in endpoints:
            if ep.get("role") == COSH_AREA_PLANT_WISE_CORE:
                measure = COSH_UUID_TO_MEASURE.get(ep.get("cosh_id"))
                if measure:
                    return measure
    return None


async def list_crops_with_measure(db: AsyncSession) -> list[dict]:
    """Admin/CM-facing extended listing: crops + their measure + a
    flag highlighting which ones still need Cosh-side classification.
    Used by `GET /admin/crop-measures`.

    Combines both Connects in a single sweep (Crop classification +
    area_plant_wise) so the page can show "X of N crops still need
    Area-wise / Plant-wise typing on the Cosh side"."""
    crops = await list_crops(db)
    out = []
    for c in crops:
        measure = await get_measure_for_biological_name(db, c["cosh_id"])
        out.append({
            "crop_cosh_id": c["cosh_id"],
            "name_en": c["name_en"],
            "measure": measure,
        })
    return out
