"""
CHA Recommendation Hierarchy Resolver
Spec: RootsTalk_AgriTeam_Document_v5-2.pdf §8.7

Priority:
1. SP recommendation (client-specific) for the exact specific_problem_cosh_id
2. PG recommendation (client-specific) for the parent problem_group of that SP
3. PG recommendation (global, client_id=NULL) for the same parent problem_group
4. None — no CHA recommendation available

The diagnosed problem_cosh_id may be:
- A specific_problem ID (has a parent problem_group in cosh_reference_cache)
- A problem_group ID (no parent — it IS the group)

We must traverse cosh_reference_cache to find the parent before PG lookup.
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
) -> Optional[ResolvedCHA]:
    """
    Full SP→PG hierarchy lookup.

    Steps:
    1. Check cosh_reference_cache: is problem_cosh_id a specific_problem or a problem_group?
    2. If specific_problem: try SP recommendation (client). Get parent PG ID.
    3. Try PG recommendation (client-specific).
    4. Try PG recommendation (global).
    5. Return first match, or None.
    """
    from app.modules.sync.models import CoshReferenceCache
    from app.modules.advisory.models import SPRecommendation, PGRecommendation

    # Step 1: Identify the entity type from Cosh cache
    cosh_entry = (await db.execute(
        select(CoshReferenceCache).where(
            CoshReferenceCache.cosh_id == problem_cosh_id,
            CoshReferenceCache.entity_type.in_(["specific_problem", "problem_group"]),
        )
    )).scalar_one_or_none()

    is_specific_problem = cosh_entry and cosh_entry.entity_type == "specific_problem"

    # Determine the parent problem_group_cosh_id
    if is_specific_problem:
        parent_pg_cosh_id = cosh_entry.parent_cosh_id  # problem_group_cosh_id
        problem_name = (cosh_entry.translations or {}).get("en", problem_cosh_id)
    else:
        # Either a problem_group directly, or unknown (no Cosh data yet)
        parent_pg_cosh_id = problem_cosh_id
        problem_name = (cosh_entry.translations or {}).get("en", problem_cosh_id) if cosh_entry else problem_cosh_id

    # Step 2: SP recommendation (client-specific, only for specific_problems)
    if is_specific_problem:
        sp = (await db.execute(
            select(SPRecommendation).where(
                SPRecommendation.specific_problem_cosh_id == problem_cosh_id,
                SPRecommendation.client_id == client_id,
                SPRecommendation.status == "ACTIVE",
            )
        )).scalar_one_or_none()

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
        pg_client = (await db.execute(
            select(PGRecommendation).where(
                PGRecommendation.problem_group_cosh_id == parent_pg_cosh_id,
                PGRecommendation.client_id == client_id,
                PGRecommendation.status == "ACTIVE",
            )
        )).scalar_one_or_none()

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
        pg_global = (await db.execute(
            select(PGRecommendation).where(
                PGRecommendation.problem_group_cosh_id == parent_pg_cosh_id,
                PGRecommendation.client_id == None,  # noqa: E711
                PGRecommendation.status == "ACTIVE",
            )
        )).scalar_one_or_none()

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
