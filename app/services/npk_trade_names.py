"""Resolve trade names for an NPK common-name selection.

Two paths depending on the L2:

  Chemical NPK    walk `tradename_commonname` Connect directly. The
                  three-group brand picker (Recommended / My Brands /
                  Other Brands) then re-uses BL-07's existing engine.

  Fertigation NPK walk `npk_fertigation_products` → resolve pos 1 to
                  common_name via `commonnames_l2` → resolve pos 2 to
                  trade_name via `tradename_manufacturer`. Only trade
                  names listed here are valid for the fertigation flow.

Returns (trade_name_cosh_id, trade_name_en, manufacturer_cosh_id_or_none).
The endpoint layer then groups by manufacturer for the three-group UI.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_COMMON_NAMES_CORE,
    COSH_COMMONNAMES_L2_CONNECT,
    COSH_INPUT_MANUFACTURERS_CORE,
    COSH_NPK_FERT_POS_COMMONNAMES_L2_ID,
    COSH_NPK_FERT_POS_TRADENAME_MFR_ID,
    COSH_NPK_FERTIGATION_PRODUCTS_CONNECT,
    COSH_TRADENAME_COMMONNAME_CONNECT,
    COSH_TRADENAME_MANUFACTURER_CONNECT,
    COSH_TRADE_NAMES_CORE,
)


async def trade_names_for_chemical_npk(
    db: AsyncSession, common_name_cosh_id: str,
) -> list[tuple[str, str, Optional[str]]]:
    """Walk tradename_commonname → trade_names, then tradename_manufacturer
    → manufacturers, returning the joined view."""
    tncn = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_TRADENAME_COMMONNAME_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()

    tn_ids: set[str] = set()
    for r in tncn:
        ep_map = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep_map.get(COSH_COMMON_NAMES_CORE) == common_name_cosh_id:
            tn = ep_map.get(COSH_TRADE_NAMES_CORE)
            if tn:
                tn_ids.add(tn)

    if not tn_ids:
        return []
    return await _materialize_trade_names(db, tn_ids)


async def trade_names_for_fertigation_npk(
    db: AsyncSession, common_name_cosh_id: str,
) -> list[tuple[str, str, Optional[str]]]:
    """Walk npk_fertigation_products → commonnames_l2 reverse → match
    common_name → take the corresponding tradename_manufacturer → resolve
    trade_name. Restricts the brand pool to fertigation-approved products
    only."""
    # Build commonnames_l2 connect_id → common_name map.
    cnl2_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_COMMONNAMES_L2_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    cnl2_to_cn: dict[str, str] = {}
    for r in cnl2_rows:
        for e in r.endpoints or []:
            if e.get("role") == COSH_COMMON_NAMES_CORE and e.get("cosh_id"):
                cnl2_to_cn[r.connect_id] = e["cosh_id"]
                break

    # Build tradename_manufacturer connect_id → (trade_name, manufacturer) map.
    tnm_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_TRADENAME_MANUFACTURER_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    tnm_to_pair: dict[str, tuple[str, Optional[str]]] = {}
    for r in tnm_rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        tn = ep.get(COSH_TRADE_NAMES_CORE)
        mfr = ep.get(COSH_INPUT_MANUFACTURERS_CORE)
        if tn:
            tnm_to_pair[r.connect_id] = (tn, mfr)

    fert_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_NPK_FERTIGATION_PRODUCTS_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()

    tn_ids: set[str] = set()
    mfr_by_tn: dict[str, Optional[str]] = {}
    for r in fert_rows:
        # Identify by POSITION (role names at pos 1/2 are auto-generated).
        cnl2_id = tnm_id = None
        for e in r.endpoints or []:
            pos = e.get("position")
            if pos == COSH_NPK_FERT_POS_COMMONNAMES_L2_ID:
                cnl2_id = e.get("cosh_id")
            elif pos == COSH_NPK_FERT_POS_TRADENAME_MFR_ID:
                tnm_id = e.get("cosh_id")
        if not cnl2_id or not tnm_id:
            continue
        if cnl2_to_cn.get(cnl2_id) != common_name_cosh_id:
            continue
        pair = tnm_to_pair.get(tnm_id)
        if not pair:
            continue
        tn, mfr = pair
        tn_ids.add(tn)
        # Keep the first manufacturer we see — a trade name shouldn't
        # appear under two manufacturers in a well-formed Cosh.
        mfr_by_tn.setdefault(tn, mfr)

    if not tn_ids:
        return []
    rows = await _materialize_trade_names(db, tn_ids)
    # Override the manufacturer from our explicit map (the materializer
    # falls back to tradename_manufacturer too but the fertigation walk
    # is the authoritative source here).
    return [(tn_id, name, mfr_by_tn.get(tn_id, mfr)) for tn_id, name, mfr in rows]


async def _materialize_trade_names(
    db: AsyncSession, tn_ids: set[str],
) -> list[tuple[str, str, Optional[str]]]:
    """Resolve trade-name cosh_ids → (cosh_id, english_name, manufacturer_cosh_id)."""
    cores = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == COSH_TRADE_NAMES_CORE,
            CoshCoreItem.cosh_id.in_(tn_ids),
            CoshCoreItem.status == "active",
        )
    )).scalars().all()

    # Resolve manufacturer per tn via tradename_manufacturer Connect.
    tnm_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_TRADENAME_MANUFACTURER_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    tn_to_mfr: dict[str, Optional[str]] = {}
    for r in tnm_rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        tn = ep.get(COSH_TRADE_NAMES_CORE)
        if tn in tn_ids:
            tn_to_mfr[tn] = ep.get(COSH_INPUT_MANUFACTURERS_CORE)

    out: list[tuple[str, str, Optional[str]]] = []
    for c in cores:
        name = (c.translations or {}).get("en") or c.cosh_id
        out.append((c.cosh_id, name, tn_to_mfr.get(c.cosh_id)))
    out.sort(key=lambda r: r[1].lower())  # alphabetical per spec §3.1
    return out
