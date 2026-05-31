"""
BL-07 Brand Selection
Determines whether an order item's practice has a locked brand or an unlocked
brand requiring dealer selection, and returns a grouped brand options list.

Locked brand: Practice has an element of element_type 'brand' with a cosh_ref
  that points to a specific brand in cosh_core_items. Dealer confirms and
  proceeds — no selection needed.

Unlocked brand: No locked brand element. Return three groups:
  Group 1 — brands whose manufacturer_cosh_id matches any of the dealer's active
             dealership manufacturer_client_id or manufacturer_name (preferred)
  Group 2 — all other active brands from cosh_core_items for this practice
  Group 3 — sentinel "Not in system / Report missing brand"

OR-relation auto-close: When a dealer marks one item in an OR group as
  AVAILABLE, other items in the same order with the same relation_id are set to
  NOT_NEEDED automatically.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


# ── Phase 3.3 helpers: source elements from snapshot when available ─────────

def _el_field(el, name):
    """Read an element attribute uniformly whether it's a SQLAlchemy row
    (master) or a dict (snapshot content)."""
    if el is None:
        return None
    if isinstance(el, dict):
        return el.get(name)
    return getattr(el, name, None)


def _practice_elements_from_snapshot(snapshot, practice_id: str):
    """If a snapshot is provided, return the frozen element list for this
    practice (list of dicts). Returns None when no snapshot is given OR
    the practice id is not present inside the snapshot's content (caller
    falls back to master).
    """
    if snapshot is None:
        return None
    content = getattr(snapshot, "content", None) or {}
    for p in (content.get("practices") or []):
        if p.get("id") == practice_id:
            return p.get("elements") or []
    return None


@dataclass
class BrandOption:
    cosh_id: str
    name: str
    manufacturer: Optional[str] = None
    preferred: bool = False


# ── Batch 25 — Formulation → Brand Unit Family ────────────────────────────
#
# Cosh's `formulation` core attaches via `brand.metadata_.formulation_cosh_id`.
# Each formulation maps to one of three unit families:
#
#   - "solid"    → kg / g          (granular powders, crystals, pellets, WG/WP/WSG)
#   - "liquid"   → L / ml          (SC, EC, SL, EW, suspensions, emulsions, solutions)
#   - "discrete" → numbers         (traps, lures, sachets, tablets)
#
# This replaces the per-brand manual unit maintenance the user flagged as
# painful — maintenance is now one small mapping per formulation class.

_LIQUID_KEYWORDS = (
    "liquid", "suspension", "emulsion", "solution",
    "soluble liquid", "flowable", "(sc)", "(ec)", "(sl)", "(ew)", "(soluble",
)
_DISCRETE_KEYWORDS = (
    "trap", "lure", "sachet", "tablet", "capsule",
)


def _classify_formulation_to_unit_family(formulation_name: Optional[str]) -> str:
    """`solid` | `liquid` | `discrete` based on the formulation core's
    English name. Defaults to `solid` when unknown (matches the dominant
    Cosh formulation set + the safer-misclassification choice for
    granular fertilisers, which are the largest brand cohort)."""
    if not formulation_name:
        return "solid"
    fmt = formulation_name.lower()
    for kw in _DISCRETE_KEYWORDS:
        if kw in fmt:
            return "discrete"
    for kw in _LIQUID_KEYWORDS:
        if kw in fmt:
            return "liquid"
    return "solid"


UNIT_OPTIONS_BY_FAMILY = {
    "solid":    ["kg", "g"],
    "liquid":   ["L", "ml"],
    "discrete": ["numbers"],
}


@dataclass
class BrandOptionsResult:
    is_locked: bool
    locked_brand_cosh_id: Optional[str] = None
    locked_brand_name: Optional[str] = None
    locked_brand_unit_family: Optional[str] = None
    # Batch 25 — three groups per the 2026-05-31 user spec.
    # Recommended Brands sits above My Brands, which sits above Other.
    group_recommended: list[BrandOption] = None
    group_my: list[BrandOption] = None
    group_other: list[BrandOption] = None
    # Map of brand cosh_id → unit family (solid / liquid / discrete).
    # Returned alongside the groups so the PWA can constrain the
    # Given-Volume unit dropdown to what physically makes sense for
    # the brand the dealer picks.
    brand_unit_family: dict[str, str] = None

    def __post_init__(self):
        if self.group_recommended is None:
            self.group_recommended = []
        if self.group_my is None:
            self.group_my = []
        if self.group_other is None:
            self.group_other = []
        if self.brand_unit_family is None:
            self.brand_unit_family = {}

    def to_dict(self) -> dict:
        if self.is_locked:
            return {
                "type": "LOCKED",
                "locked_brand_cosh_id": self.locked_brand_cosh_id,
                "locked_brand_name": self.locked_brand_name,
                "locked_brand_unit_family": self.locked_brand_unit_family,
                "groups": [],
                "brand_unit_family": self.brand_unit_family,
                "unit_options_by_family": UNIT_OPTIONS_BY_FAMILY,
            }
        # Hide empty groups per user spec (2026-05-31).
        groups = []
        if self.group_recommended:
            groups.append({
                "label": "Recommended Brands",
                "brands": [{"cosh_id": b.cosh_id, "name": b.name,
                            "manufacturer": b.manufacturer} for b in self.group_recommended],
            })
        if self.group_my:
            groups.append({
                "label": "My Brands",
                "brands": [{"cosh_id": b.cosh_id, "name": b.name,
                            "manufacturer": b.manufacturer} for b in self.group_my],
            })
        if self.group_other:
            groups.append({
                "label": "Other Brands",
                "brands": [{"cosh_id": b.cosh_id, "name": b.name,
                            "manufacturer": b.manufacturer} for b in self.group_other],
            })
        return {
            "type": "UNLOCKED",
            "locked_brand_cosh_id": None,
            "locked_brand_name": None,
            "locked_brand_unit_family": None,
            "groups": groups,
            "brand_unit_family": self.brand_unit_family,
            "unit_options_by_family": UNIT_OPTIONS_BY_FAMILY,
        }


async def get_brand_options(
    db,
    practice_id: str,
    dealer_user_id: str,
    snapshot=None,
) -> BrandOptionsResult:
    """
    Returns brand options for a given practice and dealer.
    Queries cosh_core_items for available brands and filters by
    dealer's active dealership relationships.

    Phase 3.3: when `snapshot` is provided (a LockedTimelineSnapshot row from
    the per-subscription versioning system), the practice's elements are
    sourced from the frozen snapshot content rather than the master Element
    table. This protects the dealer's view of brand-lock state from SE
    edits made AFTER order placement (Rule 5). Cosh lookups (brand name
    translations, alternative-brand list) stay master-sourced because
    cosh_core_items is a global reference, not per-subscription.
    """
    from sqlalchemy import select
    from app.modules.advisory.models import Practice, Element
    from app.modules.orders.models import DealerRelationship
    from app.modules.sync.models import CoshCoreItem

    # Batch 39I-a (2026-05-16) — Practice.is_brand_locked is now the
    # authoritative lock flag. We always load the Practice (even when a
    # snapshot is present) to read the flag; snapshot content is used
    # only for element values, which is what Rule 5 protects.
    practice = (await db.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one_or_none()
    if practice is None:
        return BrandOptionsResult(is_locked=False)

    elements = _practice_elements_from_snapshot(snapshot, practice_id)
    if elements is None:
        # No snapshot or practice not found inside snapshot — fall back to master.
        elements = (await db.execute(
            select(Element).where(Element.practice_id == practice_id)
        )).scalars().all()

    # Locked: the SE checked Lock Brand on this Practice. The locked
    # Trade Name comes from the BRAND_NAME element (legacy lowercase
    # 'brand' element_type also supported for any pre-39I-a rows that
    # may have been authored against the older schema).
    if practice.is_brand_locked:
        locked_el = next(
            (e for e in elements
             if _el_field(e, "element_type") in ("BRAND_NAME", "brand")
             and _el_field(e, "cosh_ref")),
            None,
        )
        if locked_el:
            locked_cosh_ref = _el_field(locked_el, "cosh_ref")
            brand_entry = (await db.execute(
                select(CoshCoreItem).where(
                    CoshCoreItem.cosh_id == locked_cosh_ref,
                )
            )).scalar_one_or_none()
            brand_name = None
            unit_family = None
            if brand_entry:
                if brand_entry.translations:
                    brand_name = brand_entry.translations.get("en") or locked_cosh_ref
                # Batch 25 — also resolve unit family for the locked brand
                # so the PWA's Given-Volume dropdown is constrained.
                fid = (brand_entry.metadata_ or {}).get("formulation_cosh_id")
                if fid:
                    fmt = (await db.execute(
                        select(CoshCoreItem).where(
                            CoshCoreItem.cosh_id == fid,
                            CoshCoreItem.core_type == "formulation",
                        )
                    )).scalar_one_or_none()
                    fmt_name = (fmt.translations or {}).get("en") if (fmt and fmt.translations) else None
                    unit_family = _classify_formulation_to_unit_family(fmt_name)
                else:
                    unit_family = _classify_formulation_to_unit_family(None)
            return BrandOptionsResult(
                is_locked=True,
                locked_brand_cosh_id=locked_cosh_ref,
                locked_brand_name=brand_name or locked_cosh_ref,
                locked_brand_unit_family=unit_family,
                brand_unit_family={locked_cosh_ref: unit_family} if unit_family else {},
            )
        # is_brand_locked was True but BRAND_NAME was wiped from the
        # snapshot — defensive: fall through to the unlocked branch
        # rather than emit half-locked state. The Practice should be
        # repaired by re-saving via the SA portal (the validation
        # catches the same gap on write).

    # Unlocked brand — find available brands from cosh_core_items.
    common_name_el = next(
        (e for e in elements
         if _el_field(e, "element_type") in ("COMMON_NAME", "common_name")
         and _el_field(e, "cosh_ref")),
        None,
    )
    common_name_cosh_id = _el_field(common_name_el, "cosh_ref") if common_name_el else None

    # Batch 25 — Recommended brand detection. When the SE picks a
    # specific BRAND_NAME with cosh_ref on an UNLOCKED practice,
    # that's a recommendation (not a hard lock). Same element shape
    # as a locked brand; the lock flag is the discriminator.
    recommended_el = next(
        (e for e in elements
         if _el_field(e, "element_type") in ("BRAND_NAME", "brand")
         and _el_field(e, "cosh_ref")),
        None,
    )
    recommended_cosh_id = _el_field(recommended_el, "cosh_ref") if recommended_el else None

    if common_name_cosh_id:
        brands_result = await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.core_type == "brand",
                CoshCoreItem.parent_cosh_id == common_name_cosh_id,
                CoshCoreItem.status == "active",
            ).order_by(CoshCoreItem.cosh_id)
        )
        all_brands = brands_result.scalars().all()
    else:
        all_brands = []

    # Dealer's active dealerships
    rels_result = await db.execute(
        select(DealerRelationship).where(
            DealerRelationship.dealer_user_id == dealer_user_id,
            DealerRelationship.status == "ACTIVE",
        )
    )
    dealer_rels = rels_result.scalars().all()
    preferred_client_ids = {r.manufacturer_client_id for r in dealer_rels if r.manufacturer_client_id}
    preferred_names = {r.manufacturer_name.lower() for r in dealer_rels}

    # Batch 25 — batch-load formulations for all brands in one round so
    # the per-brand metadata lookup doesn't fan out into N queries.
    formulation_ids = {
        (b.metadata_ or {}).get("formulation_cosh_id")
        for b in all_brands
    }
    formulation_ids = {fid for fid in formulation_ids if fid}
    formulation_names: dict[str, str] = {}
    if formulation_ids:
        fmt_rows = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.cosh_id.in_(formulation_ids),
                CoshCoreItem.core_type == "formulation",
            )
        )).scalars().all()
        for f in fmt_rows:
            tr = f.translations or {}
            if isinstance(tr, dict):
                formulation_names[f.cosh_id] = tr.get("en") or ""

    group_recommended: list[BrandOption] = []
    group_my: list[BrandOption] = []
    group_other: list[BrandOption] = []
    brand_unit_family: dict[str, str] = {}

    for b in all_brands:
        name = (b.translations or {}).get("en") or b.cosh_id
        manufacturer = (b.metadata_ or {}).get("manufacturer_name")
        manufacturer_client_id = (b.metadata_ or {}).get("manufacturer_client_id")
        is_preferred = (
            manufacturer_client_id in preferred_client_ids or
            (manufacturer and manufacturer.lower() in preferred_names)
        )
        option = BrandOption(
            cosh_id=b.cosh_id, name=name, manufacturer=manufacturer,
            preferred=is_preferred,
        )

        # Unit family — derived from formulation, not maintained per brand.
        fid = (b.metadata_ or {}).get("formulation_cosh_id")
        family = _classify_formulation_to_unit_family(formulation_names.get(fid))
        brand_unit_family[b.cosh_id] = family

        # User spec 2026-05-31: same brand never repeated across groups —
        # appears in highest-priority group only. Recommended > My > Other.
        if b.cosh_id == recommended_cosh_id:
            group_recommended.append(option)
        elif is_preferred:
            group_my.append(option)
        else:
            group_other.append(option)

    # Sort alphabetically within each group (user spec).
    group_recommended.sort(key=lambda b: (b.name or "").lower())
    group_my.sort(key=lambda b: (b.name or "").lower())
    group_other.sort(key=lambda b: (b.name or "").lower())

    return BrandOptionsResult(
        is_locked=False,
        group_recommended=group_recommended,
        group_my=group_my,
        group_other=group_other,
        brand_unit_family=brand_unit_family,
    )
