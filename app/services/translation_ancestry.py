"""Builds AncestryContext for a given (entity_type, entity_id).

Kept separate from translation_service.py so the entity-specific
lookup logic doesn't bloat the transport layer. Add a builder here
each time we onboard a new entity_type.

Phase T-2 ships with Package.description only. T-3 adds Element.value
(walks practice → timeline → parent → crop) and the other two.
"""
import logging
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.translation_service import AncestryContext
from app.modules.sync.models import CoshCoreItem

logger = logging.getLogger(__name__)


async def _resolve_crop_name(db: AsyncSession, crop_cosh_id: Optional[str]) -> Optional[str]:
    """English crop name from Cosh Core, or None if unresolved.
    Every content ancestry hits this helper — batched at the Celery
    task level in future once we translate multiple entities in one
    task; for now one call per translate is fine (Package.description
    is authored one at a time)."""
    if not crop_cosh_id:
        return None
    row = (await db.execute(
        select(CoshCoreItem.translations).where(
            CoshCoreItem.cosh_id == crop_cosh_id,
        )
    )).scalar_one_or_none()
    if not row:
        return None
    if isinstance(row, dict):
        return row.get("en") or next((v for v in row.values() if v), None)
    return None


async def build_ancestry_for_package_description(
    db: AsyncSession, package_id: str,
) -> AncestryContext:
    """Package.description ancestry — just crop name for now.
    Adds duration + package name if the model benefits from them
    (currently not used to keep the prompt short)."""
    from app.modules.advisory.models import Package
    pkg = (await db.execute(
        select(Package.crop_cosh_id).where(Package.id == package_id)
    )).scalar_one_or_none()
    crop_name = await _resolve_crop_name(db, pkg) if pkg else None
    return AncestryContext(crop_name=crop_name)


async def build_ancestry_for_seed_variety_description(
    db: AsyncSession, variety_id: str,
) -> AncestryContext:
    """SeedVariety.description_points ancestry — crop + product type."""
    from app.modules.seed_mgmt.models import SeedVariety
    row = (await db.execute(
        select(SeedVariety.crop_cosh_id, SeedVariety.variety_type).where(
            SeedVariety.id == variety_id,
        )
    )).one_or_none()
    if not row:
        return AncestryContext(product_type="SEED")
    crop_cosh_id, variety_type = row
    crop_name = await _resolve_crop_name(db, crop_cosh_id)
    return AncestryContext(
        crop_name=crop_name,
        product_type="SEED",
        field_notes=f"Variety type: {variety_type}" if variety_type else None,
    )


# T-3 additions land here:
#   - build_ancestry_for_element_value(db, element_id)
#   - build_ancestry_for_standard_response_question(db, sr_id)
