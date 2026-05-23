"""Standard Q&A library — entry-level CRUD (L4-real Sub-batch 1).

Spec §14.9. Subject Experts curate a library of question-rooted
advisories; FarmPundits pick the closest match while responding to
farmer queries.

Pre-L4-real (commit 40f4238 earlier today) the model carried
`answer_text` + `answer_media` as the advisory body. That was the
"notepad" cut and got dropped in migration `4b8e2c1a93f5` once we
adopted UCAT — Q&A advisories carry full Timelines (in
`pg_timelines`,polymorphic by Sub-batch 1) the same way PG and SP
do.

This test file covers entry-level CRUD only. Timeline / Practice /
Element CRUD lands in Sub-batch 2.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.farmpundit.models import StandardResponse
from app.modules.farmpundit.router import (
create_standard_response,delete_standard_response,
list_standard_responses,publish_standard_response,
search_standard_responses,update_standard_response,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_client_user, make_user


async def _se_for(db, *, client):
    user = await make_user(db, name=f"SE-{client.short_name}")
    await make_client_user(db, user=user, client=client)
    return user


# ── create ──────────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_returns_metadata(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    out = await create_standard_response(
client_id=client.id,
data={
"question_text": "Why are leaves yellowing in young paddy?",
"crop_cosh_id": "crop:paddy",
},
db=db,current_user=se,
)
    assert out["question_text"].startswith("Why are leaves")
    assert out["crop_cosh_id"] == "crop:paddy"
    assert out["created_by"] == se.id
    # The advisory body lives on linked timelines — it's not in this
    # response. Sub-batch 2 will add the timeline endpoints.
    assert "answer_text" not in out
    assert "answer_media" not in out


@requires_docker
@pytest.mark.asyncio
async def test_create_strips_whitespace(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    out = await create_standard_response(
client_id=client.id,
data={"question_text": "  Q?  "},
db=db,current_user=se,
)
    assert out["question_text"] == "Q?"


@requires_docker
@pytest.mark.asyncio
async def test_create_requires_question_text(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    for bad in (None, "", "   "):
        with pytest.raises(HTTPException) as ei:
            await create_standard_response(
client_id=client.id,
data={"question_text": bad},
db=db,current_user=se,
)
        assert ei.value.status_code == 422
        assert "question_text" in ei.value.detail


# ── list filters ────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_returns_client_scoped_only(db):
    client_a = await make_client(db)
    client_b = await make_client(db)
    se_a = await _se_for(db, client=client_a)
    se_b = await _se_for(db, client=client_b)
    await db.commit()

    await create_standard_response(
client_id=client_a.id,
data={"question_text": "A's question"},
db=db,current_user=se_a,
)
    await create_standard_response(
client_id=client_b.id,
data={"question_text": "B's question"},
db=db,current_user=se_b,
)

    out = await list_standard_responses(
client_id=client_a.id,db=db,current_user=se_a,
)
    assert {r["question_text"] for r in out} == {"A's question"}


@requires_docker
@pytest.mark.asyncio
async def test_list_filters_by_crop_or_agnostic(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    await create_standard_response(
client_id=client.id,
data={"question_text": "Paddy Q","crop_cosh_id": "crop:paddy"},
db=db,current_user=se,
)
    await create_standard_response(
client_id=client.id,
data={"question_text": "Tomato Q","crop_cosh_id": "crop:tomato"},
db=db,current_user=se,
)
    await create_standard_response(
client_id=client.id,
data={"question_text": "Generic Q"},# no crop
db=db,current_user=se,
)

    paddy = await list_standard_responses(
client_id=client.id,crop_cosh_id="crop:paddy",
db=db,current_user=se,
)
    assert {r["question_text"] for r in paddy} == {"Paddy Q"}

    agnostic = await list_standard_responses(
client_id=client.id,crop_cosh_id="AGNOSTIC",
db=db,current_user=se,
)
    assert {r["question_text"] for r in agnostic} == {"Generic Q"}

    everything = await list_standard_responses(
client_id=client.id,db=db,current_user=se,
)
    assert {r["question_text"] for r in everything} == {"Paddy Q", "Tomato Q", "Generic Q"}


@requires_docker
@pytest.mark.asyncio
async def test_list_filters_by_search(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    await create_standard_response(
client_id=client.id,
data={"question_text": "Yellow leaves on paddy"},
db=db,current_user=se,
)
    await create_standard_response(
client_id=client.id,
data={"question_text": "Brown spots on tomato"},
db=db,current_user=se,
)

    out = await list_standard_responses(
client_id=client.id,search="yellow",
db=db,current_user=se,
)
    assert {r["question_text"] for r in out} == {"Yellow leaves on paddy"}


# ── update + delete ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_update_persists_new_values(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    created = await create_standard_response(
client_id=client.id,
data={"question_text": "Old Q"},
db=db,current_user=se,
)

    updated = await update_standard_response(
client_id=client.id,sr_id=created["id"],
data={
"question_text": "New Q",
"crop_cosh_id": "crop:wheat",
},
db=db,current_user=se,
)
    assert updated["question_text"] == "New Q"
    assert updated["crop_cosh_id"] == "crop:wheat"


@requires_docker
@pytest.mark.asyncio
async def test_update_404_for_other_clients_response(db):
    client_a = await make_client(db)
    client_b = await make_client(db)
    se_a = await _se_for(db, client=client_a)
    se_b = await _se_for(db, client=client_b)
    await db.commit()

    sr_b = await create_standard_response(
client_id=client_b.id,
data={"question_text": "B's question"},
db=db,current_user=se_b,
)

    with pytest.raises(HTTPException) as ei:
        await update_standard_response(
client_id=client_a.id,sr_id=sr_b["id"],
data={"question_text": "Hijacked"},
db=db,current_user=se_a,
)
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_delete_removes_row(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    sr = await create_standard_response(
client_id=client.id,
data={"question_text": "Q?"},
db=db,current_user=se,
)

    await delete_standard_response(
client_id=client.id,sr_id=sr["id"],
db=db,current_user=se,
)

    leftover = (await db.execute(
select(StandardResponse).where(StandardResponse.id == sr["id"])
    )).scalar_one_or_none()
    assert leftover is None


@requires_docker
@pytest.mark.asyncio
async def test_delete_404_for_other_clients_response(db):
    client_a = await make_client(db)
    client_b = await make_client(db)
    se_a = await _se_for(db, client=client_a)
    se_b = await _se_for(db, client=client_b)
    await db.commit()

    sr_b = await create_standard_response(
client_id=client_b.id,
data={"question_text": "B's"},
db=db,current_user=se_b,
)

    with pytest.raises(HTTPException) as ei:
        await delete_standard_response(
client_id=client_a.id,sr_id=sr_b["id"],
db=db,current_user=se_a,
)
    assert ei.value.status_code == 404


# ── membership gate (M7 still applies to all CA-side endpoints) ─────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_rejects_non_member(db):
    client = await make_client(db)
    outsider = await make_user(db, name="Outsider", skip_auto_link=True)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await list_standard_responses(
client_id=client.id,db=db,current_user=outsider,
)
    assert ei.value.status_code == 403


# ── pundit-side search ──────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pundit_search_returns_metadata(db):
    """The Pundit-side search returns the entry's question + crop —
    enough to render the picker. Timelines come via a separate
    fetch when the Pundit selects an entry (Sub-batch 2 / 6)."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    pundit = await make_user(db, name="Pundit")
    await db.commit()

    sr = await create_standard_response(
client_id=client.id,
data={"question_text": "How to control aphids?"},
db=db,current_user=se,
)
    # Pundit-side search only sees ACTIVE rows.
    await publish_standard_response(
client_id=client.id,sr_id=sr["id"],db=db,current_user=se,
)

    out = await search_standard_responses(
client_id=client.id,search="aphids",
db=db,current_user=pundit,
)
    assert len(out) == 1
    assert out[0]["question_text"] == "How to control aphids?"


# ── pg_timelines polymorphism: dual-FK CHECK ────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pg_timelines_check_constraint_rejects_dual_parent(db):
    """A row with BOTH pg_recommendation_id and standard_response_id
    set must fail the DB CHECK. This is the invariant that makes
    `parent_kind` a sound derivation — exactly one parent, ever."""
    from app.modules.advisory.models import (
PGRecommendation,Timeline,
)
    from sqlalchemy.exc import IntegrityError

    client = await make_client(db)
    await db.commit()

    pg = PGRecommendation(
problem_group_cosh_id="pg:test",
client_id=client.id,
area_or_plant="AREA_WISE",
status="DRAFT",
)
    db.add(pg)
    await db.flush()

    sr = StandardResponse(
client_id=client.id,question_text="Q?",
)
    db.add(sr)
    await db.flush()

    db.add(Timeline(
pg_recommendation_id=pg.id,
standard_response_id=sr.id,# both set — violates CHECK
name="Bad timeline",
from_value=0,to_value=7,
))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


@requires_docker
@pytest.mark.asyncio
async def test_pg_timelines_check_constraint_rejects_zero_parents(db):
    """Symmetric: NEITHER parent set is also a violation."""
    from app.modules.advisory.models import Timeline
    from sqlalchemy.exc import IntegrityError

    client = await make_client(db)
    await db.commit()

    db.add(Timeline(
pg_recommendation_id=None,
standard_response_id=None,# zero parents — violates CHECK
name="Orphan timeline",
from_value=0,to_value=7,
))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


@requires_docker
@pytest.mark.asyncio
async def test_pg_timeline_parent_kind_property(db):
    """The Python-side `parent_kind` derivation reads 'PG' or 'QA'
    based on which FK is set. No schema column; pure read-side
    convenience for the unified advisory-render service."""
    from app.modules.advisory.models import (
PGRecommendation,Timeline,
)

    client = await make_client(db)
    await db.commit()

    pg = PGRecommendation(
problem_group_cosh_id="pg:test",
client_id=client.id,
area_or_plant="AREA_WISE",
status="DRAFT",
)
    db.add(pg)
    sr = StandardResponse(client_id=client.id, question_text="Q?")
    db.add(sr)
    await db.flush()

    pg_tl = Timeline(
pg_recommendation_id=pg.id,name="PG-rooted",
from_value=0,to_value=7,
)
    qa_tl = Timeline(
standard_response_id=sr.id,name="QA-rooted",
from_value=0,to_value=7,from_type="DAYS_AFTER_RESPONSE",
)
    db.add_all([pg_tl, qa_tl])
    await db.flush()

    assert pg_tl.parent_kind == "PG"
    assert qa_tl.parent_kind == "QA"
