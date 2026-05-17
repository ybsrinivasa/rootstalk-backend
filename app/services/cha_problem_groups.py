"""Problem Group catalogue — async lookups against Cosh's
`problem_groups` Core (Batch 39R-bridge, 2026-05-17).

History: through Batches 1-28 this module was a hardcoded V1 stopgap
because Cosh hadn't yet shipped a `problem_groups` Connect. Batch
39Q-frontend (2026-05-16) shipped real Cosh problem_groups data and
swapped the SA-portal `/advisory/global/problem-groups` endpoint to
read from it. This batch finishes the job: the CA-portal helpers
(`cha_list_problems`, `cha_list_recommendations`,
`cha_list_timelines`, `cha_list_practices`, plus the
`is_known_problem_group` create-PG validator) now read the same
Cosh source as the SA portal.

Why bridge in two batches: SA-side is just a picker; CA-side has
~10 tests that insert `PGRecommendation` rows directly with the
legacy `pg:fungal_diseases`-style slugs. The conftest fixture is
extended (same commit as this swap) to seed those legacy slugs as
Cosh `problem_groups` items so existing tests keep passing; new
work should use real Cosh UUIDs.

Shape returned by `list_problem_groups` is preserved:
  [{cosh_id, name_en, status}]
sorted alphabetically by `name_en`. Only `active` Cosh items surface.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshCoreItem
from app.services.cosh_constants import COSH_PROBLEM_GROUPS_CORE


# Legacy slugs the CA portal carried through V1. Kept here as a
# module-level constant ONLY so tests/conftest.py can pre-seed them
# as Cosh `problem_groups` items, keeping the ~10 legacy tests green
# while production code reads exclusively from Cosh. Do NOT consume
# this list from production code — it stays in lock-step with what
# the test fixtures auto-seed.
LEGACY_V1_PROBLEM_GROUPS: list[dict] = [
    {"cosh_id": "pg:fungal_diseases",       "name_en": "Fungal Diseases"},
    {"cosh_id": "pg:bacterial_diseases",    "name_en": "Bacterial Diseases"},
    {"cosh_id": "pg:viral_diseases",        "name_en": "Viral Diseases"},
    {"cosh_id": "pg:nematode_infestations", "name_en": "Nematode Infestations"},
    {"cosh_id": "pg:sucking_pests",         "name_en": "Sucking Pests"},
    {"cosh_id": "pg:chewing_pests",         "name_en": "Chewing Pests"},
    {"cosh_id": "pg:boring_pests",          "name_en": "Boring Pests"},
    {"cosh_id": "pg:weed_competition",      "name_en": "Weed Competition"},
    {"cosh_id": "pg:nutrient_deficiency",   "name_en": "Nutrient Deficiency"},
    {"cosh_id": "pg:water_stress",          "name_en": "Water Stress"},
    {"cosh_id": "pg:heat_stress",           "name_en": "Heat Stress"},
    {"cosh_id": "pg:cold_stress",           "name_en": "Cold Stress"},
]


async def list_problem_groups(db: AsyncSession) -> list[dict]:
    """Return [{cosh_id, name_en, status}] sorted by name_en. Only
    `active` Cosh problem_groups items surface. Empty list when
    Cosh has no rows."""
    rows = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == COSH_PROBLEM_GROUPS_CORE,
            CoshCoreItem.status == "active",
        )
    )).scalars().all()
    items = []
    for r in rows:
        t = r.translations or {}
        items.append({
            "cosh_id": r.cosh_id,
            "name_en": t.get("en") or t.get("English") or r.cosh_id,
            "status": "active",
        })
    items.sort(key=lambda x: x["name_en"].casefold())
    return items


async def is_known_problem_group(db: AsyncSession, cosh_id: str) -> bool:
    """Membership check used by the CA-side create-PG validator.
    True iff Cosh has an `active` `problem_groups` row for the given
    cosh_id."""
    row = (await db.execute(
        select(CoshCoreItem.cosh_id).where(
            CoshCoreItem.cosh_id == cosh_id,
            CoshCoreItem.core_type == COSH_PROBLEM_GROUPS_CORE,
            CoshCoreItem.status == "active",
        )
    )).scalar_one_or_none()
    return row is not None
