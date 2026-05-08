"""CHA — Global→Local PG re-import + Local PG → Local SP import tests.

Two import flows, both with the same overwrite semantics:

  1. POST /client/{id}/pg-recommendations/import/{global_pg_id}
     [?force=true]
     • First import: deep-copy global PG content into a new local PG.
     • Re-import without force: 409 with `existing` summary so the CA
       portal can show "this will overwrite your edits" warning.
     • Re-import with force=true: wipes existing local content and
       re-imports fresh.

  2. POST /client/{id}/sp-recommendations/{sp_id}/import-from-pg/{local_pg_id}
     [?force=true]
     • Deep-copies a LOCAL PG's timelines/practices/elements into an
       existing SP as a starting point. SE customises from there.
     • Same 409 / force=true overwrite pattern.
     • Cross-client source PGs are 404 — no info leak.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    PGElement, PGPractice, PGRecommendation, PGTimeline,
    SPElement, SPPractice, SPRecommendation, SPTimeline,
)
from app.modules.advisory.router import (
    import_global_pg, import_pg_into_sp,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


# ── Seed helpers ────────────────────────────────────────────────────────────

async def _seed_global_pg_with_content(
    db, *, problem_group_cosh_id="pg:fungal", with_practice=True,
) -> PGRecommendation:
    """Seed a global PG with one timeline and (optionally) one practice
    + one element, so we can verify deep-copy preserves all three."""
    pg = PGRecommendation(
        problem_group_cosh_id=problem_group_cosh_id,
        client_id=None,
        application_type="SPRAY",
        status="ACTIVE",  # publish gate: only ACTIVE globals are importable
    )
    db.add(pg)
    await db.flush()
    tl = PGTimeline(
        pg_recommendation_id=pg.id, name="GTL-1",
        from_type="DAYS_AFTER_DETECTION", from_value=0, to_value=14,
    )
    db.add(tl)
    await db.flush()
    if with_practice:
        p = PGPractice(
            timeline_id=tl.id, l0_type="INPUT", l1_type="PESTICIDE",
            l2_type="CHEMICAL_PESTICIDES", display_order=1,
            is_special_input=False, frequency_days=None,
        )
        db.add(p)
        await db.flush()
        db.add(PGElement(
            practice_id=p.id, element_type="COMMON_NAME",
            cosh_ref="cn:imida", value=None,
            unit_cosh_id=None, display_order=1,
        ))
        await db.flush()
    return pg


# ── Global PG → Local PG ────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_global_pg_import_initial(db):
    user = await make_user(db, name="SE")
    src = await _seed_global_pg_with_content(db)
    client = await make_client(db)
    await db.commit()

    out = await import_global_pg(
        client_id=client.id, global_pg_id=src.id,
        db=db, current_user=user,
    )
    assert out.client_id == client.id
    assert out.parent_id == src.id

    tls = (await db.execute(
        select(PGTimeline).where(PGTimeline.pg_recommendation_id == out.id)
    )).scalars().all()
    assert len(tls) == 1
    practices = (await db.execute(
        select(PGPractice).where(PGPractice.timeline_id == tls[0].id)
    )).scalars().all()
    assert len(practices) == 1
    elements = (await db.execute(
        select(PGElement).where(PGElement.practice_id == practices[0].id)
    )).scalars().all()
    assert len(elements) == 1
    assert elements[0].cosh_ref == "cn:imida"


@requires_docker
@pytest.mark.asyncio
async def test_global_pg_reimport_without_force_returns_409_with_summary(db):
    """Spec: re-import warns the SE that local edits will be overwritten,
    and surfaces what's there. The CA portal renders this as a
    confirmation dialog before re-calling with force=true."""
    user = await make_user(db, name="SE")
    src = await _seed_global_pg_with_content(db)
    client = await make_client(db)
    await db.commit()

    # First import succeeds.
    await import_global_pg(
        client_id=client.id, global_pg_id=src.id,
        db=db, current_user=user,
    )

    # Re-import without force fails with structured 409.
    with pytest.raises(HTTPException) as exc:
        await import_global_pg(
            client_id=client.id, global_pg_id=src.id,
            force=False, db=db, current_user=user,
        )
    assert exc.value.status_code == 409
    body = exc.value.detail
    assert body["code"] == "import_would_overwrite"
    assert body["existing"]["timeline_count"] == 1
    assert "force=true" in body["message"]


@requires_docker
@pytest.mark.asyncio
async def test_global_pg_reimport_with_force_overwrites(db):
    """Force re-import wipes the local copy's content and re-imports
    fresh from the global. Local recommendation row keeps the same id
    so any references survive."""
    user = await make_user(db, name="SE")
    src = await _seed_global_pg_with_content(db)
    client = await make_client(db)
    await db.commit()

    # First import.
    local = await import_global_pg(
        client_id=client.id, global_pg_id=src.id,
        db=db, current_user=user,
    )
    local_id_before = local.id

    # SE adds a custom timeline to their local copy.
    db.add(PGTimeline(
        pg_recommendation_id=local.id, name="SE custom timeline",
        from_type="DAYS_AFTER_DETECTION", from_value=20, to_value=30,
    ))
    await db.commit()
    tl_count_before = (await db.execute(
        select(PGTimeline).where(PGTimeline.pg_recommendation_id == local.id)
    )).scalars().all()
    assert len(tl_count_before) == 2  # 1 from import + 1 SE-added

    # Now force re-import. The custom timeline gets wiped; only the
    # global's 1 timeline remains.
    out = await import_global_pg(
        client_id=client.id, global_pg_id=src.id,
        force=True, db=db, current_user=user,
    )
    assert out.id == local_id_before  # row preserved, content replaced

    after_tls = (await db.execute(
        select(PGTimeline).where(PGTimeline.pg_recommendation_id == out.id)
    )).scalars().all()
    assert len(after_tls) == 1
    assert after_tls[0].name == "GTL-1"  # the global's name


@requires_docker
@pytest.mark.asyncio
async def test_global_pg_import_404_when_global_missing(db):
    user = await make_user(db, name="SE")
    client = await make_client(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await import_global_pg(
            client_id=client.id, global_pg_id="nonexistent",
            db=db, current_user=user,
        )
    assert exc.value.status_code == 404


# ── Local PG → Local SP ─────────────────────────────────────────────────────

async def _seed_local_pg(
    db, *, client_id, problem_group_cosh_id="pg:fungal",
) -> PGRecommendation:
    """Seed a local PG (no parent_id, has client_id) with one timeline
    + one practice + one element."""
    pg = PGRecommendation(
        problem_group_cosh_id=problem_group_cosh_id,
        client_id=client_id,
        parent_id=None,
        application_type="SPRAY",
    )
    db.add(pg)
    await db.flush()
    tl = PGTimeline(
        pg_recommendation_id=pg.id, name="LocalPGTL",
        from_type="DAYS_AFTER_DETECTION", from_value=0, to_value=10,
    )
    db.add(tl)
    await db.flush()
    p = PGPractice(
        timeline_id=tl.id, l0_type="INPUT", l1_type="FERTILIZER",
        l2_type="MANURES", display_order=1, is_special_input=False,
    )
    db.add(p)
    await db.flush()
    db.add(PGElement(
        practice_id=p.id, element_type="COMMON_NAME",
        cosh_ref="cn:fym", value=None, display_order=1,
    ))
    await db.flush()
    return pg


async def _seed_empty_sp(
    db, *, client_id, specific_problem_cosh_id="sp:powdery",
) -> SPRecommendation:
    sp = SPRecommendation(
        specific_problem_cosh_id=specific_problem_cosh_id,
        client_id=client_id, application_type="SPRAY",
    )
    db.add(sp)
    await db.flush()
    return sp


@requires_docker
@pytest.mark.asyncio
async def test_pg_to_sp_initial_import_copies_all_content(db):
    user = await make_user(db, name="SE")
    client = await make_client(db)
    pg = await _seed_local_pg(db, client_id=client.id)
    sp = await _seed_empty_sp(db, client_id=client.id)
    await db.commit()

    out = await import_pg_into_sp(
        client_id=client.id, sp_id=sp.id, local_pg_id=pg.id,
        db=db, current_user=user,
    )
    assert out.id == sp.id

    tls = (await db.execute(
        select(SPTimeline).where(SPTimeline.sp_recommendation_id == sp.id)
    )).scalars().all()
    assert len(tls) == 1
    assert tls[0].name == "LocalPGTL"

    practices = (await db.execute(
        select(SPPractice).where(SPPractice.timeline_id == tls[0].id)
    )).scalars().all()
    assert len(practices) == 1
    assert practices[0].l2_type == "MANURES"

    elements = (await db.execute(
        select(SPElement).where(SPElement.practice_id == practices[0].id)
    )).scalars().all()
    assert len(elements) == 1
    assert elements[0].cosh_ref == "cn:fym"


@requires_docker
@pytest.mark.asyncio
async def test_pg_to_sp_reimport_without_force_returns_409(db):
    user = await make_user(db, name="SE")
    client = await make_client(db)
    pg = await _seed_local_pg(db, client_id=client.id)
    sp = await _seed_empty_sp(db, client_id=client.id)
    await db.commit()

    # First import.
    await import_pg_into_sp(
        client_id=client.id, sp_id=sp.id, local_pg_id=pg.id,
        db=db, current_user=user,
    )

    # Second import without force fails.
    with pytest.raises(HTTPException) as exc:
        await import_pg_into_sp(
            client_id=client.id, sp_id=sp.id, local_pg_id=pg.id,
            force=False, db=db, current_user=user,
        )
    assert exc.value.status_code == 409
    body = exc.value.detail
    assert body["code"] == "import_would_overwrite"
    assert body["existing"]["timeline_count"] == 1


@requires_docker
@pytest.mark.asyncio
async def test_pg_to_sp_reimport_with_force_overwrites(db):
    user = await make_user(db, name="SE")
    client = await make_client(db)
    pg = await _seed_local_pg(db, client_id=client.id)
    sp = await _seed_empty_sp(db, client_id=client.id)
    await db.commit()

    # First import.
    await import_pg_into_sp(
        client_id=client.id, sp_id=sp.id, local_pg_id=pg.id,
        db=db, current_user=user,
    )

    # Add a custom SE timeline to the SP.
    db.add(SPTimeline(
        sp_recommendation_id=sp.id, name="SE custom timeline",
        from_type="DAYS_AFTER_DETECTION", from_value=20, to_value=30,
    ))
    await db.commit()

    # Force re-import; custom timeline wiped.
    await import_pg_into_sp(
        client_id=client.id, sp_id=sp.id, local_pg_id=pg.id,
        force=True, db=db, current_user=user,
    )
    after_tls = (await db.execute(
        select(SPTimeline).where(SPTimeline.sp_recommendation_id == sp.id)
    )).scalars().all()
    assert len(after_tls) == 1
    assert after_tls[0].name == "LocalPGTL"


@requires_docker
@pytest.mark.asyncio
async def test_pg_to_sp_cross_client_source_404(db):
    """Importing from another client's PG returns 404 — no information
    leak about the other client's content."""
    user = await make_user(db, name="SE")
    client_a = await make_client(db, full_name="Client A")
    client_b = await make_client(db, full_name="Client B")
    other_pg = await _seed_local_pg(db, client_id=client_b.id)
    sp_a = await _seed_empty_sp(db, client_id=client_a.id)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await import_pg_into_sp(
            client_id=client_a.id, sp_id=sp_a.id, local_pg_id=other_pg.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_pg_to_sp_404_when_sp_missing(db):
    user = await make_user(db, name="SE")
    client = await make_client(db)
    pg = await _seed_local_pg(db, client_id=client.id)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await import_pg_into_sp(
            client_id=client.id, sp_id="nonexistent", local_pg_id=pg.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 404
