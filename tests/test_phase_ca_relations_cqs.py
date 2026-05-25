"""Batch N1 (2026-05-18) — CA-side Relations + Conditional Questions.

Mirrors the SA Global CCA endpoint set so the shared RelationsSection
and CQsSection components can mount on CA pipes (CCA in N1; PG, SP,
QA in N2). Pipe-agnostic at the URL level: timeline-scoped GET +
POST, resource-scoped PUT/DELETE.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    ConditionalAnswer, ConditionalQuestion, Element, Package,
    PackageStatus, PackageType, Practice, PracticeConditional,
    PracticeL0, Relation, RelationConditional, RelationType,
    Timeline, TimelineFromType,
)
from app.modules.advisory.router import (
    _assert_timeline_belongs_to_client, create_relation,
    delete_client_conditional_question, delete_client_relation,
    list_client_conditional_questions, list_client_relations,
    update_client_conditional_question,
)
from app.modules.advisory.schemas import (
    CQReplace, RelationCreate,
)
from app.modules.clients.models import ClientUserRole
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_package, make_timeline, make_user,
)


# ── Test seed helpers ──────────────────────────────────────────────────────

async def _seed_two_practices(db, timeline, l1_type="FERTILIZER"):
    """Seed two CCA Practices with a COMMON_NAME element so the
    Relation save rules have non-trivial input identity."""
    practices = []
    for i, name in enumerate(["urea", "dap"]):
        p = Practice(
            timeline_id=timeline.id,
            l0_type=PracticeL0.INPUT,
            l1_type=l1_type,
            l2_type="CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS",
            display_order=i,
            is_special_input=False,
            common_name_cosh_id=f"cn:{name}",
        )
        db.add(p)
        await db.flush()
        db.add(Element(
            practice_id=p.id, element_type="COMMON_NAME",
            cosh_ref=f"cn:{name}", display_order=0,
        ))
        practices.append(p)
    await db.flush()
    return practices


async def _make_relation_on_timeline(db, se, client, timeline, practices):
    """Use the production create_relation endpoint so this test
    exercises the same path the SE hits."""
    return await create_relation(
        client_id=client.id, timeline_id=timeline.id,
        request=RelationCreate(
            relation_type=RelationType.AND, expression="P1 AND P2",
            parts=[[[practices[0].id]], [[practices[1].id]]],
        ),
        db=db, current_user=se,
    )


# ── list_client_relations ─────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_client_relations_empty_returns_empty_list(db):
    client = await make_client(db)
    se = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    await db.commit()
    out = await list_client_relations(
        client_id=client.id, timeline_id=tl.id,
        db=db, current_user=se,
    )
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_list_client_relations_returns_created_relation(db):
    client = await make_client(db)
    se = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    practices = await _seed_two_practices(db, tl)
    await db.commit()
    rel = await _make_relation_on_timeline(db, se, client, tl, practices)

    out = await list_client_relations(
        client_id=client.id, timeline_id=tl.id,
        db=db, current_user=se,
    )
    assert len(out) == 1
    assert out[0]["id"] == rel["id"]
    assert out[0]["relation_type"] == "AND"


# ── delete_client_relation ────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_delete_client_relation_clears_practice_role(db):
    client = await make_client(db)
    se = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    practices = await _seed_two_practices(db, tl)
    await db.commit()
    rel = await _make_relation_on_timeline(db, se, client, tl, practices)
    # Practices now have relation_id set.
    p_after_set = (await db.execute(
        select(Practice).where(Practice.id == practices[0].id)
    )).scalar_one()
    assert p_after_set.relation_id == rel["id"]

    await delete_client_relation(
        client_id=client.id, relation_id=rel["id"],
        db=db, current_user=se,
    )
    # Relation is gone.
    assert (await db.execute(
        select(Relation).where(Relation.id == rel["id"])
    )).scalar_one_or_none() is None
    # Practices stay but their relation_id is cleared.
    for p in practices:
        fresh = (await db.execute(
            select(Practice).where(Practice.id == p.id)
        )).scalar_one()
        assert fresh.relation_id is None
        assert fresh.relation_role is None


@requires_docker
@pytest.mark.asyncio
async def test_list_client_relations_excludes_deleted_relation(db):
    """Repro guard for tester report 2026-05-25: a Relation that was
    deleted continued to appear in the relations table. Asserts that
    list_client_relations no longer surfaces the row immediately
    after delete_client_relation completes — i.e. the API contract
    the frontend relies on (await delete; await list)."""
    client = await make_client(db)
    se = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    practices = await _seed_two_practices(db, tl)
    await db.commit()
    rel = await _make_relation_on_timeline(db, se, client, tl, practices)

    before = await list_client_relations(
        client_id=client.id, timeline_id=tl.id,
        db=db, current_user=se,
    )
    assert len(before) == 1

    await delete_client_relation(
        client_id=client.id, relation_id=rel["id"],
        db=db, current_user=se,
    )

    after = await list_client_relations(
        client_id=client.id, timeline_id=tl.id,
        db=db, current_user=se,
    )
    assert after == [], (
        f"Deleted Relation still surfaces in list: {after}"
    )


@requires_docker
@pytest.mark.asyncio
async def test_delete_client_relation_refuses_when_in_conditional_question(db):
    """Lock rule (2026-05-25): a Relation bound to a CQ refuses delete
    with `relation_in_conditional_question`. User dismantles the CQ
    first."""
    from app.modules.advisory.router import link_relation_conditional
    from app.modules.advisory.schemas import PracticeConditionalCreate
    from fastapi import HTTPException

    client = await make_client(db)
    se = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    practices = await _seed_two_practices(db, tl)
    await db.commit()
    rel = await _make_relation_on_timeline(db, se, client, tl, practices)

    cq = ConditionalQuestion(
        timeline_id=tl.id, question_text="Is the field irrigated?",
    )
    db.add(cq)
    await db.commit()

    await link_relation_conditional(
        client_id=client.id, relation_id=rel["id"],
        request=PracticeConditionalCreate(
            practice_id="ignored", question_id=cq.id,
            answer=ConditionalAnswer.NO,
        ),
        db=db, current_user=se,
    )

    with pytest.raises(HTTPException) as ei:
        await delete_client_relation(
            client_id=client.id, relation_id=rel["id"],
            db=db, current_user=se,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "relation_in_conditional_question"
    assert "Is the field irrigated?" in ei.value.detail["message"]
    assert ei.value.detail["answer_side"] == "NO"

    # Relation still exists.
    assert (await db.execute(
        select(Relation).where(Relation.id == rel["id"])
    )).scalar_one_or_none() is not None


@requires_docker
@pytest.mark.asyncio
async def test_delete_client_relation_404_for_other_client(db):
    client_a = await make_client(db)
    client_b = await make_client(db)
    se_a = await make_user(db, name="SE-A", skip_auto_link=True)
    se_b = await make_user(db, name="SE-B", skip_auto_link=True)
    await make_client_user(
        db, user=se_a, client=client_a, role=ClientUserRole.SUBJECT_EXPERT,
    )
    await make_client_user(
        db, user=se_b, client=client_b, role=ClientUserRole.SUBJECT_EXPERT,
    )
    pkg_a = await make_package(db, client_a, crop_cosh_id="crop:test")
    tl_a = await make_timeline(db, pkg_a, from_type=TimelineFromType.DAS)
    practices = await _seed_two_practices(db, tl_a)
    await db.commit()
    rel = await _make_relation_on_timeline(db, se_a, client_a, tl_a, practices)

    # Client B's SE tries to delete A's relation through B's path — 404.
    with pytest.raises(HTTPException) as exc:
        await delete_client_relation(
            client_id=client_b.id, relation_id=rel["id"],
            db=db, current_user=se_b,
        )
    assert exc.value.status_code == 404


# ── list_client_conditional_questions ────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_client_cqs_returns_created_cq(db):
    client = await make_client(db)
    se = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    cq = ConditionalQuestion(
        timeline_id=tl.id, question_text="Did the rain start?",
    )
    db.add(cq)
    await db.commit()
    out = await list_client_conditional_questions(
        client_id=client.id, timeline_id=tl.id,
        db=db, current_user=se,
    )
    assert len(out) == 1
    assert out[0]["question_text"] == "Did the rain start?"


# ── update_client_conditional_question (atomic replace) ──────────────────

@requires_docker
@pytest.mark.asyncio
async def test_update_client_cq_renames_question(db):
    client = await make_client(db)
    se = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    cq = ConditionalQuestion(timeline_id=tl.id, question_text="Original")
    db.add(cq)
    await db.commit()
    out = await update_client_conditional_question(
        client_id=client.id, question_id=cq.id,
        request=CQReplace(
            question_text="Updated", yes=None, no=None,
        ),
        db=db, current_user=se,
    )
    assert out["question_text"] == "Updated"


@requires_docker
@pytest.mark.asyncio
async def test_update_client_cq_404_for_other_client(db):
    client_a = await make_client(db)
    client_b = await make_client(db)
    se_a = await make_user(db, name="SE-A", skip_auto_link=True)
    se_b = await make_user(db, name="SE-B", skip_auto_link=True)
    await make_client_user(
        db, user=se_a, client=client_a, role=ClientUserRole.SUBJECT_EXPERT,
    )
    await make_client_user(
        db, user=se_b, client=client_b, role=ClientUserRole.SUBJECT_EXPERT,
    )
    pkg_a = await make_package(db, client_a, crop_cosh_id="crop:test")
    tl_a = await make_timeline(db, pkg_a, from_type=TimelineFromType.DAS)
    cq = ConditionalQuestion(timeline_id=tl_a.id, question_text="A's CQ")
    db.add(cq)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await update_client_conditional_question(
            client_id=client_b.id, question_id=cq.id,
            request=CQReplace(
                question_text="hijack", yes=None, no=None,
            ),
            db=db, current_user=se_b,
        )
    assert exc.value.status_code == 404


# ── delete_client_conditional_question ───────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_delete_client_cq_drops_attachments_too(db):
    client = await make_client(db)
    se = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    practices = await _seed_two_practices(db, tl)
    cq = ConditionalQuestion(timeline_id=tl.id, question_text="Q?")
    db.add(cq)
    await db.flush()
    # Bind one practice to the CQ on the YES side.
    db.add(PracticeConditional(
        practice_id=practices[0].id,
        question_id=cq.id,
        answer=ConditionalAnswer.YES,
    ))
    await db.commit()

    await delete_client_conditional_question(
        client_id=client.id, question_id=cq.id,
        db=db, current_user=se,
    )
    # CQ gone.
    assert (await db.execute(
        select(ConditionalQuestion).where(ConditionalQuestion.id == cq.id)
    )).scalar_one_or_none() is None
    # Attachments gone.
    assert (await db.execute(
        select(PracticeConditional).where(PracticeConditional.question_id == cq.id)
    )).scalar_one_or_none() is None
    # Practices still there.
    fresh = (await db.execute(
        select(Practice).where(Practice.id == practices[0].id)
    )).scalar_one()
    assert fresh is not None


@requires_docker
@pytest.mark.asyncio
async def test_delete_client_cq_with_practice_and_relation_attachments(db):
    """Tester report 2026-05-25: CQ delete reportedly fails on CA.
    Reproducer with the fullest attachment shape — PracticeConditional
    on YES, RelationConditional on NO. Confirms backend cascades both
    sides cleanly."""
    from app.modules.advisory.router import (
        link_practice_conditional, link_relation_conditional,
    )
    from app.modules.advisory.schemas import PracticeConditionalCreate

    client = await make_client(db)
    se = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    practices = await _seed_two_practices(db, tl)
    await db.commit()
    # Need a third practice for the YES-side independent binding —
    # the first two are now in a Relation and can't carry their own CQ.
    independent = Practice(
        timeline_id=tl.id,
        l0_type=PracticeL0.INPUT,
        l1_type="FERTILIZER",
        l2_type="CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS",
        display_order=2,
    )
    db.add(independent)
    await db.flush()
    db.add(Element(
        practice_id=independent.id, element_type="COMMON_NAME",
        cosh_ref="cn:mop", display_order=0,
    ))
    await db.commit()
    rel = await _make_relation_on_timeline(db, se, client, tl, practices)

    cq = ConditionalQuestion(timeline_id=tl.id, question_text="Q?")
    db.add(cq)
    await db.flush()
    await db.commit()

    await link_practice_conditional(
        client_id=client.id, practice_id=independent.id,
        request=PracticeConditionalCreate(
            practice_id=independent.id, question_id=cq.id,
            answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=se,
    )
    await link_relation_conditional(
        client_id=client.id, relation_id=rel["id"],
        request=PracticeConditionalCreate(
            practice_id="ignored", question_id=cq.id,
            answer=ConditionalAnswer.NO,
        ),
        db=db, current_user=se,
    )

    await delete_client_conditional_question(
        client_id=client.id, question_id=cq.id,
        db=db, current_user=se,
    )
    assert (await db.execute(
        select(ConditionalQuestion).where(ConditionalQuestion.id == cq.id)
    )).scalar_one_or_none() is None
    assert (await db.execute(
        select(PracticeConditional).where(PracticeConditional.question_id == cq.id)
    )).scalar_one_or_none() is None
    assert (await db.execute(
        select(RelationConditional).where(RelationConditional.question_id == cq.id)
    )).scalar_one_or_none() is None
    # Practices + Relation survive — only the CQ + bindings go.
    assert (await db.execute(
        select(Practice).where(Practice.id == independent.id)
    )).scalar_one_or_none() is not None
    assert (await db.execute(
        select(Relation).where(Relation.id == rel["id"])
    )).scalar_one_or_none() is not None
