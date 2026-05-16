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


# ── Per-L2 data-completeness filter (Batch 39D, 2026-05-15) ────────────────
#
# Per user 2026-05-15: an SE shouldn't be able to pick a Common Name / Trade
# Name / Manufacturer / Formulation / a.i. on an L2 unless at least one
# Trade Name under that L2 has a full set of Cosh-side connections. The
# concrete rule is L2-specific: e.g. CHEMICAL_PESTICIDES requires MFR + F +
# a.i.; MICROBIAL_PESTICIDES only requires MFR.
#
# Common Name is the anchor — a CN surfaces in `list_common_names_for_l2`
# only if at least one of its Trade Names satisfies every required Cosh
# connect. The same filter cascades to the Trade Name, Manufacturer,
# Formulation, and a.i. dropdowns under that L2, so the SE never lands on
# half-populated rows.
#
# `tradename_commonname` is implicit in every entry — that's how we find
# Trade Names for a Common Name to begin with. The spec below lists the
# *additional* Cosh connects each TN must populate.
#
# CHEMICAL_FERTILIZERS_NPK_DOSAGES and FERTIGATION_NPK_DOSAGES don't use
# the Trade Name flow at all (handled via L2_TYPES_WITHOUT_TRADE_NAMES);
# they're absent from this dict on purpose.

L2_COMPLETENESS_REQUIREMENTS: dict[str, frozenset[str]] = {
    "CHEMICAL_PESTICIDES":                      frozenset({"manufacturer", "formulation", "ai"}),
    "CHEMICAL_HERBICIDES":                      frozenset({"manufacturer", "formulation", "ai"}),
    "MICROBIAL_PESTICIDES":                     frozenset({"manufacturer"}),
    "BOTANICAL_PESTICIDES":                     frozenset({"manufacturer"}),
    "INSECT_BIOCONTROL_AGENTS":                 frozenset({"manufacturer"}),
    "INSECT_TRAPS":                             frozenset({"manufacturer"}),
    "OTHER_PESTICIDES":                         frozenset({"manufacturer"}),
    "ADJUVANTS":                                frozenset({"manufacturer"}),
    "MANURES":                                  frozenset({"manufacturer"}),
    "CHEMICAL_FERTILIZER_PRODUCTS":             frozenset({"manufacturer", "formulation"}),
    "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS": frozenset({"manufacturer", "formulation"}),
    "BIOFERTILIZERS":                           frozenset({"manufacturer"}),
    "PGR_TONICS":                               frozenset({"manufacturer"}),
    "SOIL_AMENDMENTS":                          frozenset({"manufacturer"}),
}

_REQUIREMENT_TO_CONNECT: dict[str, str] = {
    "manufacturer": COSH_TRADENAME_MANUFACTURER_CONNECT,
    "formulation":  COSH_TRADENAME_FORMULATION_CONNECT,
    "ai":           COSH_TRADENAME_AI_CONNECT,
}


async def _trade_names_in_connect(
    db: AsyncSession, connect_type: str,
) -> set[str]:
    """All Trade Name cosh_ids appearing in any active row of the given
    `tradename_X` Connect. Used by `_complete_trade_names_for_l2`."""
    rows = await _walk_connect(db, connect_type=connect_type)
    out: set[str] = set()
    for r in rows:
        for ep in (r.endpoints or []):
            if ep.get("role") == COSH_TRADE_NAMES_CORE:
                tn = ep.get("cosh_id")
                if tn:
                    out.add(tn)
    return out


async def list_incomplete_cosh_data_for_l2(
    db: AsyncSession, l2_type: str,
) -> dict:
    """Per-CN / per-TN completeness report — Batch 39D-report.

    Mirrors the completeness logic but emits the data instead of
    filtering. Per Common Name under the L2, lists every Trade Name
    and which of the L2's required Cosh connects are missing on it.
    The SA uses this to spot Cosh-side gaps and prioritise fixes.

    Response shape::

      {
        "l2_type": "...",
        "applicable": True | False,    # False = L2 has no spec
        "required":   ["manufacturer", "formulation", "ai"],
        "common_names": [
          {
            "cosh_id": "...",
            "name":    "Imidacloprid",
            "trade_names": [
              {"cosh_id": "...", "name": "Confidor", "missing": []},
              {"cosh_id": "...", "name": "Brand X",  "missing": ["formulation","ai"]}
            ],
            "has_complete_tn": True,    # ≥ 1 TN has every required connect
            "no_trade_names":  False    # CN has no TN at all (orphan CN)
          },
          ...
        ]
      }
    """
    req = L2_COMPLETENESS_REQUIREMENTS.get(l2_type)
    if not req:
        return {
            "l2_type": l2_type,
            "applicable": False,
            "required": [],
            "common_names": [],
        }
    l2_uuid = _l2_uuid(l2_type)
    if l2_uuid is None:
        return {
            "l2_type": l2_type,
            "applicable": True,
            "required": sorted(req),
            "common_names": [],
        }

    # CNs under this L2 (active only — names resolved below).
    cn_l2_rows = await _walk_connect(db, connect_type=COSH_COMMONNAMES_L2_CONNECT)
    cn_ids: set[str] = set()
    for r in cn_l2_rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get(COSH_L2_DATA_CORE) != l2_uuid:
            continue
        cn = ep.get(COSH_COMMON_NAMES_CORE)
        if cn:
            cn_ids.add(cn)

    # Resolve active CN names.
    cn_names: dict[str, str] = {}
    if cn_ids:
        cn_cores = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.core_type == COSH_COMMON_NAMES_CORE,
                CoshCoreItem.cosh_id.in_(cn_ids),
                CoshCoreItem.status == "active",
            )
        )).scalars().all()
        for c in cn_cores:
            cn_names[c.cosh_id] = _translation_en(c, c.cosh_id)

    # Per-required TN sets (all TN cosh_ids that appear in each connect).
    tns_in_connect: dict[str, set[str]] = {}
    for r in req:
        tns_in_connect[r] = await _trade_names_in_connect(
            db, _REQUIREMENT_TO_CONNECT[r],
        )

    # CN → TN mapping via tradename_commonname.
    cn_to_tns: dict[str, set[str]] = {}
    all_tn_ids: set[str] = set()
    tncn_rows = await _walk_connect(
        db, connect_type=COSH_TRADENAME_COMMONNAME_CONNECT,
    )
    for r in tncn_rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        tn = ep.get(COSH_TRADE_NAMES_CORE)
        cn = ep.get(COSH_COMMON_NAMES_CORE)
        if tn and cn:
            cn_to_tns.setdefault(cn, set()).add(tn)
            all_tn_ids.add(tn)

    # Resolve active TN names.
    tn_names: dict[str, str] = {}
    if all_tn_ids:
        tn_cores = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.core_type == COSH_TRADE_NAMES_CORE,
                CoshCoreItem.cosh_id.in_(all_tn_ids),
                CoshCoreItem.status == "active",
            )
        )).scalars().all()
        for c in tn_cores:
            tn_names[c.cosh_id] = _translation_en(c, c.cosh_id)

    common_names: list[dict] = []
    for cn_id in cn_ids:
        if cn_id not in cn_names:
            continue
        tns = cn_to_tns.get(cn_id, set())
        tn_details: list[dict] = []
        has_complete = False
        for tn_id in tns:
            if tn_id not in tn_names:
                continue
            missing = [r for r in req if tn_id not in tns_in_connect[r]]
            tn_details.append({
                "cosh_id": tn_id,
                "name": tn_names[tn_id],
                "missing": sorted(missing),
            })
            if not missing:
                has_complete = True
        tn_details.sort(key=lambda x: x["name"].casefold())
        common_names.append({
            "cosh_id": cn_id,
            "name": cn_names[cn_id],
            "trade_names": tn_details,
            "has_complete_tn": has_complete,
            "no_trade_names": len(tn_details) == 0,
        })
    common_names.sort(key=lambda x: x["name"].casefold())
    return {
        "l2_type": l2_type,
        "applicable": True,
        "required": sorted(req),
        "common_names": common_names,
    }


async def _complete_trade_names_for_l2(
    db: AsyncSession, l2_type: Optional[str],
) -> Optional[set[str]]:
    """Return the set of Trade Name cosh_ids that satisfy every Cosh
    connect required by this L2's completeness spec.

    Return value semantics:
      - None  → no filter applies (L2 not in the spec, or l2_type
                wasn't supplied). Caller treats this as "every TN is
                allowed" — preserves pre-39D behaviour.
      - set() → spec applies but no TN passes (Cosh data is fully
                broken for this L2). Caller returns an empty dropdown.
      - {...} → the eligible TN set; downstream callers intersect
                against this.

    The intersection is computed once per request — if perf becomes a
    problem at scale (the four connects are walked sequentially), add
    a TTL cache here keyed on l2_type. Today the connects are small
    enough that a per-request walk is well under 100 ms.
    """
    if not l2_type:
        return None
    req = L2_COMPLETENESS_REQUIREMENTS.get(l2_type)
    if not req:
        return None
    result: Optional[set[str]] = None
    for r in req:
        connect = _REQUIREMENT_TO_CONNECT[r]
        tn_set = await _trade_names_in_connect(db, connect)
        result = tn_set if result is None else (result & tn_set)
    return result if result is not None else set()


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
    # Batch 39D completeness filter — keep only CNs that link to at
    # least one TN satisfying every required Cosh connect for this L2.
    complete_tns = await _complete_trade_names_for_l2(db, l2_type)
    if complete_tns is not None:
        if not complete_tns:
            return []
        cn_with_complete_tn: set[str] = set()
        cn_tn_rows = await _walk_connect(
            db, connect_type=COSH_TRADENAME_COMMONNAME_CONNECT,
        )
        for r in cn_tn_rows:
            ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
            tn = ep.get(COSH_TRADE_NAMES_CORE)
            cn = ep.get(COSH_COMMON_NAMES_CORE)
            if cn and tn and tn in complete_tns:
                cn_with_complete_tn.add(cn)
        common_name_ids &= cn_with_complete_tn
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
    l2_type: Optional[str] = None,
) -> list[dict]:
    """Trade names available for the given common name. When
    `manufacturer_cosh_id` is supplied, narrows to trade names made
    by that manufacturer (intersection of CN's trade names and the
    manufacturer's brands). When omitted, returns the full set of
    CN's trade names — same as Batch 19 behaviour.

    Used by the bidirectional MFR ↔ TN filter on the Add Practice
    modal (Batch 24): expert can land on a trade name either by
    drilling down from a manufacturer they remember, or by picking
    the brand directly without ever touching the manufacturer.

    `l2_type` opts into the Batch 39D completeness filter — only TNs
    that satisfy every Cosh connect required for this L2 surface."""
    tn_ids = await _trade_names_for_common_name(db, common_name_cosh_id)
    complete_tns = await _complete_trade_names_for_l2(db, l2_type)
    if complete_tns is not None:
        tn_ids &= complete_tns
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
    l2_type: Optional[str] = None,
) -> list[dict]:
    """Manufacturers in scope for the given common name. When
    `trade_name_cosh_id` is supplied, narrows to the single
    manufacturer that makes that trade name (and only if that brand
    is itself in the CN's set — defends against caller mismatches).
    When omitted, returns the full CN-scope set — same as Batch 19.

    Walk: common_name → trade names → manufacturers (dedup); then
    optionally intersect with the {manufacturer of trade_name}.

    `l2_type` opts into the Batch 39D completeness filter — only TNs
    that satisfy every Cosh connect required for this L2 contribute
    a manufacturer."""
    tn_ids = await _trade_names_for_common_name(db, common_name_cosh_id)
    complete_tns = await _complete_trade_names_for_l2(db, l2_type)
    if complete_tns is not None:
        tn_ids &= complete_tns
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
    l2_type: Optional[str] = None,
) -> Optional[set[str]]:
    """Resolve the trade-name set to filter by, based on whether SE
    has picked Common Name only, or Common Name + Trade Name.

    Returns:
      None  → no filter could be built (no inputs given) → empty result
      set() → empty filter (e.g. common_name has no trade names) → empty result
      {...} → trade_name UUIDs to keep

    `l2_type` opts into the Batch 39D completeness filter. When set,
    the CN-only branch intersects with the L2's complete-TN set; the
    TN-specific branch trusts the caller (the TN was surfaced by an
    earlier dropdown that already applied the filter)."""
    if trade_name_cosh_id:
        return {trade_name_cosh_id}
    if common_name_cosh_id:
        tn_ids = await _trade_names_for_common_name(db, common_name_cosh_id)
        complete_tns = await _complete_trade_names_for_l2(db, l2_type)
        if complete_tns is not None:
            tn_ids &= complete_tns
        return tn_ids
    return None


async def list_formulations(
    db: AsyncSession,
    common_name_cosh_id: Optional[str] = None,
    trade_name_cosh_id: Optional[str] = None,
    l2_type: Optional[str] = None,
) -> list[dict]:
    """Formulations filtered by SE's selection: when only Common Name
    is set, span all trade names sharing that common name; when Trade
    Name is set, narrow to just that one. `l2_type` opts into the
    Batch 39D completeness filter."""
    tn_filter = await _resolve_trade_name_filter(
        db, common_name_cosh_id, trade_name_cosh_id, l2_type,
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
    l2_type: Optional[str] = None,
) -> list[dict]:
    """a.i. (%) options. Same filter logic as formulations. `l2_type`
    opts into the Batch 39D completeness filter."""
    tn_filter = await _resolve_trade_name_filter(
        db, common_name_cosh_id, trade_name_cosh_id, l2_type,
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


# ── Non-input Cores (2026-05-16) ──────────────────────────────────────
# Three lookups for Non-input L0 element forms:
#
#   PLANTING_MATERIAL_QUANTITY.PLANTING_MATERIAL  → planting_material  (flat)
#   ITKS.ITK_NAME                                 → itk_data           (flat)
#   HARVESTING_MANUAL.MATURITY_INDEX              → maturity_index     (crop-filtered)
#
# Planting Material and ITKs are flat — every active row surfaces.
# Maturity Index narrows to the rows linked to the package's crop via
# the `maturity_index_crops` Connect (maturity_index × biological_names).

async def _list_all_of_core_type(
    db: AsyncSession, *, core_type: str,
) -> list[dict]:
    """All active CoshCoreItem rows of the given core_type, sorted by
    English translation. Used by the flat Non-input lookups."""
    cores = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == core_type,
            CoshCoreItem.status == "active",
        )
    )).scalars().all()
    items = [
        {"cosh_id": c.cosh_id, "name": _translation_en(c, c.cosh_id)}
        for c in cores
    ]
    return sorted(items, key=lambda x: x["name"].casefold())


async def list_planting_materials(db: AsyncSession) -> list[dict]:
    return await _list_all_of_core_type(db, core_type="planting_material")


async def list_itks(db: AsyncSession) -> list[dict]:
    return await _list_all_of_core_type(db, core_type="itk_data")


async def list_maturity_indices_for_crop(
    db: AsyncSession, *, crop_cosh_id: str,
) -> list[dict]:
    """Maturity indices linked to the given biological_names cosh_id via
    the `maturity_index_crops` Connect. Crops outside the Connect's
    coverage return an empty list — the SE then either picks a different
    crop or asks the data team to extend the Cosh-side mapping."""
    from app.services.cosh_constants import COSH_MATURITY_INDEX_CROPS_CONNECT
    rows = await _walk_connect(
        db, connect_type=COSH_MATURITY_INDEX_CROPS_CONNECT,
    )
    mi_ids: set[str] = set()
    for r in rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get("biological_names") != crop_cosh_id:
            continue
        mi = ep.get("maturity_index")
        if mi:
            mi_ids.add(mi)
    return await _resolve_names(
        db, core_type="maturity_index", cosh_ids=mi_ids,
    )
