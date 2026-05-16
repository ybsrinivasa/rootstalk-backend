"""Global PG Conditional Question endpoints (Batch 39P-c, 2026-05-16).

Pin the new `/advisory/global/pg-recommendations/{pg_id}/timelines/
{tl_id}/conditional-questions` POST + GET pair against the same body
shape as the CCA Global sibling. PUT + DELETE at the pipe-agnostic
`/advisory/global/conditional-questions/{id}` URLs are extended to
accept PG-rooted CQs too; the Practice→CQ binding endpoint goes
through `_get_global_practice`, which Batch 39P-c widened to accept
PG-rooted practices.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    ConditionalAnswer, ConditionalQuestion, Element, PGRecommendation,
    Practice, PracticeConditional, PracticeL0, Timeline,
)
from app.modules.advisory.router import (
    create_global_pg_conditional_question,
    delete_global_conditional_question,
    link_global_practice_conditional,
    list_global_pg_conditional_questions,
    update_global_conditional_question,
)
from app.modules.advisory.schemas import (
    ConditionalQuestionCreate, CQAttachmentIn, CQReplace,
    PracticeConditionalCreate,
)
from tests.conftest import requires_docker
from tests.factories import make_user


async def _seed_global_pg_with_practice(db):
    pg = PGRecommendation(
        problem_group_cosh_id=f"pg:{uuid.uuid4().hex[:6]}",
        client_id=None, area_or_plant="AREA_WISE", status="DRAFT",
    )
    db.add(pg)
    await db.flush()
    tl = Timeline(
        pg_recommendation_id=pg.id, name="TL",
        from_type="DAYS_AFTER_DETECTION", from_value=0, to_value=7,
    )
    db.add(tl)
    await db.flush()
    p = Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
        common_name_cosh_id="cn:imida",
    )
    db.add(p)
    await db.flush()
    db.add(Element(
        practice_id=p.id, element_type="COMMON_NAME",
        cosh_ref="cn:imida", value="",
    ))
    await db.flush()
    return pg, tl, p


@requires_docker
@pytest.mark.asyncio
async def test_create_pg_cq_persists_on_timeline(db):
    user = await make_user(db, name="CM")
    pg, tl, _ = await _seed_global_pg_with_practice(db)
    await db.commit()
    out = await create_global_pg_conditional_question(
        pg_id=pg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Rained?", display_order=0),
        db=db, current_user=user,
    )
    assert out.question_text == "Rained?"
    assert out.timeline_id == tl.id


@requires_docker
@pytest.mark.asyncio
async def test_list_pg_cqs_returns_bundle_with_practice_attachment(db):
    """GET bundles YES/NO attachments. Binding via the now-pipe-agnostic
    Practice→CQ link endpoint pins the PG path end-to-end."""
    user = await make_user(db, name="CM")
    pg, tl, p = await _seed_global_pg_with_practice(db)
    await db.commit()
    cq = await create_global_pg_conditional_question(
        pg_id=pg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Rained?", display_order=0),
        db=db, current_user=user,
    )
    await link_global_practice_conditional(
        practice_id=p.id,
        request=PracticeConditionalCreate(
            practice_id=p.id, question_id=cq.id, answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=user,
    )
    out = await list_global_pg_conditional_questions(
        pg_id=pg.id, timeline_id=tl.id, db=db, current_user=user,
    )
    assert len(out) == 1
    row = out[0]
    assert row["question_text"] == "Rained?"
    assert row["yes"] == {"kind": "practice", "id": p.id}
    assert row["no"] is None


@requires_docker
@pytest.mark.asyncio
async def test_put_pg_cq_rebinds_atomically(db):
    """PUT at `/advisory/global/conditional-questions/{id}` accepts
    PG-rooted CQs after Batch 39P-c. Atomic rebind clears + rewires."""
    user = await make_user(db, name="CM")
    pg, tl, p = await _seed_global_pg_with_practice(db)
    await db.commit()
    cq = await create_global_pg_conditional_question(
        pg_id=pg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Rained?", display_order=0),
        db=db, current_user=user,
    )
    await update_global_conditional_question(
        question_id=cq.id,
        request=CQReplace(
            question_text="Did it rain?",
            yes=CQAttachmentIn(kind="practice", id=p.id),
            no=None,
        ),
        db=db, current_user=user,
    )
    refreshed = (await db.execute(
        select(ConditionalQuestion).where(ConditionalQuestion.id == cq.id)
    )).scalar_one()
    assert refreshed.question_text == "Did it rain?"
    pc = (await db.execute(
        select(PracticeConditional).where(
            PracticeConditional.question_id == cq.id,
        )
    )).scalar_one_or_none()
    assert pc is not None
    assert pc.practice_id == p.id
    assert pc.answer == ConditionalAnswer.YES


@requires_docker
@pytest.mark.asyncio
async def test_delete_pg_cq_via_shared_endpoint(db):
    user = await make_user(db, name="CM")
    pg, tl, _ = await _seed_global_pg_with_practice(db)
    await db.commit()
    cq = await create_global_pg_conditional_question(
        pg_id=pg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="X?", display_order=0),
        db=db, current_user=user,
    )
    await delete_global_conditional_question(
        question_id=cq.id, db=db, current_user=user,
    )
    remaining = (await db.execute(
        select(ConditionalQuestion).where(ConditionalQuestion.id == cq.id)
    )).scalar_one_or_none()
    assert remaining is None


@requires_docker
@pytest.mark.asyncio
async def test_pg_cq_404_on_client_scoped_pg(db):
    """PG-side authoring is Global-only — a client-scoped PG row is 404."""
    user = await make_user(db, name="CM")
    from tests.factories import make_client
    client = await make_client(db)
    pg = PGRecommendation(
        problem_group_cosh_id="pg:test", client_id=client.id,
        area_or_plant="AREA_WISE", status="DRAFT",
    )
    db.add(pg)
    await db.flush()
    tl = Timeline(
        pg_recommendation_id=pg.id, name="TL",
        from_type="DAYS_AFTER_DETECTION", from_value=0, to_value=7,
    )
    db.add(tl)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await list_global_pg_conditional_questions(
            pg_id=pg.id, timeline_id=tl.id, db=db, current_user=user,
        )
    assert exc.value.status_code == 404
