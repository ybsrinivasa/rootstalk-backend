"""
CHA Recommendation Hierarchy Resolver — unit tests.
Uses mock data to verify the SP→PG lookup priority chain.

Hierarchy (spec §8.7):
1. SP recommendation (client-specific, exact specific_problem match)
2. PG recommendation (client-specific, parent problem_group of the SP)
3. PG recommendation (global, same parent problem_group)
4. None — no recommendation
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


def make_sp_entry(cosh_id: str, parent_pg_id: str, name_en: str):
    e = MagicMock()
    e.cosh_id = cosh_id
    e.entity_type = "specific_problem"
    e.parent_cosh_id = parent_pg_id
    e.translations = {"en": name_en}
    return e


def make_pg_entry(cosh_id: str, name_en: str):
    e = MagicMock()
    e.cosh_id = cosh_id
    e.entity_type = "problem_group"
    e.parent_cosh_id = None
    e.translations = {"en": name_en}
    return e


def make_sp_rec(rec_id: str, specific_problem_cosh_id: str, client_id: str):
    r = MagicMock()
    r.id = rec_id
    r.specific_problem_cosh_id = specific_problem_cosh_id
    r.client_id = client_id
    r.status = "ACTIVE"
    return r


def make_pg_rec(rec_id: str, problem_group_cosh_id: str, client_id):
    r = MagicMock()
    r.id = rec_id
    r.problem_group_cosh_id = problem_group_cosh_id
    r.client_id = client_id
    r.status = "ACTIVE"
    return r


# ── Helpers to build a mock DB session ───────────────────────────────────────

def mock_db_returning(
    cosh_entry=None,
    sp_rec=None,
    pg_client_rec=None,
    pg_global_rec=None,
):
    """Returns an AsyncSession mock that returns controlled data for each query."""
    db = AsyncMock()

    async def execute_side_effect(query):
        result = MagicMock()
        q_str = str(query)

        # Route by what seems to be queried (fragile but sufficient for unit tests)
        if 'cosh_reference_cache' in q_str.lower() or hasattr(query, 'froms'):
            result.scalar_one_or_none = MagicMock(return_value=cosh_entry)
        else:
            result.scalar_one_or_none = MagicMock(return_value=None)

        return result

    db.execute = AsyncMock(side_effect=execute_side_effect)
    return db


# ── Tests using the service directly with mocked DB ──────────────────────────

@pytest.mark.asyncio
async def test_sp_client_takes_priority():
    """If SP recommendation exists for client → deliver SP, never check PG."""
    from app.services.cha_hierarchy import resolve_cha_recommendation

    sp_entry = make_sp_entry("sp_blast_paddy", "pg_foliar_fungal", "Leaf Blast")
    sp_rec = make_sp_rec("sp_rec_1", "sp_blast_paddy", "client_A")

    db = AsyncMock()
    call_count = [0]

    async def mock_execute(query):
        call_count[0] += 1
        result = MagicMock()
        if call_count[0] == 1:
            # First call: cosh_reference_cache lookup → return SP entry
            result.scalar_one_or_none = MagicMock(return_value=sp_entry)
        elif call_count[0] == 2:
            # Second call: SP recommendation lookup → return match
            result.scalar_one_or_none = MagicMock(return_value=sp_rec)
        else:
            result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    db.execute = AsyncMock(side_effect=mock_execute)

    resolved = await resolve_cha_recommendation(db, "client_A", "sp_blast_paddy")

    assert resolved is not None
    assert resolved.recommendation_type == "SP"
    assert resolved.recommendation_id == "sp_rec_1"
    assert resolved.level == "SP_CLIENT"
    assert resolved.problem_name == "Leaf Blast"
    assert resolved.parent_pg_cosh_id == "pg_foliar_fungal"


@pytest.mark.asyncio
async def test_pg_client_when_no_sp():
    """No SP recommendation → fall back to client-specific PG."""
    from app.services.cha_hierarchy import resolve_cha_recommendation

    sp_entry = make_sp_entry("sp_blast_paddy", "pg_foliar_fungal", "Leaf Blast")
    pg_rec = make_pg_rec("pg_rec_1", "pg_foliar_fungal", "client_A")

    call_count = [0]
    db = AsyncMock()

    async def mock_execute(query):
        call_count[0] += 1
        result = MagicMock()
        if call_count[0] == 1:
            result.scalar_one_or_none = MagicMock(return_value=sp_entry)  # cosh cache
        elif call_count[0] == 2:
            result.scalar_one_or_none = MagicMock(return_value=None)       # no SP rec
        elif call_count[0] == 3:
            result.scalar_one_or_none = MagicMock(return_value=pg_rec)     # client PG rec
        else:
            result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    db.execute = AsyncMock(side_effect=mock_execute)

    resolved = await resolve_cha_recommendation(db, "client_A", "sp_blast_paddy")

    assert resolved is not None
    assert resolved.recommendation_type == "PG"
    assert resolved.recommendation_id == "pg_rec_1"
    assert resolved.level == "PG_CLIENT"
    assert resolved.parent_pg_cosh_id == "pg_foliar_fungal"


@pytest.mark.asyncio
async def test_pg_global_when_no_client_pg():
    """No SP, no client PG → fall back to global PG."""
    from app.services.cha_hierarchy import resolve_cha_recommendation

    sp_entry = make_sp_entry("sp_blast_paddy", "pg_foliar_fungal", "Leaf Blast")
    pg_global = make_pg_rec("pg_global_1", "pg_foliar_fungal", None)

    call_count = [0]
    db = AsyncMock()

    async def mock_execute(query):
        call_count[0] += 1
        result = MagicMock()
        if call_count[0] == 1:
            result.scalar_one_or_none = MagicMock(return_value=sp_entry)   # cosh cache
        elif call_count[0] == 2:
            result.scalar_one_or_none = MagicMock(return_value=None)        # no SP rec
        elif call_count[0] == 3:
            result.scalar_one_or_none = MagicMock(return_value=None)        # no client PG
        elif call_count[0] == 4:
            result.scalar_one_or_none = MagicMock(return_value=pg_global)   # global PG
        else:
            result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    db.execute = AsyncMock(side_effect=mock_execute)

    resolved = await resolve_cha_recommendation(db, "client_A", "sp_blast_paddy")

    assert resolved is not None
    assert resolved.recommendation_type == "PG"
    assert resolved.recommendation_id == "pg_global_1"
    assert resolved.level == "PG_GLOBAL"


@pytest.mark.asyncio
async def test_none_when_no_recommendations():
    """No SP, no client PG, no global PG → returns None."""
    from app.services.cha_hierarchy import resolve_cha_recommendation

    sp_entry = make_sp_entry("sp_unknown", "pg_unknown_group", "Unknown Problem")

    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(side_effect=[
        sp_entry,   # cosh cache
        None,        # no SP rec
        None,        # no client PG
        None,        # no global PG
    ])
    db.execute = AsyncMock(return_value=result)

    resolved = await resolve_cha_recommendation(db, "client_A", "sp_unknown")
    assert resolved is None


@pytest.mark.asyncio
async def test_problem_group_input_skips_sp_lookup():
    """If problem_cosh_id is a problem_group (not a specific_problem), skip SP lookup."""
    from app.services.cha_hierarchy import resolve_cha_recommendation

    pg_entry = make_pg_entry("pg_foliar_fungal", "Foliar Fungal Diseases")
    pg_rec = make_pg_rec("pg_rec_1", "pg_foliar_fungal", "client_A")

    call_count = [0]
    db = AsyncMock()

    async def mock_execute(query):
        call_count[0] += 1
        result = MagicMock()
        if call_count[0] == 1:
            result.scalar_one_or_none = MagicMock(return_value=pg_entry)  # cosh cache → it's a PG
        elif call_count[0] == 2:
            result.scalar_one_or_none = MagicMock(return_value=pg_rec)    # client PG rec
        else:
            result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    db.execute = AsyncMock(side_effect=mock_execute)

    resolved = await resolve_cha_recommendation(db, "client_A", "pg_foliar_fungal")

    assert resolved is not None
    assert resolved.recommendation_type == "PG"
    assert call_count[0] == 2   # Only 2 queries: cosh cache + client PG (no SP query)


@pytest.mark.asyncio
async def test_problem_name_set_correctly_from_cosh():
    """problem_name in result comes from cosh_reference_cache translations."""
    from app.services.cha_hierarchy import resolve_cha_recommendation

    sp_entry = make_sp_entry("sp_blast_paddy", "pg_foliar_fungal", "Paddy Leaf Blast")
    sp_rec = make_sp_rec("sp_rec_1", "sp_blast_paddy", "client_A")

    call_count = [0]
    db = AsyncMock()

    async def mock_execute(query):
        call_count[0] += 1
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(
            return_value=sp_entry if call_count[0] == 1 else sp_rec
        )
        return result

    db.execute = AsyncMock(side_effect=mock_execute)

    resolved = await resolve_cha_recommendation(db, "client_A", "sp_blast_paddy")

    assert resolved.problem_name == "Paddy Leaf Blast"
