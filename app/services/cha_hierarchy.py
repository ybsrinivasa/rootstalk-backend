"""
CHA Recommendation Hierarchy Resolver
Spec: RootsTalk_AgriTeam_Document_v5-2.pdf §8.7

Priority:
1. SP recommendation (client-specific) for the exact diagnosed problem
2. PG recommendation (client-specific) for the parent problem_group of
   that SP (resolved via Cosh's `sp_pg_crops` Connect)
3. PG recommendation (global, client_id=NULL) for the same parent
   problem_group
4. None — no CHA recommendation available

Data-shape notes (locked 2026-05-25):
- BL-08 narrows to a `biological_names` pest cosh_id. That same id
  is what the CA-portal SP authoring stores in
  `sp_recommendations.specific_problem_cosh_id` (the column name is
  legacy; the value is a biological_names id, the "specific problem"
  in the SP/PG vocabulary).
- The mapping from a biological_names pest → its parent
  `problem_groups` id is Cosh's `sp_pg_crops` Connect. We walk it
  via `sp_pg_crops_view.pg_for_sp_crop`.
- PG recommendations are keyed on a `problem_groups` cosh_id, NOT
  on a biological_names id — that's why we need the bridge.
- Older code paths assumed `cosh_core_items` carried a
  `specific_problem` row with a `parent_cosh_id`; Cosh shipped a
  different shape. The earlier resolver returned None for every
  diagnosed pest because it only matched against `specific_problem`
  / `problem_group` (singular) core types. Rewired 2026-05-25.
"""
from dataclasses import dataclass
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


@dataclass
class ResolvedCHA:
    """Result of the hierarchy lookup."""
    recommendation_type: str        # "SP" | "PG"
    recommendation_id: str          # ID in sp_recommendations or pg_recommendations
    problem_name: str               # Display name for the advisory ("Leaf Blast")
    parent_pg_cosh_id: str          # The resolved problem_groups cosh_id
    level: str                      # "SP_CLIENT" | "PG_CLIENT" | "PG_GLOBAL"


async def resolve_cha_recommendation(
    db: AsyncSession,
    client_id: str,
    problem_cosh_id: str,
    crop_cosh_id: Optional[str] = None,
) -> Optional[ResolvedCHA]:
    """
    Full SP → PG hierarchy lookup.

    Steps:
    1. Try SP recommendation (client + this problem + this crop).
    2. Resolve the parent `problem_groups` id via `sp_pg_crops`
       (falls back to direct lookup when the diagnosed id is itself
       a problem_groups item — unusual but possible).
    3. Try PG recommendation (client-specific) on that group,
       optionally narrowed to the crop's `area_or_plant` measure.
    4. Try PG recommendation (global, `client_id IS NULL`) on the
       same group.
    5. Return None when nothing matches — the commit endpoint still
       succeeds, but no TriggeredCHAEntry is created.
    """
    from app.modules.sync.models import CoshCoreItem
    from app.modules.advisory.models import SPRecommendation, PGRecommendation
    from app.services.cosh_crop_view import get_measure_for_biological_name
    from app.services.sp_pg_crops_view import pg_for_sp_crop

    # Display name for the advisory card — resolved from Cosh's
    # translation row for the diagnosed cosh_id. Used in every
    # branch; cheap one-shot lookup.
    diagnosed_core = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id == problem_cosh_id,
            CoshCoreItem.status == "active",
        )
    )).scalar_one_or_none()
    problem_name = (
        (diagnosed_core.translations or {}).get("en", problem_cosh_id)
        if diagnosed_core else problem_cosh_id
    )

    # Step 1: SP_CLIENT — exact specific-problem match by this client.
    # The SP rec's `specific_problem_cosh_id` carries the
    # biological_names id, same shape as what BL-08 emits, so we
    # match directly without going through cosh_core_items.
    sp_q = select(SPRecommendation).where(
        SPRecommendation.specific_problem_cosh_id == problem_cosh_id,
        SPRecommendation.client_id == client_id,
        SPRecommendation.status == "ACTIVE",
    )
    if crop_cosh_id:
        sp_q = sp_q.where(SPRecommendation.crop_cosh_id == crop_cosh_id)
    sp = (await db.execute(sp_q)).scalar_one_or_none()
    if sp:
        return ResolvedCHA(
            recommendation_type="SP",
            recommendation_id=sp.id,
            problem_name=problem_name,
            parent_pg_cosh_id=problem_cosh_id,
            level="SP_CLIENT",
        )

    # Step 2: bridge biological_names → problem_groups via sp_pg_crops.
    parent_pg_cosh_id: Optional[str] = None
    if crop_cosh_id:
        parent_pg_cosh_id = await pg_for_sp_crop(
            db, sp_cosh_id=problem_cosh_id, crop_cosh_id=crop_cosh_id,
        )
    if not parent_pg_cosh_id:
        # Edge: the diagnosed cosh_id may itself be a `problem_groups`
        # Core (BL-08 narrowed to a group-level pest). Honour that.
        pg_core = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.cosh_id == problem_cosh_id,
                CoshCoreItem.core_type == "problem_groups",
                CoshCoreItem.status == "active",
            )
        )).scalar_one_or_none()
        if pg_core:
            parent_pg_cosh_id = problem_cosh_id

    if not parent_pg_cosh_id:
        # No bridge to any PG; honest "nothing to recommend yet".
        return None

    # Crop typing for the area_or_plant filter. Optional — when the
    # crop isn't classified yet we let the filter widen.
    crop_measure: Optional[str] = None
    if crop_cosh_id:
        crop_measure = await get_measure_for_biological_name(db, crop_cosh_id)

    # Step 3: PG_CLIENT — client-authored bundle on the group.
    pg_q = select(PGRecommendation).where(
        PGRecommendation.problem_group_cosh_id == parent_pg_cosh_id,
        PGRecommendation.client_id == client_id,
        PGRecommendation.status == "ACTIVE",
    )
    if crop_measure:
        pg_q = pg_q.where(PGRecommendation.area_or_plant == crop_measure)
    pg_client = (await db.execute(pg_q)).scalar_one_or_none()
    if pg_client:
        return ResolvedCHA(
            recommendation_type="PG",
            recommendation_id=pg_client.id,
            problem_name=problem_name,
            parent_pg_cosh_id=parent_pg_cosh_id,
            level="PG_CLIENT",
        )

    # Step 4: PG_GLOBAL — RootsTalk's curated default.
    pg_global_q = select(PGRecommendation).where(
        PGRecommendation.problem_group_cosh_id == parent_pg_cosh_id,
        PGRecommendation.client_id == None,  # noqa: E711
        PGRecommendation.status == "ACTIVE",
    )
    if crop_measure:
        pg_global_q = pg_global_q.where(
            PGRecommendation.area_or_plant == crop_measure,
        )
    pg_global = (await db.execute(pg_global_q)).scalar_one_or_none()
    if pg_global:
        return ResolvedCHA(
            recommendation_type="PG",
            recommendation_id=pg_global.id,
            problem_name=problem_name,
            parent_pg_cosh_id=parent_pg_cosh_id,
            level="PG_GLOBAL",
        )

    return None
