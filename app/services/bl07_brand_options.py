"""
BL-07 Brand Selection
Determines whether an order item's practice has a locked brand or an unlocked
brand requiring dealer selection, and returns a grouped brand options list.

Locked brand: Practice has an element of element_type 'brand' with a cosh_ref
  that points to a specific brand in cosh_reference_cache. Dealer confirms and
  proceeds — no selection needed.

Unlocked brand: No locked brand element. Return three groups:
  Group 1 — brands whose manufacturer_cosh_id matches any of the dealer's active
             dealership manufacturer_client_id or manufacturer_name (preferred)
  Group 2 — all other active brands from cosh_reference_cache for this practice
  Group 3 — sentinel "Not in system / Report missing brand"

OR-relation auto-close: When a dealer marks one item in an OR group as
  AVAILABLE, other items in the same order with the same relation_id are set to
  NOT_NEEDED automatically.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class BrandOption:
    cosh_id: str
    name: str
    manufacturer: Optional[str] = None
    preferred: bool = False


@dataclass
class BrandOptionsResult:
    is_locked: bool
    locked_brand_cosh_id: Optional[str] = None
    locked_brand_name: Optional[str] = None
    group1: list[BrandOption] = None
    group2: list[BrandOption] = None

    def __post_init__(self):
        if self.group1 is None:
            self.group1 = []
        if self.group2 is None:
            self.group2 = []

    def to_dict(self) -> dict:
        if self.is_locked:
            return {
                "type": "LOCKED",
                "locked_brand_cosh_id": self.locked_brand_cosh_id,
                "locked_brand_name": self.locked_brand_name,
                "groups": [],
            }
        return {
            "type": "UNLOCKED",
            "locked_brand_cosh_id": None,
            "locked_brand_name": None,
            "groups": [
                {
                    "label": "Your preferred brands",
                    "brands": [{"cosh_id": b.cosh_id, "name": b.name,
                                "manufacturer": b.manufacturer} for b in self.group1],
                },
                {
                    "label": "Other available brands",
                    "brands": [{"cosh_id": b.cosh_id, "name": b.name,
                                "manufacturer": b.manufacturer} for b in self.group2],
                },
            ],
        }


async def get_brand_options(
    db,
    practice_id: str,
    dealer_user_id: str,
) -> BrandOptionsResult:
    """
    Returns brand options for a given practice and dealer.
    Queries cosh_reference_cache for available brands and filters by
    dealer's active dealership relationships.
    """
    from sqlalchemy import select
    from app.modules.advisory.models import Practice, Element
    from app.modules.orders.models import DealerRelationship
    from app.modules.sync.models import CoshReferenceCache

    practice = (await db.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one_or_none()
    if not practice:
        return BrandOptionsResult(is_locked=False)

    elements = (await db.execute(
        select(Element).where(Element.practice_id == practice_id)
    )).scalars().all()

    # Check for locked brand: element_type == 'brand' with a specific cosh_ref
    locked_el = next((e for e in elements if e.element_type == "brand" and e.cosh_ref), None)
    if locked_el:
        brand_entry = (await db.execute(
            select(CoshReferenceCache).where(
                CoshReferenceCache.cosh_id == locked_el.cosh_ref,
                CoshReferenceCache.entity_type == "brand",
            )
        )).scalar_one_or_none()
        brand_name = None
        if brand_entry and brand_entry.translations:
            brand_name = brand_entry.translations.get("en") or locked_el.cosh_ref
        return BrandOptionsResult(
            is_locked=True,
            locked_brand_cosh_id=locked_el.cosh_ref,
            locked_brand_name=brand_name or locked_el.cosh_ref,
        )

    # Unlocked brand — find available brands from cosh_reference_cache
    common_name_el = next((e for e in elements if e.element_type == "common_name" and e.cosh_ref), None)
    common_name_cosh_id = common_name_el.cosh_ref if common_name_el else None

    if common_name_cosh_id:
        brands_result = await db.execute(
            select(CoshReferenceCache).where(
                CoshReferenceCache.entity_type == "brand",
                CoshReferenceCache.parent_cosh_id == common_name_cosh_id,
                CoshReferenceCache.status == "active",
            ).order_by(CoshReferenceCache.cosh_id)
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

    group1, group2 = [], []
    for b in all_brands:
        name = (b.translations or {}).get("en") or b.cosh_id
        manufacturer = (b.metadata_ or {}).get("manufacturer_name")
        manufacturer_client_id = (b.metadata_ or {}).get("manufacturer_client_id")
        is_preferred = (
            manufacturer_client_id in preferred_client_ids or
            (manufacturer and manufacturer.lower() in preferred_names)
        )
        option = BrandOption(cosh_id=b.cosh_id, name=name, manufacturer=manufacturer, preferred=is_preferred)
        if is_preferred:
            group1.append(option)
        else:
            group2.append(option)

    return BrandOptionsResult(is_locked=False, group1=group1, group2=group2)
