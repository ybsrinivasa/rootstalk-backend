"""Read-through lookups for the seven input-options Connects shipped
by Cosh on 2026-05-14. These drive the cascading dropdowns on the
Add Practice modal (Common Name → Trade Name / Manufacturer →
Formulation / a.i.) and the per-L2 element forms (Units, Application
Methods).

Why read-through (not mirrored like `package_parameters`):
  - Nothing on the RootsTalk side FKs to these Cosh entities. We
    store the picked `cosh_id` on `Practice.element_values` directly;
    the dropdown lookups don't need local primary keys.
  - The vocabularies are large (trade names alone will run into the
    thousands once Cosh fills them in) and pure read traffic — a
    mirror would just duplicate Cosh's tables for no gain.

The Connects:
  commonnames_l2          common_names_of_inputs × l2_data
  application_methods_l2  application_methods × l2_data
  l2_units_unittypes      l2_data × units_data × unit_types  (3-endpoint)
  tradename_commonname    trade_names × common_names_of_inputs
  tradename_manufacturer  trade_names × input_manufacturers
  tradename_formulation   trade_names × formulations
  tradename_ai            trade_names × a_i

Trade Names are NOT applicable for the two NPK-dosage L2s
(CHEMICAL_FERTILIZERS_NPK_DOSAGES, FERTIGATION_NPK_DOSAGES). Callers
gate on `l2_uses_trade_names(l2_type)` before showing those columns.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_AI_CORE,
    COSH_APPLICATION_METHODS_CORE,
    COSH_APPLICATION_METHODS_L2_CONNECT,
    COSH_COMMON_NAMES_CORE,
    COSH_COMMONNAMES_L2_CONNECT,
    COSH_FORMULATIONS_CORE,
    COSH_INPUT_MANUFACTURERS_CORE,
    COSH_L2_DATA_CORE,
    COSH_L2_UNITS_UNITTYPES_CONNECT,
    COSH_TRADE_NAMES_CORE,
    COSH_TRADENAME_AI_CONNECT,
    COSH_TRADENAME_COMMONNAME_CONNECT,
    COSH_TRADENAME_FORMULATION_CONNECT,
    COSH_TRADENAME_MANUFACTURER_CONNECT,
    COSH_UNIT_TYPES_CORE,
    COSH_UNITS_DATA_CORE,
    PYTHON_L2_TO_COSH_UUID,
    UNIT_TYPE_SLUG_TO_COSH_UUIDS,
)

L2_TYPES_WITHOUT_TRADE_NAMES = {
    "CHEMICAL_FERTILIZERS_NPK_DOSAGES",
    "FERTIGATION_NPK_DOSAGES",
}


def l2_uses_trade_names(l2_type: str) -> bool:
    return l2_type not in L2_TYPES_WITHOUT_TRADE_NAMES


def _translation_en(core: Optional[CoshCoreItem], fallback: str) -> str:
    if core is None:
        return fallback
    t = core.translations or {}
    return t.get("en") or t.get("English") or fallback


async def _resolve_names(
    db: AsyncSession, *, core_type: str, cosh_ids: set[str],
) -> list[dict]:
    """Look up active CoshCoreItem rows of the given type for the
    given UUIDs and return `[{cosh_id, name}, ...]` sorted by name.
    Inactive Core items are filtered out (Cosh-side lifecycle)."""
    if not cosh_ids:
        return []
    cores = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == core_type,
            CoshCoreItem.cosh_id.in_(cosh_ids),
            CoshCoreItem.status == "active",
        )
    )).scalars().all()
    items = [
        {"cosh_id": c.cosh_id, "name": _translation_en(c, c.cosh_id)}
        for c in cores
    ]
    return sorted(items, key=lambda x: x["name"].casefold())


async def _walk_connect(
    db: AsyncSession, *, connect_type: str,
) -> list[CoshConnectRow]:
    rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == connect_type,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    return list(rows)


def _l2_uuid(l2_type: str) -> Optional[str]:
    return PYTHON_L2_TO_COSH_UUID.get(l2_type)


# ── Per-L2 lookups ─────────────────────────────────────────────────────────

async def list_common_names_for_l2(
    db: AsyncSession, l2_type: str,
) -> list[dict]:
    l2_uuid = _l2_uuid(l2_type)
    if l2_uuid is None:
        return []
    rows = await _walk_connect(db, connect_type=COSH_COMMONNAMES_L2_CONNECT)
    common_name_ids: set[str] = set()
    for r in rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get(COSH_L2_DATA_CORE) != l2_uuid:
            continue
        cn = ep.get(COSH_COMMON_NAMES_CORE)
        if cn:
            common_name_ids.add(cn)
    return await _resolve_names(
        db, core_type=COSH_COMMON_NAMES_CORE, cosh_ids=common_name_ids,
    )


async def list_application_methods_for_l2(
    db: AsyncSession, l2_type: str,
) -> list[dict]:
    l2_uuid = _l2_uuid(l2_type)
    if l2_uuid is None:
        return []
    rows = await _walk_connect(
        db, connect_type=COSH_APPLICATION_METHODS_L2_CONNECT,
    )
    method_ids: set[str] = set()
    for r in rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get(COSH_L2_DATA_CORE) != l2_uuid:
            continue
        m = ep.get(COSH_APPLICATION_METHODS_CORE)
        if m:
            method_ids.add(m)
    return await _resolve_names(
        db, core_type=COSH_APPLICATION_METHODS_CORE, cosh_ids=method_ids,
    )


async def list_units_for_l2(
    db: AsyncSession, l2_type: str, unit_type_slug: str,
) -> list[dict]:
    """Three-endpoint Connect: keep rows where the L2 matches AND the
    unit_type is in the allowed set for the requested slug."""
    l2_uuid = _l2_uuid(l2_type)
    allowed_unit_types = set(
        UNIT_TYPE_SLUG_TO_COSH_UUIDS.get(unit_type_slug, [])
    )
    if l2_uuid is None or not allowed_unit_types:
        return []
    rows = await _walk_connect(
        db, connect_type=COSH_L2_UNITS_UNITTYPES_CONNECT,
    )
    unit_ids: set[str] = set()
    for r in rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get(COSH_L2_DATA_CORE) != l2_uuid:
            continue
        if ep.get(COSH_UNIT_TYPES_CORE) not in allowed_unit_types:
            continue
        u = ep.get(COSH_UNITS_DATA_CORE)
        if u:
            unit_ids.add(u)
    return await _resolve_names(
        db, core_type=COSH_UNITS_DATA_CORE, cosh_ids=unit_ids,
    )


# ── Common Name → Trade / Manufacturer / Formulation / a.i. ───────────────

async def _trade_names_for_common_name(
    db: AsyncSession, common_name_cosh_id: str,
) -> set[str]:
    """Internal: set of trade_name cosh_ids that share the given
    common_name. Underpins the three downstream cascades."""
    rows = await _walk_connect(
        db, connect_type=COSH_TRADENAME_COMMONNAME_CONNECT,
    )
    tn_ids: set[str] = set()
    for r in rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get(COSH_COMMON_NAMES_CORE) != common_name_cosh_id:
            continue
        tn = ep.get(COSH_TRADE_NAMES_CORE)
        if tn:
            tn_ids.add(tn)
    return tn_ids


async def _trade_names_for_manufacturer(
    db: AsyncSession, manufacturer_cosh_id: str,
) -> set[str]:
    """Internal: set of trade_name cosh_ids made by the given
    manufacturer."""
    rows = await _walk_connect(
        db, connect_type=COSH_TRADENAME_MANUFACTURER_CONNECT,
    )
    tn_ids: set[str] = set()
    for r in rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get(COSH_INPUT_MANUFACTURERS_CORE) != manufacturer_cosh_id:
            continue
        tn = ep.get(COSH_TRADE_NAMES_CORE)
        if tn:
            tn_ids.add(tn)
    return tn_ids


async def list_trade_names_for_common_name(
    db: AsyncSession,
    common_name_cosh_id: str,
    manufacturer_cosh_id: Optional[str] = None,
) -> list[dict]:
    """Trade names available for the given common name. When
    `manufacturer_cosh_id` is supplied, narrows to trade names made
    by that manufacturer (intersection of CN's trade names and the
    manufacturer's brands). When omitted, returns the full set of
    CN's trade names — same as Batch 19 behaviour.

    Used by the bidirectional MFR ↔ TN filter on the Add Practice
    modal (Batch 24): expert can land on a trade name either by
    drilling down from a manufacturer they remember, or by picking
    the brand directly without ever touching the manufacturer."""
    tn_ids = await _trade_names_for_common_name(db, common_name_cosh_id)
    if manufacturer_cosh_id:
        tn_ids &= await _trade_names_for_manufacturer(
            db, manufacturer_cosh_id,
        )
    return await _resolve_names(
        db, core_type=COSH_TRADE_NAMES_CORE, cosh_ids=tn_ids,
    )


async def list_manufacturers_for_common_name(
    db: AsyncSession,
    common_name_cosh_id: str,
    trade_name_cosh_id: Optional[str] = None,
) -> list[dict]:
    """Manufacturers in scope for the given common name. When
    `trade_name_cosh_id` is supplied, narrows to the single
    manufacturer that makes that trade name (and only if that brand
    is itself in the CN's set — defends against caller mismatches).
    When omitted, returns the full CN-scope set — same as Batch 19.

    Walk: common_name → trade names → manufacturers (dedup); then
    optionally intersect with the {manufacturer of trade_name}."""
    tn_ids = await _trade_names_for_common_name(db, common_name_cosh_id)
    if trade_name_cosh_id:
        # Defensive: only include the manufacturer if the trade_name
        # is actually one of CN's brands (caller might pass a stale
        # combo from a parallel form update).
        if trade_name_cosh_id not in tn_ids:
            return []
        tn_ids = {trade_name_cosh_id}
    if not tn_ids:
        return []
    rows = await _walk_connect(
        db, connect_type=COSH_TRADENAME_MANUFACTURER_CONNECT,
    )
    mfr_ids: set[str] = set()
    for r in rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get(COSH_TRADE_NAMES_CORE) not in tn_ids:
            continue
        m = ep.get(COSH_INPUT_MANUFACTURERS_CORE)
        if m:
            mfr_ids.add(m)
    return await _resolve_names(
        db, core_type=COSH_INPUT_MANUFACTURERS_CORE, cosh_ids=mfr_ids,
    )


async def _connect_other_end_for_trade_names(
    db: AsyncSession, *, connect_type: str, other_role: str,
    trade_name_filter: set[str],
) -> set[str]:
    """Generic: walk a `tradename_X` Connect, keep rows whose
    trade_name endpoint is in `trade_name_filter`, return the set of
    cosh_ids on `other_role`."""
    rows = await _walk_connect(db, connect_type=connect_type)
    out: set[str] = set()
    for r in rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get(COSH_TRADE_NAMES_CORE) not in trade_name_filter:
            continue
        v = ep.get(other_role)
        if v:
            out.add(v)
    return out


async def _resolve_trade_name_filter(
    db: AsyncSession,
    common_name_cosh_id: Optional[str],
    trade_name_cosh_id: Optional[str],
) -> Optional[set[str]]:
    """Resolve the trade-name set to filter by, based on whether SE
    has picked Common Name only, or Common Name + Trade Name.

    Returns:
      None  → no filter could be built (no inputs given) → empty result
      set() → empty filter (e.g. common_name has no trade names) → empty result
      {...} → trade_name UUIDs to keep
    """
    if trade_name_cosh_id:
        return {trade_name_cosh_id}
    if common_name_cosh_id:
        return await _trade_names_for_common_name(db, common_name_cosh_id)
    return None


async def list_formulations(
    db: AsyncSession,
    common_name_cosh_id: Optional[str] = None,
    trade_name_cosh_id: Optional[str] = None,
) -> list[dict]:
    """Formulations filtered by SE's selection: when only Common Name
    is set, span all trade names sharing that common name; when Trade
    Name is set, narrow to just that one."""
    tn_filter = await _resolve_trade_name_filter(
        db, common_name_cosh_id, trade_name_cosh_id,
    )
    if tn_filter is None or not tn_filter:
        return []
    form_ids = await _connect_other_end_for_trade_names(
        db, connect_type=COSH_TRADENAME_FORMULATION_CONNECT,
        other_role=COSH_FORMULATIONS_CORE, trade_name_filter=tn_filter,
    )
    return await _resolve_names(
        db, core_type=COSH_FORMULATIONS_CORE, cosh_ids=form_ids,
    )


async def list_ai_concentrations(
    db: AsyncSession,
    common_name_cosh_id: Optional[str] = None,
    trade_name_cosh_id: Optional[str] = None,
) -> list[dict]:
    """a.i. (%) options. Same filter logic as formulations."""
    tn_filter = await _resolve_trade_name_filter(
        db, common_name_cosh_id, trade_name_cosh_id,
    )
    if tn_filter is None or not tn_filter:
        return []
    ai_ids = await _connect_other_end_for_trade_names(
        db, connect_type=COSH_TRADENAME_AI_CONNECT,
        other_role=COSH_AI_CORE, trade_name_filter=tn_filter,
    )
    return await _resolve_names(
        db, core_type=COSH_AI_CORE, cosh_ids=ai_ids,
    )
