"""
Cosh Cascade Service

Named lookup functions that walk the typed Cosh tables to produce the
filtered option lists referenced by `cosh_cascade:<name>` sources in
the L2 element rule book (see `l2_element_rules.py`).

This module is the single boundary between the L2 element validator
and the underlying Cosh storage. Reads from `cosh_core_items`
exclusively (Connects live in `cosh_connect_rows` but no cascade
declared in `l2_element_rules.py` walks Connects today).

Data shape this module assumes
------------------------------
  cosh_core_items (core_type='common_name')   : top-level CNI item.
  cosh_core_items (core_type='brand')         : parent_cosh_id = CNI;
                                                metadata_ may carry:
                                                  manufacturer_name        (str)
                                                  manufacturer_client_id   (str, optional)
                                                  formulation_cosh_id      (str, optional)
                                                  ai_concentration         (str, optional)
  cosh_core_items (core_type='formulation')   : referenced by brand.metadata_

Manufacturers do not currently have their own Cosh rows; they exist as
name strings on brand metadata. The cascade returns option `value`s
that the SE picks and the validator stores back into Element.cosh_ref.
For manufacturers this is the name string itself; for brands and
formulations it's the cosh_id.

Cascade names recognised here must match `COSH_CASCADE_LOOKUPS` in
`l2_element_rules.py`. Adding a new cascade requires updating both
files.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshCoreItem


# ── Public types ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CascadeOption:
    """One row in a cascade dropdown's option list.

    `value` is what the SE's choice is stored as in Element.cosh_ref —
    a cosh_id for entity-backed options (brand, formulation), a name
    string for free-text options (manufacturer).

    `label` is the display string shown in the dropdown.
    """
    value: str
    label: str


# ── Per-cascade walks ───────────────────────────────────────────────────────

async def manufacturers_for_common_name(
    db: AsyncSession, common_name_cosh_id: Optional[str],
) -> list[CascadeOption]:
    """Distinct manufacturer name strings across active brand rows
    whose parent is this CNI. Sorted case-insensitive by label."""
    if not common_name_cosh_id:
        return []
    result = await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == "brand",
            CoshCoreItem.parent_cosh_id == common_name_cosh_id,
            CoshCoreItem.status == "active",
        )
    )
    seen: dict[str, CascadeOption] = {}
    for row in result.scalars().all():
        meta = row.metadata_ or {}
        mn = meta.get("manufacturer_name")
        if not mn:
            continue
        key = mn.lower()
        if key not in seen:
            seen[key] = CascadeOption(value=mn, label=mn)
    return sorted(seen.values(), key=lambda o: o.label.lower())


async def brands_for_common_name_and_manufacturer(
    db: AsyncSession,
    common_name_cosh_id: Optional[str],
    manufacturer_name: Optional[str],
) -> list[CascadeOption]:
    """Brand options under a CNI. The `manufacturer_name` filter is
    OPTIONAL (Batch 24, 2026-05-14): when supplied, narrows to
    brands made by that manufacturer (case-insensitive match); when
    None, returns the full set of CN's brands. This mirrors the new
    Add-Practice contract where MFR and BRAND_NAME are independent
    optional peers. Sorted by display label."""
    if not common_name_cosh_id:
        return []
    target = manufacturer_name.lower() if manufacturer_name else None
    result = await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == "brand",
            CoshCoreItem.parent_cosh_id == common_name_cosh_id,
            CoshCoreItem.status == "active",
        )
    )
    options: list[CascadeOption] = []
    for row in result.scalars().all():
        meta = row.metadata_ or {}
        if target is not None and (meta.get("manufacturer_name") or "").lower() != target:
            continue
        label = (row.translations or {}).get("en") or row.cosh_id
        options.append(CascadeOption(value=row.cosh_id, label=label))
    return sorted(options, key=lambda o: o.label.lower())


async def formulation_for_brand(
    db: AsyncSession,
    brand_cosh_id: Optional[str],
    common_name_cosh_id: Optional[str] = None,
) -> list[CascadeOption]:
    """Formulations under (brand | common_name) — Batch 27, 2026-05-14.

    Routing:
      - brand provided: legacy `brand` Core lookup (single auto-determined
        formulation_cosh_id from the brand's metadata). Preserves the
        existing test-fixture path that seeds synthetic `brand` Cores.
      - brand=None, common_name provided: walks the real Cosh shape via
        `cosh_options_view.list_formulations` — spans every trade name
        sharing the common name. This is the path the production UX
        exercises now that F is selectable on CN alone.
      - both None: empty.
    """
    if brand_cosh_id:
        brand = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.cosh_id == brand_cosh_id,
                CoshCoreItem.core_type == "brand",
            )
        )).scalar_one_or_none()
        if not brand:
            return []
        fid = (brand.metadata_ or {}).get("formulation_cosh_id")
        if not fid:
            return []
        fmt = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.cosh_id == fid,
                CoshCoreItem.core_type == "formulation",
            )
        )).scalar_one_or_none()
        label = (fmt.translations or {}).get("en") if fmt and fmt.translations else None
        return [CascadeOption(value=fid, label=label or fid)]
    if common_name_cosh_id:
        from app.services.cosh_options_view import list_formulations
        items = await list_formulations(db, common_name_cosh_id=common_name_cosh_id)
        return [CascadeOption(value=i["cosh_id"], label=i["name"]) for i in items]
    return []


async def ai_concentration_for_brand(
    db: AsyncSession,
    brand_cosh_id: Optional[str],
    common_name_cosh_id: Optional[str] = None,
) -> list[CascadeOption]:
    """a.i. concentration values — same routing as
    `formulation_for_brand`. When brand provided, returns the brand's
    single auto-determined a.i. (from metadata). When only common_name,
    spans the CN's trade names via cosh_options_view."""
    if brand_cosh_id:
        brand = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.cosh_id == brand_cosh_id,
                CoshCoreItem.core_type == "brand",
            )
        )).scalar_one_or_none()
        if not brand:
            return []
        ai = (brand.metadata_ or {}).get("ai_concentration") \
            or (brand.metadata_ or {}).get("ai_concentration_cosh_id")
        if not ai:
            return []
        ai = str(ai)
        return [CascadeOption(value=ai, label=ai)]
    if common_name_cosh_id:
        from app.services.cosh_options_view import list_ai_concentrations
        items = await list_ai_concentrations(
            db, common_name_cosh_id=common_name_cosh_id,
        )
        return [CascadeOption(value=i["cosh_id"], label=i["name"]) for i in items]
    return []


# ── Generic Core dropdowns (for `cosh_core:<slug>` sources) ────────────────

async def list_core_options(
    db: AsyncSession, entity_type: str,
) -> list[CascadeOption]:
    """All active items of a Cosh Core type. Used by the validator to
    enforce that values for `cosh_core:<slug>` fields are real Cosh
    entities. Sorted by English label.

    The parameter is kept named `entity_type` for back-compat with the
    L2 element validator's COSH_CORE_SLUG_MAP — its values are core_type
    strings now, but the slug-to-core-type mapping is 1:1 today."""
    result = await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == entity_type,
            CoshCoreItem.status == "active",
        )
    )
    options: list[CascadeOption] = []
    for row in result.scalars().all():
        label = (row.translations or {}).get("en") or row.cosh_id
        options.append(CascadeOption(value=row.cosh_id, label=label))
    return sorted(options, key=lambda o: o.label.lower())


# ── Unified entry point ─────────────────────────────────────────────────────

CASCADE_INPUTS: dict[str, tuple[str, ...]] = {
    "manufacturers_for_common_name":           ("COMMON_NAME",),
    # Batch 24: MANUFACTURER is now an optional cross-filter, not a
    # required upstream. Listed as ("COMMON_NAME",) to match the
    # rule book; MANUFACTURER (when set) is consumed via inputs.get
    # at dispatch time.
    "brands_for_common_name_and_manufacturer": ("COMMON_NAME",),
    # Batch 27: same pattern — F and a.i. cascade_from=("COMMON_NAME",)
    # in the rule book; BRAND_NAME (when set) is an optional narrowing
    # filter consumed at dispatch time.
    "formulation_for_brand":                   ("COMMON_NAME",),
    "ai_concentration_for_brand":              ("COMMON_NAME",),
}


async def list_cascade_options(
    db: AsyncSession, cascade_name: str, inputs: dict[str, Optional[str]],
) -> list[CascadeOption]:
    """Validator-facing dispatch. Accepts the full upstream-field map
    and extracts the inputs each cascade needs. Returns [] when any
    required upstream field is missing — the caller decides whether
    that's an error (mandatory_if_set) or expected (cascade not yet
    activated)."""
    if cascade_name == "manufacturers_for_common_name":
        return await manufacturers_for_common_name(db, inputs.get("COMMON_NAME"))
    if cascade_name == "brands_for_common_name_and_manufacturer":
        return await brands_for_common_name_and_manufacturer(
            db, inputs.get("COMMON_NAME"), inputs.get("MANUFACTURER"),
        )
    if cascade_name == "formulation_for_brand":
        return await formulation_for_brand(
            db, inputs.get("BRAND_NAME"),
            common_name_cosh_id=inputs.get("COMMON_NAME"),
        )
    if cascade_name == "ai_concentration_for_brand":
        return await ai_concentration_for_brand(
            db, inputs.get("BRAND_NAME"),
            common_name_cosh_id=inputs.get("COMMON_NAME"),
        )
    raise ValueError(f"Unknown cascade: {cascade_name}")
