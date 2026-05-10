"""
CHA Recommendation Hierarchy Resolver
Spec: RootsTalk_AgriTeam_Document_v5-2.pdf §8.7

Priority:
1. SP recommendation (client-specific) for the exact specific_problem_cosh_id
2. PG recommendation (client-specific) for the parent problem_group of that SP
3. PG recommendation (global, client_id=NULL) for the same parent problem_group
4. None — no CHA recommendation available

The diagnosed problem_cosh_id may be:
- A specific_problem ID (has a parent problem_group in cosh_core_items)
- A problem_group ID (no parent — it IS the group)

We must traverse cosh_core_items to find the parent before PG lookup.
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
    parent_pg_cosh_id: str          # The resolved problem_group_cosh_id
    level: str                      # "SP_CLIENT" | "PG_CLIENT" | "PG_GLOBAL"


async def resolve_cha_recommendation(
    db: AsyncSession,
    client_id: str,
    problem_cosh_id: str,
    crop_cosh_id: Optional[str] = None,
) -> Optional[ResolvedCHA]:
    """
    Full SP→PG hierarchy lookup.

    Steps:
    1. Check cosh_core_items: is problem_cosh_id a specific_problem or a problem_group?
    2. If specific_problem: try SP recommendation (client) — filter by
       crop_cosh_id when provided. Get parent PG ID.
    3. Try PG recommendation (client-specific) — filter by area_or_plant
       matching the crop's typing when crop_cosh_id is provided.
    4. Try PG recommendation (global) — same filter.
    5. Return first match, or None.

    The `crop_cosh_id` argument is needed because PG recommendations
    are bundled per `area_or_plant` post-CHA-PG-Round-1 (2026-05-10):
    a single (client, problem_group) can host both an area-wise
    bundle and a plant-wise bundle. Without the crop, the resolver
    would arbitrarily return one — a regression. Pass the
    subscription's crop here to land on the right bundle.

    SP rows are also crop-bound (post-SP-Round-1); the `crop_cosh_id`
    filter is defensive — sp_cosh_ids are crop-unique by Cosh design,
    but the row-level filter mirrors the PG bundle pattern.
    """
    from app.modules.sync.models import CoshCoreItem
    from app.modules.advisory.models import SPRecommendation, PGRecommendation
    from app.services.cosh_crop_view import get_measure_for_biological_name

    # Step 1: Identify the entity type from cosh_core_items
    cosh_entry = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id == problem_cosh_id,
            CoshCoreItem.core_type.in_(["specific_problem", "problem_group"]),
        )
    )).scalar_one_or_none()

    is_specific_problem = cosh_entry and cosh_entry.core_type == "specific_problem"

    # Determine the parent problem_group_cosh_id
    if is_specific_problem:
        parent_pg_cosh_id = cosh_entry.parent_cosh_id  # problem_group_cosh_id
        problem_name = (cosh_entry.translations or {}).get("en", problem_cosh_id)
    else:
        # Either a problem_group directly, or unknown (no Cosh data yet)
        parent_pg_cosh_id = problem_cosh_id
        problem_name = (cosh_entry.translations or {}).get("en", problem_cosh_id) if cosh_entry else problem_cosh_id

    # Derive the crop's area/plant typing once — used to scope PG to
    # the right bundle. None when crop_cosh_id is not supplied or
    # when Cosh hasn't classified the crop yet (then PG filter
    # falls back to "any bundle").
    crop_measure: Optional[str] = None
    if crop_cosh_id:
        crop_measure = await get_measure_for_biological_name(db, crop_cosh_id)

    # Step 2: SP recommendation (client-specific, only for specific_problems)
    if is_specific_problem:
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
                parent_pg_cosh_id=parent_pg_cosh_id or problem_cosh_id,
                level="SP_CLIENT",
            )

    # Step 3: PG recommendation (client-specific)
    if parent_pg_cosh_id:
        pg_q = select(PGRecommendation).where(
            PGRecommendation.problem_group_cosh_id == parent_pg_cosh_id,
            PGRecommendation.client_id == client_id,
            PGRecommendation.status == "ACTIVE",
        )
        if crop_measure:
            pg_q = pg_q.where(PGRecommendation.area_or_plant == crop_measure)
        pg_client = (await db.execute(pg_q)).scalar_one_or_none()

        if pg_client:
            # Get PG name from cache if not already resolved
            pg_name = problem_name
            if not is_specific_problem and cosh_entry:
                pg_name = (cosh_entry.translations or {}).get("en", parent_pg_cosh_id)
            return ResolvedCHA(
                recommendation_type="PG",
                recommendation_id=pg_client.id,
                problem_name=pg_name,
                parent_pg_cosh_id=parent_pg_cosh_id,
                level="PG_CLIENT",
            )

        # Step 4: PG recommendation (global)
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
            pg_name = problem_name
            if not is_specific_problem and cosh_entry:
                pg_name = (cosh_entry.translations or {}).get("en", parent_pg_cosh_id)
            return ResolvedCHA(
                recommendation_type="PG",
                recommendation_id=pg_global.id,
                problem_name=pg_name,
                parent_pg_cosh_id=parent_pg_cosh_id,
                level="PG_GLOBAL",
            )

    return None  # No recommendation found for this problem
