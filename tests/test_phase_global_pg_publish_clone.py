"""Global PG publish gate + clone-to-draft + lineage (Batch 39P-d,
2026-05-16).

Pins three guarantees:

  1. `assert_global_pg_publish_ready` enforces ≥1 Timeline, every
     Timeline ≥1 Practice, no dangling Conditional Questions.
  2. `clone_global_pg_to_draft` produces a new DRAFT with all
     Practices, Elements, Relations, CQs, and bindings carried via
     the pipe-agnostic `_deep_copy_advisory_content`.
  3. `get_global_pg_lineage` returns every row in the
     `(problem_group, area_or_plant, client_id=NULL)` lineage with
     `is_current` flagging the path's pg_id.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    ConditionalAnswer, ConditionalQuestion, Element, PGRecommendation,
    Practice, PracticeConditional, PracticeL0, Relation, RelationType,
    Timeline,
)
from app.modules.advisory.router import (
    clone_global_pg_to_draft, create_global_pg_relation,
    create_global_pg_conditional_question, get_global_pg_lineage,
    link_global_practice_conditional, publish_global_pg,
)
from app.modules.advisory.schemas import (
    ConditionalQuestionCreate, PracticeConditionalCreate, RelationCreate,
)
from tests.conftest import requires_docker
from tests.factories import make_user


async def _seed_pg(db, *, status: str = "DRAFT") -> PGRecommendation:
    pg = PGRecommendation(
        problem_group_cosh_id=f"pg:{uuid.uuid4().hex[:6]}",
        client_id=None, area_or_plant="AREA_WISE", status=status,
    )
    db.add(pg)
    await db.flush()
    return pg


async def _add_timeline(db, pg: PGRecommendation) -> Timeline:
    tl = Timeline(
        pg_recommendation_id=pg.id, name=f"TL-{uuid.uuid4().hex[:4]}",
        from_type="DAYS_AFTER_DETECTION", from_value=0, to_value=7,
    )
    db.add(tl)
    await db.flush()
    return tl


async def _add_practice(db, tl: Timeline, *, common: str | None = None) -> Practice:
    p = Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
        common_name_cosh_id=common,
    )
    db.add(p)
    await db.flush()
    if common:
        db.add(Element(
            practice_id=p.id, element_type="COMMON_NAME",
            cosh_ref=common, value="",
        ))
        await db.flush()
    return p


# ── Publish gate ───────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_publish_pg_blocks_when_no_timelines(db):
    user = await make_user(db, name="CM")
    pg = await _seed_pg(db)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await publish_global_pg(pg_id=pg.id, db=db, current_user=user)
    assert exc.value.status_code == 422
    body = exc.value.detail
    assert body["code"] == "publish_blocked"
    codes = [m["code"] for m in body["missing"]]
    assert "publish_no_timelines" in codes


@requires_docker
@pytest.mark.asyncio
async def test_publish_pg_blocks_when_timeline_has_no_practice(db):
    user = await make_user(db, name="CM")
    pg = await _seed_pg(db)
    await _add_timeline(db, pg)  # empty timeline
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await publish_global_pg(pg_id=pg.id, db=db, current_user=user)
    body = exc.value.detail
    empty = [m for m in body["missing"] if m["code"] == "publish_timeline_empty"]
    assert len(empty) == 1


@requires_docker
@pytest.mark.asyncio
async def test_publish_pg_blocks_when_dangling_cq(db):
    """A Conditional Question with no YES/NO binding fails the gate.
    Shares the dangling-CQ rule with CCA Global Package."""
    user = await make_user(db, name="CM")
    pg = await _seed_pg(db)
    tl = await _add_timeline(db, pg)
    await _add_practice(db, tl, common="cn:imida")
    await db.commit()
    await create_global_pg_conditional_question(
        pg_id=pg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Rained?", display_order=0),
        db=db, current_user=user,
    )
    with pytest.raises(HTTPException) as exc:
        await publish_global_pg(pg_id=pg.id, db=db, current_user=user)
    body = exc.value.detail
    codes = [m["code"] for m in body["missing"]]
    assert "conditional_question_no_links" in codes


@requires_docker
@pytest.mark.asyncio
async def test_publish_pg_succeeds_when_gates_pass(db):
    user = await make_user(db, name="CM")
    pg = await _seed_pg(db)
    tl = await _add_timeline(db, pg)
    await _add_practice(db, tl, common="cn:imida")
    await db.commit()
    out = await publish_global_pg(pg_id=pg.id, db=db, current_user=user)
    assert out.status == "ACTIVE"


# ── clone-to-draft ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_clone_pg_active_creates_draft_with_content(db):
    user = await make_user(db, name="CM")
    pg = await _seed_pg(db)
    tl = await _add_timeline(db, pg)
    p = await _add_practice(db, tl, common="cn:imida")
    await db.commit()
    pg = await publish_global_pg(pg_id=pg.id, db=db, current_user=user)
    assert pg.status == "ACTIVE"

    draft = await clone_global_pg_to_draft(
        pg_id=pg.id, db=db, current_user=user,
    )
    assert draft.id != pg.id
    assert draft.status == "DRAFT"
    assert draft.problem_group_cosh_id == pg.problem_group_cosh_id
    assert draft.area_or_plant == pg.area_or_plant

    new_tls = (await db.execute(
        select(Timeline).where(Timeline.pg_recommendation_id == draft.id)
    )).scalars().all()
    assert len(new_tls) == 1
    new_ps = (await db.execute(
        select(Practice).where(Practice.timeline_id == new_tls[0].id)
    )).scalars().all()
    assert len(new_ps) == 1
    new_els = (await db.execute(
        select(Element).where(Element.practice_id == new_ps[0].id)
    )).scalars().all()
    assert {e.element_type for e in new_els} == {"COMMON_NAME"}


@requires_docker
@pytest.mark.asyncio
async def test_clone_pg_carries_relation_and_cq_with_bindings(db):
    """Sub-structures (Relations + CQs + PracticeConditional bindings)
    survive the clone — same UCAT contract as CCA's clone-to-draft."""
    user = await make_user(db, name="CM")
    pg = await _seed_pg(db)
    tl = await _add_timeline(db, pg)
    p_a = await _add_practice(db, tl, common="cn:a")
    p_b = await _add_practice(db, tl, common="cn:b")
    await db.commit()
    rel = await create_global_pg_relation(
        pg_id=pg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND, parts=[[[p_a.id, p_b.id]]],
        ),
        db=db, current_user=user,
    )
    cq = await create_global_pg_conditional_question(
        pg_id=pg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Rained?", display_order=0),
        db=db, current_user=user,
    )
    # Bind the CQ to a fresh practice (not part of the relation, so
    # PracticeConditional binding is allowed).
    p_solo = await _add_practice(db, tl, common="cn:solo")
    await db.commit()
    await link_global_practice_conditional(
        practice_id=p_solo.id,
        request=PracticeConditionalCreate(
            practice_id=p_solo.id, question_id=cq.id, answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=user,
    )
    pg = await publish_global_pg(pg_id=pg.id, db=db, current_user=user)

    draft = await clone_global_pg_to_draft(
        pg_id=pg.id, db=db, current_user=user,
    )
    # Relation copied + practices re-wired.
    new_rels = (await db.execute(
        select(Relation).join(Timeline).where(Timeline.pg_recommendation_id == draft.id)
    )).scalars().all()
    assert len(new_rels) == 1
    new_practices = (await db.execute(
        select(Practice).join(Timeline).where(Timeline.pg_recommendation_id == draft.id)
    )).scalars().all()
    in_relation = [p for p in new_practices if p.relation_id is not None]
    assert len(in_relation) == 2
    # CQ + PracticeConditional binding copied.
    new_cqs = (await db.execute(
        select(ConditionalQuestion).join(Timeline)
        .where(Timeline.pg_recommendation_id == draft.id)
    )).scalars().all()
    assert len(new_cqs) == 1
    assert new_cqs[0].id != cq.id
    new_pcs = (await db.execute(
        select(PracticeConditional).where(
            PracticeConditional.question_id == new_cqs[0].id,
        )
    )).scalars().all()
    assert len(new_pcs) == 1
    assert new_pcs[0].answer == ConditionalAnswer.YES


@requires_docker
@pytest.mark.asyncio
async def test_clone_pg_flips_existing_draft_to_inactive(db):
    user = await make_user(db, name="CM")
    pg = await _seed_pg(db)
    tl = await _add_timeline(db, pg)
    await _add_practice(db, tl, common="cn:x")
    await db.commit()
    pg = await publish_global_pg(pg_id=pg.id, db=db, current_user=user)
    draft1 = await clone_global_pg_to_draft(
        pg_id=pg.id, db=db, current_user=user,
    )
    draft2 = await clone_global_pg_to_draft(
        pg_id=pg.id, db=db, current_user=user,
    )
    refreshed = (await db.execute(
        select(PGRecommendation).where(PGRecommendation.id == draft1.id)
    )).scalar_one()
    assert refreshed.status == "INACTIVE"
    assert draft2.status == "DRAFT"


@requires_docker
@pytest.mark.asyncio
async def test_clone_pg_refuses_draft_source(db):
    user = await make_user(db, name="CM")
    pg = await _seed_pg(db)  # DRAFT by default
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await clone_global_pg_to_draft(
            pg_id=pg.id, db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "clone_source_is_draft"


# ── lineage ────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_lineage_lists_all_rows_with_is_current(db):
    user = await make_user(db, name="CM")
    pg = await _seed_pg(db)
    tl = await _add_timeline(db, pg)
    await _add_practice(db, tl, common="cn:x")
    await db.commit()
    pg = await publish_global_pg(pg_id=pg.id, db=db, current_user=user)
    draft = await clone_global_pg_to_draft(
        pg_id=pg.id, db=db, current_user=user,
    )

    rows = await get_global_pg_lineage(
        pg_id=draft.id, db=db, current_user=user,
    )
    ids = [r["id"] for r in rows]
    assert pg.id in ids and draft.id in ids
    # DRAFT first.
    assert rows[0]["status"] == "DRAFT"
    assert rows[0]["id"] == draft.id
    assert next(r for r in rows if r["id"] == draft.id)["is_current"] is True
    assert next(r for r in rows if r["id"] == pg.id)["is_current"] is False
