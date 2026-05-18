"""Read-through service over Cosh's `sp_pg_crops` Connect (shipped
2026-05-14, 1,633 rows). 3-endpoint shape:

  pos 1: biological_names   ← the SP
  pos 2: problem_groups     ← the PG
  pos 3: biological_names   ← the crop

Three lookup directions:

  • `list_crops_for_pg(pg)`            — crops applicable to a PG.
  • `list_pgs_for_crop(crop)`          — PGs applicable to a crop.
  • `list_sps_for_pg_crop(pg, crop)`   — SPs at the (PG, crop) pair.

Pure read-through. RootsTalk side does not mirror these — diagnosis /
authoring surfaces query through this service every time.

Translations: each item carries `name_en` (`translations.en`), with
fallback to the cosh_id for stability. Inactive Core items are
dropped from the response so they never surface as a pickable option.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_BIOLOGICAL_NAMES_CORE,
    COSH_PROBLEM_GROUPS_CORE,
    COSH_SP_PG_CROPS_CONNECT,
    SPPC_POS_CROP,
    SPPC_POS_PG,
    SPPC_POS_SP,
)


def _endpoint_at_position(row: CoshConnectRow, position: int) -> Optional[str]:
    for e in row.endpoints or []:
        if e.get("position") == position:
            return e.get("cosh_id")
    return None


async def _walk_active_rows(db: AsyncSession) -> list[CoshConnectRow]:
    rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_SP_PG_CROPS_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    return list(rows)


def _translation_en(core: Optional[CoshCoreItem], fallback: str) -> str:
    if core is None:
        return fallback
    t = core.translations or {}
    return t.get("en") or t.get("English") or fallback


async def _resolve_core_names(
    db: AsyncSession, *, core_type: str, cosh_ids: set[str],
) -> dict[str, str]:
    """Return {cosh_id: en_name} for *active* Core items of the
    requested core_type. Inactive items are dropped, so a stale
    SP / PG / crop never surfaces in dropdowns."""
    cosh_ids = {c for c in cosh_ids if c}
    if not cosh_ids:
        return {}
    cores = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == core_type,
            CoshCoreItem.cosh_id.in_(cosh_ids),
            CoshCoreItem.status == "active",
        )
    )).scalars().all()
    return {c.cosh_id: _translation_en(c, c.cosh_id) for c in cores}


def _collect_at_position(
    rows: list[CoshConnectRow], position: int,
) -> set[str]:
    out: set[str] = set()
    for r in rows:
        v = _endpoint_at_position(r, position)
        if v:
            out.add(v)
    return out


def _name_items(
    cosh_ids: set[str], name_by_id: dict[str, str],
) -> list[dict]:
    items = [
        {"cosh_id": c, "name_en": name_by_id[c]}
        for c in cosh_ids if c in name_by_id
    ]
    return sorted(items, key=lambda x: x["name_en"].casefold())


# ── Lookup directions ────────────────────────────────────────────────────


async def list_crops_for_pg(
    db: AsyncSession, *, pg_cosh_id: str,
) -> list[dict]:
    """Crops applicable to a Problem Group. Returns [{cosh_id, name_en}],
    sorted alphabetically. Empty list when the PG has no rows or is
    unknown."""
    rows = [
        r for r in await _walk_active_rows(db)
        if _endpoint_at_position(r, SPPC_POS_PG) == pg_cosh_id
    ]
    crop_ids = _collect_at_position(rows, SPPC_POS_CROP)
    names = await _resolve_core_names(
        db, core_type=COSH_BIOLOGICAL_NAMES_CORE, cosh_ids=crop_ids,
    )
    return _name_items(crop_ids, names)


async def list_pgs_for_crop(
    db: AsyncSession, *, crop_cosh_id: str,
) -> list[dict]:
    """Problem Groups applicable to a crop. Reverse direction of
    `list_crops_for_pg`."""
    rows = [
        r for r in await _walk_active_rows(db)
        if _endpoint_at_position(r, SPPC_POS_CROP) == crop_cosh_id
    ]
    pg_ids = _collect_at_position(rows, SPPC_POS_PG)
    names = await _resolve_core_names(
        db, core_type=COSH_PROBLEM_GROUPS_CORE, cosh_ids=pg_ids,
    )
    return _name_items(pg_ids, names)


async def list_sps_for_pg_crop(
    db: AsyncSession, *, pg_cosh_id: str, crop_cosh_id: str,
) -> list[dict]:
    """Specific Problems at the (PG, crop) intersection."""
    rows = [
        r for r in await _walk_active_rows(db)
        if _endpoint_at_position(r, SPPC_POS_PG) == pg_cosh_id
        and _endpoint_at_position(r, SPPC_POS_CROP) == crop_cosh_id
    ]
    sp_ids = _collect_at_position(rows, SPPC_POS_SP)
    names = await _resolve_core_names(
        db, core_type=COSH_BIOLOGICAL_NAMES_CORE, cosh_ids=sp_ids,
    )
    return _name_items(sp_ids, names)


async def list_sps_for_crop(
    db: AsyncSession, *, crop_cosh_id: str,
) -> list[dict]:
    """All Specific Problems applicable to a crop, across every PG.

    Used by the CA-SP authoring page (SE picks the crop, sees every
    SP that could be authored against it). 2026-05-18 — replaces the
    hardcoded `_SPECIFIC_PROBLEMS_V1` stopgap with the real
    Cosh-sourced list.
    """
    rows = [
        r for r in await _walk_active_rows(db)
        if _endpoint_at_position(r, SPPC_POS_CROP) == crop_cosh_id
    ]
    sp_ids = _collect_at_position(rows, SPPC_POS_SP)
    names = await _resolve_core_names(
        db, core_type=COSH_BIOLOGICAL_NAMES_CORE, cosh_ids=sp_ids,
    )
    return _name_items(sp_ids, names)
