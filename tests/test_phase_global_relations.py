"""Global Practice Relations (Batch 39A, 2026-05-15).

Mirror of the client-scoped Relations integration tests in
`test_phase_cca_step4_integration.py`, but targeting the
`/advisory/global/...` endpoints introduced in Batch 39A. The
validators they share are exercised exhaustively in
`test_relation_validation.py` and the client-scoped integration
tests — here we just confirm the global endpoints compose end-to-end
and the global-scope guard (Package.client_id IS NULL) is enforced.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    ConditionalAnswer, ConditionalQuestion, Element, Package,
    PackageStatus, PackageType, Practice, PracticeConditional, PracticeL0,
    Relation, RelationConditional, RelationType, Timeline, TimelineFromType,
)
from app.modules.advisory.router import (
    create_global_conditional_question, create_global_relation,
    delete_global_conditional_question, delete_global_relation,
    link_global_practice_conditional, link_global_relation_conditional,
    list_global_conditional_questions, list_global_relations_for_timeline,
    update_global_conditional_question,
)
from app.modules.advisory.schemas import (
    CQAttachmentIn, CQReplace, ConditionalQuestionCreate,
    PracticeConditionalCreate, RelationCreate,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_package, make_timeline, make_user


async def _seed_global_pkg(db, *, name: str = "GP") -> Package:
    pkg = Package(
        client_id=None,
        name=name,
        crop_cosh_id="crop:tomato",
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.ACTIVE,
    )
    db.add(pkg)
    await db.flush()
    return pkg


async def _seed_practice(
    db, *, timeline: Timeline, l0=PracticeL0.INPUT, l1="PESTICIDE",
    common_name_cosh_id: str | None = None,
) -> Practice:
    p = Practice(
        timeline_id=timeline.id, l0_type=l0, l1_type=l1,
        common_name_cosh_id=common_name_cosh_id,
    )
    db.add(p)
    await db.flush()
    if common_name_cosh_id:
        db.add(Element(
            practice_id=p.id, element_type="COMMON_NAME",
            cosh_ref=common_name_cosh_id, value="",
        ))
        await db.flush()
    return p


async def _setup_global_timeline(db) -> tuple[Package, Timeline, "User"]:
    user = await make_user(db, name="SA")
    pkg = await _seed_global_pkg(db)
    tl = Timeline(
        package_id=pkg.id, name="TL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    db.add(tl)
    await db.flush()
    return pkg, tl, user


# ── Happy paths ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_global_pure_and_relation(db):
    pkg, tl, user = await _setup_global_timeline(db)
    p1 = await _seed_practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:1")
    p2 = await _seed_practice(db, timeline=tl, l1="FERTILIZER", common_name_cosh_id="cn:2")
    await db.commit()

    out = await create_global_relation(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND,
            parts=[[[p1.id, p2.id]]],
        ),
        db=db, current_user=user,
    )
    assert out["relation_type"] == "AND"
    refreshed = (await db.execute(
        select(Practice).where(Practice.id.in_([p1.id, p2.id]))
    )).scalars().all()
    for p in refreshed:
        assert p.relation_id == out["id"]
        assert p.relation_role.startswith("PART_1__OPT_1__POS_")


@requires_docker
@pytest.mark.asyncio
async def test_create_global_pure_or_relation(db):
    pkg, tl, user = await _setup_global_timeline(db)
    p1 = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:1")
    p2 = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:2")
    await db.commit()

    out = await create_global_relation(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.OR,
            parts=[[[p1.id], [p2.id]]],
        ),
        db=db, current_user=user,
    )
    assert out["relation_type"] == "OR"


@requires_docker
@pytest.mark.asyncio
async def test_list_global_relations_reconstructs_parts(db):
    pkg, tl, user = await _setup_global_timeline(db)
    p1 = await _seed_practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:1")
    p2 = await _seed_practice(db, timeline=tl, l1="FERTILIZER", common_name_cosh_id="cn:2")
    await db.commit()
    out = await create_global_relation(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND,
            parts=[[[p1.id, p2.id]]],
            expression="p1 AND p2",
        ),
        db=db, current_user=user,
    )

    listed = await list_global_relations_for_timeline(
        pkg_id=pkg.id, timeline_id=tl.id, db=db, current_user=user,
    )
    assert len(listed) == 1
    rel = listed[0]
    assert rel["id"] == out["id"]
    assert rel["relation_type"] == "AND"
    assert rel["expression"] == "p1 AND p2"
    # parts shape mirrors input
    assert rel["parts"] == [[[p1.id, p2.id]]]
    assert rel["conditional"] is None


# ── Global-scope guard ───────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_global_relation_refuses_client_scoped_package(db):
    """A client-scoped Package must not be reachable via the global URL."""
    client = await make_client(db)
    user = await make_user(db, name="SA")
    pkg = await make_package(db, client, name="ClientP")
    tl = await make_timeline(db, pkg)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_global_relation(
            pkg_id=pkg.id, timeline_id=tl.id,
            request=RelationCreate(
                relation_type=RelationType.AND, parts=[[["x"]]],
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_create_global_relation_refuses_timeline_on_other_package(db):
    """Timeline that doesn't belong to the named Package is rejected."""
    pkg_a, tl_a, user = await _setup_global_timeline(db)
    pkg_b = await _seed_global_pkg(db, name="GP-B")
    tl_b = Timeline(
        package_id=pkg_b.id, name="TLB",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    db.add(tl_b)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_global_relation(
            pkg_id=pkg_a.id, timeline_id=tl_b.id,
            request=RelationCreate(
                relation_type=RelationType.AND, parts=[[["x"]]],
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 404


# ── DELETE ───────────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_delete_global_relation_refuses_when_in_conditional_question(db):
    """Lock rule (2026-05-25): SA-Global Relation delete refuses when
    a Conditional Question still binds it. Replaces the previous
    cascading behaviour — the user dismantles the CQ first."""
    pkg, tl, user = await _setup_global_timeline(db)
    p1 = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:1")
    p2 = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:2")
    await db.commit()
    out = await create_global_relation(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND, parts=[[[p1.id, p2.id]]],
        ),
        db=db, current_user=user,
    )
    q = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Is the field flooded?"),
        db=db, current_user=user,
    )
    await link_global_relation_conditional(
        relation_id=out["id"],
        request=PracticeConditionalCreate(
            practice_id="ignored",
            question_id=q.id, answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=user,
    )

    with pytest.raises(HTTPException) as ei:
        await delete_global_relation(
            relation_id=out["id"], db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "relation_in_conditional_question"
    assert "Is the field flooded?" in ei.value.detail["message"]

    # Relation + binding both still in place.
    assert (await db.execute(
        select(Relation).where(Relation.id == out["id"])
    )).scalar_one_or_none() is not None
    assert (await db.execute(
        select(RelationConditional).where(
            RelationConditional.relation_id == out["id"],
        )
    )).scalar_one_or_none() is not None


@requires_docker
@pytest.mark.asyncio
async def test_delete_global_relation_succeeds_after_cq_removed(db):
    """Happy-path follow-up: delete the CQ first, then the Relation
    deletes cleanly and clears the practice roles."""
    pkg, tl, user = await _setup_global_timeline(db)
    p1 = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:1")
    p2 = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:2")
    await db.commit()
    out = await create_global_relation(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND, parts=[[[p1.id, p2.id]]],
        ),
        db=db, current_user=user,
    )
    q = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Q?"),
        db=db, current_user=user,
    )
    await link_global_relation_conditional(
        relation_id=out["id"],
        request=PracticeConditionalCreate(
            practice_id="ignored",
            question_id=q.id, answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=user,
    )

    # Dismantle the CQ first — its delete cascades through the binding.
    await delete_global_conditional_question(
        question_id=q.id, db=db, current_user=user,
    )
    # Now the Relation is unlocked and deletes cleanly.
    await delete_global_relation(
        relation_id=out["id"], db=db, current_user=user,
    )

    # Practices retained; relation_id + role cleared.
    refreshed = (await db.execute(
        select(Practice).where(Practice.id.in_([p1.id, p2.id]))
    )).scalars().all()
    assert len(refreshed) == 2
    for p in refreshed:
        assert p.relation_id is None
        assert p.relation_role is None
    assert (await db.execute(
        select(Relation).where(Relation.id == out["id"])
    )).scalar_one_or_none() is None


# ── Conditional Questions ────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_and_list_global_conditional_questions(db):
    pkg, tl, user = await _setup_global_timeline(db)
    await db.commit()
    q1 = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Has the monsoon arrived?", display_order=0),
        db=db, current_user=user,
    )
    q2 = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Is the field flooded?", display_order=1),
        db=db, current_user=user,
    )
    listed = await list_global_conditional_questions(
        pkg_id=pkg.id, timeline_id=tl.id, db=db, current_user=user,
    )
    # Batch 39E: list returns enriched dicts with yes/no attachment
    # buckets. With no attachments yet, both sides are None.
    assert [q["id"] for q in listed] == [q1.id, q2.id]
    for q in listed:
        assert q["yes"] is None
        assert q["no"] is None


@requires_docker
@pytest.mark.asyncio
async def test_link_global_practice_conditional(db):
    pkg, tl, user = await _setup_global_timeline(db)
    p = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:1")
    await db.commit()
    q = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Q?"),
        db=db, current_user=user,
    )
    pc = await link_global_practice_conditional(
        practice_id=p.id,
        request=PracticeConditionalCreate(
            practice_id=p.id, question_id=q.id, answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=user,
    )
    assert pc.question_id == q.id
    assert pc.answer == ConditionalAnswer.YES


@requires_docker
@pytest.mark.asyncio
async def test_link_global_relation_conditional_idempotent_on_same_question(db):
    pkg, tl, user = await _setup_global_timeline(db)
    p1 = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:1")
    p2 = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:2")
    await db.commit()
    rel = await create_global_relation(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND, parts=[[[p1.id, p2.id]]],
        ),
        db=db, current_user=user,
    )
    q = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Q?"),
        db=db, current_user=user,
    )
    first = await link_global_relation_conditional(
        relation_id=rel["id"],
        request=PracticeConditionalCreate(
            practice_id="ignored", question_id=q.id, answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=user,
    )
    # Same (relation, question) — different answer should update in place,
    # not error.
    second = await link_global_relation_conditional(
        relation_id=rel["id"],
        request=PracticeConditionalCreate(
            practice_id="ignored", question_id=q.id, answer=ConditionalAnswer.NO,
        ),
        db=db, current_user=user,
    )
    assert first.id == second.id
    assert second.answer == ConditionalAnswer.NO


@requires_docker
@pytest.mark.asyncio
async def test_link_global_practice_conditional_refuses_client_scoped(db):
    """A practice that lives on a client-scoped Package can't be linked
    via the global endpoint."""
    client = await make_client(db)
    user = await make_user(db, name="SA")
    pkg = await make_package(db, client, name="P")
    tl = await make_timeline(db, pkg)
    p = await _seed_practice(db, timeline=tl)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await link_global_practice_conditional(
            practice_id=p.id,
            request=PracticeConditionalCreate(
                practice_id=p.id, question_id="q-doesnt-matter",
                answer=ConditionalAnswer.YES,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 404


# ── Conditional fold-in on list ──────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_folds_in_relation_conditional(db):
    pkg, tl, user = await _setup_global_timeline(db)
    p1 = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:1")
    p2 = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:2")
    await db.commit()
    rel = await create_global_relation(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.IF, parts=[[[p1.id, p2.id]]],
        ),
        db=db, current_user=user,
    )
    q = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Has the monsoon arrived?"),
        db=db, current_user=user,
    )
    await link_global_relation_conditional(
        relation_id=rel["id"],
        request=PracticeConditionalCreate(
            practice_id="ignored", question_id=q.id, answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=user,
    )
    listed = await list_global_relations_for_timeline(
        pkg_id=pkg.id, timeline_id=tl.id, db=db, current_user=user,
    )
    assert len(listed) == 1
    cond = listed[0]["conditional"]
    assert cond is not None
    assert cond["question_id"] == q.id
    assert cond["question_text"] == "Has the monsoon arrived?"
    assert cond["answer"] == "YES"


# ── Batch 39E — CQ list with bundled YES/NO attachments + DELETE ────────────


@requires_docker
@pytest.mark.asyncio
async def test_list_cqs_bundles_yes_and_no_attachments(db):
    """A CQ with a Practice on YES and a Relation on NO surfaces both
    sides on the list endpoint. Either side may be a Practice (Path B)
    or a Relation (Path A)."""
    pkg, tl, user = await _setup_global_timeline(db)
    p_yes = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:y")
    p_a = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:a")
    p_b = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:b")
    await db.commit()
    rel = await create_global_relation(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND, parts=[[[p_a.id, p_b.id]]],
        ),
        db=db, current_user=user,
    )
    q = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Has it rained?"),
        db=db, current_user=user,
    )
    # YES side → independent Practice (Path B).
    await link_global_practice_conditional(
        practice_id=p_yes.id,
        request=PracticeConditionalCreate(
            practice_id=p_yes.id, question_id=q.id, answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=user,
    )
    # NO side → Relation (Path A).
    await link_global_relation_conditional(
        relation_id=rel["id"],
        request=PracticeConditionalCreate(
            practice_id="ignored", question_id=q.id, answer=ConditionalAnswer.NO,
        ),
        db=db, current_user=user,
    )
    listed = await list_global_conditional_questions(
        pkg_id=pkg.id, timeline_id=tl.id, db=db, current_user=user,
    )
    assert len(listed) == 1
    row = listed[0]
    assert row["question_text"] == "Has it rained?"
    assert row["yes"] == {"kind": "practice", "id": p_yes.id}
    assert row["no"]  == {"kind": "relation", "id": rel["id"]}


@requires_docker
@pytest.mark.asyncio
async def test_delete_global_cq_clears_attachments(db):
    """Dropping a CQ removes every PracticeConditional and
    RelationConditional bound to it. The practices/relations
    themselves remain — only the gating goes."""
    pkg, tl, user = await _setup_global_timeline(db)
    p = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:p")
    p_a = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:a")
    p_b = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:b")
    await db.commit()
    rel = await create_global_relation(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND, parts=[[[p_a.id, p_b.id]]],
        ),
        db=db, current_user=user,
    )
    q = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Q?"),
        db=db, current_user=user,
    )
    await link_global_practice_conditional(
        practice_id=p.id,
        request=PracticeConditionalCreate(
            practice_id=p.id, question_id=q.id, answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=user,
    )
    await link_global_relation_conditional(
        relation_id=rel["id"],
        request=PracticeConditionalCreate(
            practice_id="ignored", question_id=q.id, answer=ConditionalAnswer.NO,
        ),
        db=db, current_user=user,
    )

    await delete_global_conditional_question(
        question_id=q.id, db=db, current_user=user,
    )
    # CQ gone.
    cq = (await db.execute(
        select(ConditionalQuestion).where(ConditionalQuestion.id == q.id)
    )).scalar_one_or_none()
    assert cq is None
    # PC + RC rows gone.
    pcs = (await db.execute(
        select(PracticeConditional).where(PracticeConditional.question_id == q.id)
    )).scalars().all()
    rcs = (await db.execute(
        select(RelationConditional).where(RelationConditional.question_id == q.id)
    )).scalars().all()
    assert pcs == []
    assert rcs == []
    # Practices/Relation still alive.
    p_still = (await db.execute(
        select(Practice).where(Practice.id == p.id)
    )).scalar_one_or_none()
    assert p_still is not None
    rel_still = (await db.execute(
        select(Relation).where(Relation.id == rel["id"])
    )).scalar_one_or_none()
    assert rel_still is not None


@requires_docker
@pytest.mark.asyncio
async def test_delete_global_cq_refuses_client_scoped(db):
    """A CQ on a client-scoped Timeline can't be deleted via the
    global URL."""
    from tests.factories import make_client, make_package, make_timeline
    client = await make_client(db)
    user = await make_user(db, name="SA")
    pkg = await make_package(db, client, name="ClientP")
    tl = await make_timeline(db, pkg)
    cq = ConditionalQuestion(timeline_id=tl.id, question_text="Q?")
    db.add(cq)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await delete_global_conditional_question(
            question_id=cq.id, db=db, current_user=user,
        )
    assert exc.value.status_code == 404


# ── Batch 39G — Edit Conditional Question ──────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_update_cq_replaces_question_text_and_attachments(db):
    """PUT updates question text and re-binds the YES/NO sides
    atomically. Switching attachments on the same CQ is allowed
    because the existing rows are cleared first."""
    pkg, tl, user = await _setup_global_timeline(db)
    p1 = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:1")
    p2 = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:2")
    await db.commit()
    q = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Has it rained?"),
        db=db, current_user=user,
    )
    # Initial state: P1 on YES.
    await link_global_practice_conditional(
        practice_id=p1.id,
        request=PracticeConditionalCreate(
            practice_id=p1.id, question_id=q.id, answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=user,
    )

    # Edit: rename + swap YES from P1 to P2, add P1 to NO.
    out = await update_global_conditional_question(
        question_id=q.id,
        request=CQReplace(
            question_text="Has it rained in the last 3 days?",
            yes=CQAttachmentIn(kind="practice", id=p2.id),
            no=CQAttachmentIn(kind="practice", id=p1.id),
        ),
        db=db, current_user=user,
    )
    assert out["question_text"] == "Has it rained in the last 3 days?"

    listed = await list_global_conditional_questions(
        pkg_id=pkg.id, timeline_id=tl.id, db=db, current_user=user,
    )
    row = listed[0]
    assert row["question_text"] == "Has it rained in the last 3 days?"
    assert row["yes"] == {"kind": "practice", "id": p2.id}
    assert row["no"]  == {"kind": "practice", "id": p1.id}


@requires_docker
@pytest.mark.asyncio
async def test_update_cq_can_unbind_one_side(db):
    """Passing yes=None / no=None clears that side."""
    pkg, tl, user = await _setup_global_timeline(db)
    p = await _seed_practice(db, timeline=tl, common_name_cosh_id="cn:1")
    await db.commit()
    q = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Q?"),
        db=db, current_user=user,
    )
    await link_global_practice_conditional(
        practice_id=p.id,
        request=PracticeConditionalCreate(
            practice_id=p.id, question_id=q.id, answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=user,
    )

    # Clear YES, keep NO unbound (also originally unbound).
    await update_global_conditional_question(
        question_id=q.id,
        request=CQReplace(question_text="Q?", yes=None, no=None),
        db=db, current_user=user,
    )
    listed = await list_global_conditional_questions(
        pkg_id=pkg.id, timeline_id=tl.id, db=db, current_user=user,
    )
    assert listed[0]["yes"] is None
    assert listed[0]["no"]  is None


@requires_docker
@pytest.mark.asyncio
async def test_update_cq_refuses_client_scoped(db):
    """The global PUT can't reach a client-scoped CQ."""
    from tests.factories import make_client, make_package, make_timeline
    client = await make_client(db)
    user = await make_user(db, name="SA")
    pkg = await make_package(db, client, name="ClientP")
    tl = await make_timeline(db, pkg)
    cq = ConditionalQuestion(timeline_id=tl.id, question_text="Q?")
    db.add(cq)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await update_global_conditional_question(
            question_id=cq.id,
            request=CQReplace(question_text="new"),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_update_cq_blank_question_text_422(db):
    pkg, tl, user = await _setup_global_timeline(db)
    await db.commit()
    q = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Q?"),
        db=db, current_user=user,
    )
    with pytest.raises(HTTPException) as exc:
        await update_global_conditional_question(
            question_id=q.id,
            request=CQReplace(question_text="   "),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "cq_question_text_required"
