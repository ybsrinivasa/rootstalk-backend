"""CA-SP Phase 2 (2026-05-21) — clone-to-draft + lineage.

Pre-fix: SP detail page on testing showed neither a "+Start New Edit"
button nor a Publish button on ACTIVE rows — there was no way to
make changes. Phase 1 hardened the backend to refuse mutations on
non-DRAFT SPs; Phase 2 (this file) adds the path forward:

  • POST .../clone-to-draft → new DRAFT row in the same lineage,
    reusing an existing DRAFT slot when present.
  • GET  .../lineage       → flat list of every row sharing
    (client_id, specific_problem_cosh_id, crop_cosh_id), ordered
    DRAFT-first.

Mirrors test_phase_pg_versioning.py shape exactly so future readers
spot the parallel without effort.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Practice, SPRecommendation, Timeline,
)
from app.modules.advisory.router import (
    clone_client_sp_to_draft, get_client_sp_lineage,
)
from app.modules.clients.models import ClientUserRole
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_sp_practice, make_sp_recommendation,
    make_sp_timeline, make_user,
)


async def _seed_se(db, client):
    se = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    return se


async def _make_active_sp_with_timeline(db, client):
    """ACTIVE SP carrying one timeline + one practice — enough to
    prove deep-copy lands every layer."""
    sp = await make_sp_recommendation(db, client)
    tl = await make_sp_timeline(db, sp, name="TL_src")
    await make_sp_practice(db, tl)
    sp.status = "ACTIVE"
    await db.commit()
    return sp


# ── clone_client_sp_to_draft ──────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_clone_active_to_draft_deep_copies_content(db):
    """The "+ Start new edit" flow: clone an ACTIVE row → new DRAFT
    with timelines / practices / elements duplicated."""
    client = await make_client(db)
    se = await _seed_se(db, client)
    src = await _make_active_sp_with_timeline(db, client)

    cloned = await clone_client_sp_to_draft(
        client_id=client.id, sp_id=src.id,
        db=db, current_user=se,
    )
    assert cloned.id != src.id
    assert cloned.status == "DRAFT"
    assert cloned.specific_problem_cosh_id == src.specific_problem_cosh_id
    assert cloned.crop_cosh_id == src.crop_cosh_id

    # Timelines + practices copied across.
    new_tls = (await db.execute(
        select(Timeline).where(Timeline.sp_recommendation_id == cloned.id)
    )).scalars().all()
    assert len(new_tls) == 1
    new_practices = (await db.execute(
        select(Practice).where(Practice.timeline_id == new_tls[0].id)
    )).scalars().all()
    assert len(new_practices) == 1


@requires_docker
@pytest.mark.asyncio
async def test_clone_to_draft_refuses_draft_source(db):
    """clone-to-draft refuses if the source is already a DRAFT —
    edit it in place. Mirrors clone_client_pg_to_draft's gate."""
    client = await make_client(db)
    se = await _seed_se(db, client)
    sp = await make_sp_recommendation(db, client)
    sp.status = "DRAFT"
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await clone_client_sp_to_draft(
            client_id=client.id, sp_id=sp.id,
            db=db, current_user=se,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "clone_source_is_draft"


@requires_docker
@pytest.mark.asyncio
async def test_clone_to_draft_reuses_existing_draft(db):
    """Single-DRAFT invariant. If a DRAFT already exists in the
    lineage, clone-to-draft returns it without creating a new row
    or demoting the existing one — the SE was probably mid-edit."""
    client = await make_client(db)
    se = await _seed_se(db, client)
    # ACTIVE v1 source.
    src = await _make_active_sp_with_timeline(db, client)
    # Separately seed a DRAFT in the same lineage.
    existing_draft = SPRecommendation(
        specific_problem_cosh_id=src.specific_problem_cosh_id,
        client_id=client.id,
        crop_cosh_id=src.crop_cosh_id,
        status="DRAFT",
        version=1,
    )
    db.add(existing_draft)
    await db.commit()

    reused = await clone_client_sp_to_draft(
        client_id=client.id, sp_id=src.id,
        db=db, current_user=se,
    )
    assert reused.id == existing_draft.id
    assert reused.status == "DRAFT"
    # Make sure no new SP rows landed.
    all_rows = (await db.execute(
        select(SPRecommendation).where(
            SPRecommendation.client_id == client.id,
            SPRecommendation.specific_problem_cosh_id == src.specific_problem_cosh_id,
            SPRecommendation.crop_cosh_id == src.crop_cosh_id,
        )
    )).scalars().all()
    assert len(all_rows) == 2  # the ACTIVE source + the pre-existing DRAFT


@requires_docker
@pytest.mark.asyncio
async def test_clone_to_draft_404_when_missing(db):
    client = await make_client(db)
    se = await _seed_se(db, client)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await clone_client_sp_to_draft(
            client_id=client.id, sp_id="does-not-exist",
            db=db, current_user=se,
        )
    assert exc.value.status_code == 404


# ── lineage endpoint ──────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_lineage_returns_all_versions_draft_first(db):
    """Two-row lineage: clone INACTIVE → DRAFT. Lineage endpoint
    returns both rows with DRAFT-first ordering."""
    client = await make_client(db)
    se = await _seed_se(db, client)
    v1 = await _make_active_sp_with_timeline(db, client)
    # Move v1 to INACTIVE so the clone can land cleanly.
    v1.status = "INACTIVE"
    await db.commit()

    v2 = await clone_client_sp_to_draft(
        client_id=client.id, sp_id=v1.id,
        db=db, current_user=se,
    )

    out = await get_client_sp_lineage(
        client_id=client.id, sp_id=v2.id,
        db=db, current_user=se,
    )
    assert isinstance(out, list)
    assert len(out) == 2
    # DRAFT (v2) comes first; INACTIVE (v1) follows.
    assert out[0]["id"] == v2.id
    assert out[0]["status"] == "DRAFT"
    assert out[0]["is_current"] is True
    assert out[1]["id"] == v1.id
    assert out[1]["status"] == "INACTIVE"
    assert out[1]["is_current"] is False


@requires_docker
@pytest.mark.asyncio
async def test_lineage_scoped_by_natural_key(db):
    """An SP for a different specific_problem in the same client
    must not appear in this SP's lineage. Same for a different crop."""
    client = await make_client(db)
    se = await _seed_se(db, client)
    target = await make_sp_recommendation(
        db, client,
        specific_problem_cosh_id="sp:anthracnose", crop_cosh_id="crop:tomato",
    )
    # Different specific_problem — must NOT appear.
    await make_sp_recommendation(
        db, client,
        specific_problem_cosh_id="sp:blight", crop_cosh_id="crop:tomato",
    )
    # Different crop — must NOT appear.
    await make_sp_recommendation(
        db, client,
        specific_problem_cosh_id="sp:anthracnose", crop_cosh_id="crop:chilli",
    )
    await db.commit()

    out = await get_client_sp_lineage(
        client_id=client.id, sp_id=target.id,
        db=db, current_user=se,
    )
    assert len(out) == 1
    assert out[0]["id"] == target.id


@requires_docker
@pytest.mark.asyncio
async def test_lineage_404_when_missing(db):
    client = await make_client(db)
    se = await _seed_se(db, client)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await get_client_sp_lineage(
            client_id=client.id, sp_id="missing",
            db=db, current_user=se,
        )
    assert exc.value.status_code == 404
