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
    COSH_BIOLOGICAL_NAMES_CORE,
    COSH_NAME_ROLE_CONNECT,
    COSH_ROLE_CROP_UUID,
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
