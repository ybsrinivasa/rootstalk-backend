"""Materialised brand lookup cache.

Why this exists (fix 2026-06-01): the previous BL-07 brand resolver
searched `cosh_core_items` for `core_type='brand'`, but Cosh stores
brands as `trade_names` Core rows linked via two Connects:

  - tradename_commonname  : (trade_name × common_name)
  - tradename_manufacturer: (trade_name × input_manufacturer)
  - tradename_formulation : (trade_name × formulation)

13k+ trade-name rows × 800+ manufacturers. Walking the Connect rows
on every brand-picker request is too slow, so we materialise the
join into `brand_lookup_cache` and refresh on demand.

Refresh model mirrors `dealer_manufacturer_catalog`: SA endpoint or
lazy bootstrap on first read for an unknown common_name.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models import BrandLookupCache
from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_COMMON_NAMES_CORE,
    COSH_FORMULATIONS_CORE,
    COSH_INPUT_MANUFACTURERS_CORE,
    COSH_TRADENAME_COMMONNAME_CONNECT,
    COSH_TRADENAME_FORMULATION_CONNECT,
    COSH_TRADENAME_MANUFACTURER_CONNECT,
    COSH_TRADENAMES_UNITS_CONNECT,
    COSH_TRADE_NAMES_CORE,
    COSH_UNITS_DATA_CORE,
)


async def rebuild_brand_cache(db: AsyncSession) -> int:
    """Truncate-and-reload. Returns the number of rows written.

    Walks three Connects once each, resolves names from Cores in two
    bulk queries, then builds the cross-product. Ordering: stream all
    needed cosh_ids into a single Core query so we never N+1.
    """
    # tradename → common_name (may be many — Captan trade names appear in
    # multiple tradename_commonname rows, one per CN they map to).
    tncn_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_TRADENAME_COMMONNAME_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()

    # tradename → manufacturer (one per trade_name; we keep the first
    # if a TN appears twice for safety).
    tnm_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_TRADENAME_MANUFACTURER_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    tn_to_mfr: dict[str, str] = {}
    for r in tnm_rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        tn = ep.get(COSH_TRADE_NAMES_CORE)
        mfr = ep.get(COSH_INPUT_MANUFACTURERS_CORE)
        if tn and mfr:
            tn_to_mfr.setdefault(tn, mfr)

    # tradename → formulation (optional — many trade names lack this).
    tnf_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_TRADENAME_FORMULATION_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    tn_to_formulation: dict[str, str] = {}
    for r in tnf_rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        tn = ep.get(COSH_TRADE_NAMES_CORE)
        fmt = ep.get(COSH_FORMULATIONS_CORE)
        if tn and fmt:
            tn_to_formulation.setdefault(tn, fmt)

    # tradename → [unit cosh_ids]. One or more units per trade name
    # (e.g. Captaf sold in both 100 g and 1 kg packs).
    tnu_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_TRADENAMES_UNITS_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    tn_to_unit_ids: dict[str, list[str]] = {}
    for r in tnu_rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        tn = ep.get(COSH_TRADE_NAMES_CORE)
        unit = ep.get(COSH_UNITS_DATA_CORE)
        if tn and unit:
            tn_to_unit_ids.setdefault(tn, []).append(unit)

    # Bulk-resolve core names. One IN-query covers all four core types
    # we need translations for.
    needed_cosh_ids: set[str] = set()
    for r in tncn_rows:
        for ep in r.endpoints or []:
            cid = ep.get("cosh_id")
            if cid:
                needed_cosh_ids.add(cid)
    needed_cosh_ids.update(tn_to_mfr.values())
    needed_cosh_ids.update(tn_to_formulation.values())
    for unit_list in tn_to_unit_ids.values():
        needed_cosh_ids.update(unit_list)

    cores = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id.in_(needed_cosh_ids),
            CoshCoreItem.status == "active",
        )
    )).scalars().all() if needed_cosh_ids else []
    en_name = {
        c.cosh_id: ((c.translations or {}).get("en") or c.cosh_id)
        for c in cores
    }
    valid_core_ids = {c.cosh_id for c in cores}

    # Write phase — truncate then bulk-insert. SQLAlchemy's bulk insert
    # tops out fine for ~tens of thousands of rows; we're under that.
    await db.execute(BrandLookupCache.__table__.delete())
    now = datetime.now(timezone.utc)
    written = 0
    seen: set[tuple[str, str]] = set()  # PK dedup guard
    for r in tncn_rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        tn = ep.get(COSH_TRADE_NAMES_CORE)
        cn = ep.get(COSH_COMMON_NAMES_CORE)
        if not (tn and cn):
            continue
        # Skip rows whose Cores aren't active — keeps the cache
        # consistent with the rest of the active Cosh dataset.
        if tn not in valid_core_ids or cn not in valid_core_ids:
            continue
        key = (cn, tn)
        if key in seen:
            continue
        seen.add(key)
        mfr_id = tn_to_mfr.get(tn)
        fmt_id = tn_to_formulation.get(tn)
        # Build the unit list once; the cache row mirrors what the
        # /brand-options endpoint surfaces straight to the PWA dropdown.
        units: list[dict] = []
        seen_unit_ids: set[str] = set()
        for unit_id in tn_to_unit_ids.get(tn, []):
            if unit_id in seen_unit_ids:
                continue
            seen_unit_ids.add(unit_id)
            units.append({
                "cosh_id": unit_id,
                "name": en_name.get(unit_id, unit_id),
            })
        # Alphabetical so the dropdown order is stable.
        units.sort(key=lambda u: u["name"].lower())
        db.add(BrandLookupCache(
            common_name_cosh_id=cn,
            trade_name_cosh_id=tn,
            trade_name=en_name.get(tn, tn),
            manufacturer_cosh_id=mfr_id,
            manufacturer_name=en_name.get(mfr_id) if mfr_id else None,
            formulation_cosh_id=fmt_id,
            formulation_name=en_name.get(fmt_id) if fmt_id else None,
            units=units,
            refreshed_at=now,
        ))
        written += 1
    await db.commit()
    return written


async def get_brands_for_common_name(
    db: AsyncSession, common_name_cosh_id: str,
) -> list[BrandLookupCache]:
    """Returns cached brand rows for one common name. Lazy bootstrap
    on empty cache so the first hit after a deploy still works."""
    rows = (await db.execute(
        select(BrandLookupCache).where(
            BrandLookupCache.common_name_cosh_id == common_name_cosh_id,
        ).order_by(BrandLookupCache.trade_name)
    )).scalars().all()
    if rows:
        return list(rows)

    # No rows for this CN — could be a Cosh gap OR an empty cache.
    # Check whether the cache has ANY rows; if so, the CN is genuinely
    # uncovered. If not, the cache hasn't been built yet — bootstrap.
    any_row = (await db.execute(
        select(BrandLookupCache).limit(1)
    )).scalar_one_or_none()
    if any_row is not None:
        return []  # genuine miss, no point rebuilding

    await rebuild_brand_cache(db)
    rows = (await db.execute(
        select(BrandLookupCache).where(
            BrandLookupCache.common_name_cosh_id == common_name_cosh_id,
        ).order_by(BrandLookupCache.trade_name)
    )).scalars().all()
    return list(rows)
