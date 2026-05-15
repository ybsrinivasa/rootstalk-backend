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
    delete_global_relation, link_global_practice_conditional,
    link_global_relation_conditional, list_global_conditional_questions,
    list_global_relations_for_timeline,
)
from app.modules.advisory.schemas import (
    ConditionalQuestionCreate, PracticeConditionalCreate, RelationCreate,
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
async def test_delete_global_relation_clears_role_and_conditional(db):
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
    # Bind a CQ to the relation.
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
    # Relation gone.
    rel = (await db.execute(
        select(Relation).where(Relation.id == out["id"])
    )).scalar_one_or_none()
    assert rel is None
    # RelationConditional gone.
    rc = (await db.execute(
        select(RelationConditional).where(
            RelationConditional.relation_id == out["id"],
        )
    )).scalar_one_or_none()
    assert rc is None


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
    assert [q.id for q in listed] == [q1.id, q2.id]


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
