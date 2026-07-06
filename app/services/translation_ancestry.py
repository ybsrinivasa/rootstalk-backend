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


# ── Phase T-3 additions ──────────────────────────────────────────────────

# Element types whose `value` we translate. Everything else (URLs,
# formula tokens, cosh-sourced dropdown values, numeric fields) skipped.
# Whitelist over blacklist so a new numeric element type doesn't
# accidentally get sent to Claude.
TRANSLATABLE_ELEMENT_TYPES = frozenset({
    "INSTRUCTIONS",
    "TITLE",
    "DESCRIPTION",
})


async def build_ancestry_for_element_value(
    db: AsyncSession, element_id: str,
) -> tuple[Optional[AncestryContext], Optional[str]]:
    """Element ancestry — walks practice → timeline → parent (one of
    Package / PGRecommendation / SPRecommendation / StandardResponse)
    → crop_cosh_id → crop name.

    Returns (ancestry, element_type) so the caller can decide whether
    the element_type is in the translatable set BEFORE fetching source
    text. Returns (None, None) when the element or its parents don't
    exist.
    """
    from app.modules.advisory.models import (
        Element, Practice, Timeline, Package,
        PGRecommendation, SPRecommendation,
    )
    from app.modules.farmpundit.models import StandardResponse

    row = (await db.execute(
        select(
            Element.element_type,
            Practice.l0_type, Practice.l1_type, Practice.l2_type,
            Timeline.package_id, Timeline.pg_recommendation_id,
            Timeline.sp_recommendation_id, Timeline.standard_response_id,
        )
        .join(Practice, Practice.id == Element.practice_id)
        .join(Timeline, Timeline.id == Practice.timeline_id)
        .where(Element.id == element_id)
    )).one_or_none()
    if not row:
        return None, None

    (element_type, l0_type, l1_type, l2_type,
     package_id, pg_id, sp_id, sr_id) = row

    crop_cosh_id: Optional[str] = None
    parent_kind: Optional[str] = None
    if package_id:
        parent_kind = "CCA package"
        crop_cosh_id = (await db.execute(
            select(Package.crop_cosh_id).where(Package.id == package_id)
        )).scalar_one_or_none()
    elif pg_id:
        parent_kind = "CHA problem-group advisory"
        # PGRecommendation is crop-agnostic by design — no
        # crop_cosh_id column. Prompt just carries the parent_kind
        # hint; crop_name stays None which the read-path handles
        # ("no additional context available" line in the prompt).
        crop_cosh_id = None
    elif sp_id:
        parent_kind = "CHA specific-problem advisory"
        crop_cosh_id = (await db.execute(
            select(SPRecommendation.crop_cosh_id).where(SPRecommendation.id == sp_id)
        )).scalar_one_or_none()
    elif sr_id:
        parent_kind = "Q&A standard response"
        crop_cosh_id = (await db.execute(
            select(StandardResponse.crop_cosh_id).where(StandardResponse.id == sr_id)
        )).scalar_one_or_none()

    crop_name = await _resolve_crop_name(db, crop_cosh_id)

    l0_val = l0_type.value if hasattr(l0_type, "value") else str(l0_type or "")
    parts = [f"Element type: {element_type}", f"Parent: {parent_kind or 'unknown'}"]
    if l0_val:
        parts.append(f"L0: {l0_val}")
    if l1_type:
        parts.append(f"L1: {l1_type}")
    if l2_type:
        parts.append(f"L2: {l2_type}")
    field_notes = " · ".join(parts)

    return AncestryContext(crop_name=crop_name, field_notes=field_notes), element_type


async def build_ancestry_for_standard_response_question(
    db: AsyncSession, sr_id: str,
) -> AncestryContext:
    """StandardResponse.question_text ancestry — just crop name.
    Q&A standard responses are curated as farmer-facing FAQ text; the
    prompt hint helps the model use the right register."""
    from app.modules.farmpundit.models import StandardResponse
    crop_cosh_id = (await db.execute(
        select(StandardResponse.crop_cosh_id).where(StandardResponse.id == sr_id)
    )).scalar_one_or_none()
    crop_name = await _resolve_crop_name(db, crop_cosh_id)
    return AncestryContext(
        crop_name=crop_name,
        field_notes="Q&A curated question — should read as a farmer would ask it",
    )
