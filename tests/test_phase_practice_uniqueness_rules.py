"""Rule 1 (Common Name unique per Timeline, PESTICIDE+FERTILIZER) and
Rule 3 (Practice/Relation linked to at most one CQ, regardless of
branch) — locked down 2026-05-22.

Rule 1 ships as an app-layer validator wired into every practice
add/update endpoint across SA-CCA, SA-PG, CA-CCA, CA-PG, CA-SP, CA-QA.
Rule 3 ships as DB UniqueConstraints on practice_conditionals.practice_id
and relation_conditionals.relation_id. The app-layer rejection code
already existed (`practice_already_in_conditional`); the new
constraints make the invariant structural so a future bug can't
slip a duplicate through.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.modules.advisory.models import (
    ConditionalAnswer, Element, Practice, PracticeConditional,
    RelationConditional,
)
from app.modules.advisory.router import (
    _assert_no_duplicate_common_name_in_timeline,
)
from app.modules.advisory.schemas import ElementIn
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_conditional_question, make_element, make_package,
    make_practice, make_relation, make_timeline,
)


# ── Rule 1: Common Name uniqueness within Timeline ────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_cn_duplicate_pesticide_same_timeline_rejected(db):
    client = await make_client(db)
    pkg = await make_package(db, client)
    tl = await make_timeline(db, pkg)
    p1 = await make_practice(db, tl, l1="PESTICIDE", l2="CONTACT_FUNGICIDE")
    await make_element(db, p1, element_type="COMMON_NAME", cosh_ref="cn:mancozeb", value=None)
    await db.commit()

    elements = [ElementIn(element_type="COMMON_NAME", cosh_ref="cn:mancozeb")]
    with pytest.raises(HTTPException) as exc:
        await _assert_no_duplicate_common_name_in_timeline(
            db, timeline_id=tl.id, l1_type="PESTICIDE", elements=elements,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "common_name_duplicate_in_timeline"
    assert exc.value.detail["l1_type"] == "PESTICIDE"
    assert exc.value.detail["existing_practice_id"] == p1.id


@requires_docker
@pytest.mark.asyncio
async def test_cn_duplicate_fertilizer_same_timeline_rejected(db):
    client = await make_client(db)
    pkg = await make_package(db, client)
    tl = await make_timeline(db, pkg)
    p1 = await make_practice(db, tl, l1="FERTILIZER", l2="UREA")
    await make_element(db, p1, element_type="COMMON_NAME", cosh_ref="cn:urea")
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await _assert_no_duplicate_common_name_in_timeline(
            db, timeline_id=tl.id, l1_type="FERTILIZER",
            elements=[ElementIn(element_type="COMMON_NAME", cosh_ref="cn:urea")],
        )
    assert exc.value.detail["code"] == "common_name_duplicate_in_timeline"


@requires_docker
@pytest.mark.asyncio
async def test_cn_duplicate_non_input_l1_allowed(db):
    """Rule 1 only applies to PESTICIDE and FERTILIZER. Other L1
    types (HOST, NON_INPUT, etc.) are exempt — pass through."""
    client = await make_client(db)
    pkg = await make_package(db, client)
    tl = await make_timeline(db, pkg)
    p1 = await make_practice(db, tl, l0="NON_INPUT", l1="ITKS", l2="ITKS")
    await make_element(db, p1, element_type="COMMON_NAME", cosh_ref="cn:dummy")
    await db.commit()

    # No exception — non-PEST/FERT L1 types are exempt.
    await _assert_no_duplicate_common_name_in_timeline(
        db, timeline_id=tl.id, l1_type="ITKS",
        elements=[ElementIn(element_type="COMMON_NAME", cosh_ref="cn:dummy")],
    )


@requires_docker
@pytest.mark.asyncio
async def test_cn_duplicate_across_timelines_allowed(db):
    """Same Common Name CAN repeat across timelines of the same Package."""
    client = await make_client(db)
    pkg = await make_package(db, client)
    tl_a = await make_timeline(db, pkg, name="Sowing")
    tl_b = await make_timeline(db, pkg, name="Vegetative")
    p1 = await make_practice(db, tl_a, l1="PESTICIDE", l2="CONTACT_FUNGICIDE")
    await make_element(db, p1, element_type="COMMON_NAME", cosh_ref="cn:mancozeb")
    await db.commit()

    # Adding the same CN to a different Timeline must NOT raise.
    await _assert_no_duplicate_common_name_in_timeline(
        db, timeline_id=tl_b.id, l1_type="PESTICIDE",
        elements=[ElementIn(element_type="COMMON_NAME", cosh_ref="cn:mancozeb")],
    )


@requires_docker
@pytest.mark.asyncio
async def test_cn_duplicate_exclude_self_on_update(db):
    """Editing a Practice in place (its own CN unchanged) must NOT
    flag itself. The helper takes exclude_practice_id for this."""
    client = await make_client(db)
    pkg = await make_package(db, client)
    tl = await make_timeline(db, pkg)
    p1 = await make_practice(db, tl, l1="PESTICIDE", l2="CONTACT_FUNGICIDE")
    await make_element(db, p1, element_type="COMMON_NAME", cosh_ref="cn:copper")
    await db.commit()

    await _assert_no_duplicate_common_name_in_timeline(
        db, timeline_id=tl.id, l1_type="PESTICIDE",
        elements=[ElementIn(element_type="COMMON_NAME", cosh_ref="cn:copper")],
        exclude_practice_id=p1.id,
    )


@requires_docker
@pytest.mark.asyncio
async def test_cn_missing_element_passes(db):
    """A Practice with no COMMON_NAME element can't conflict — pass."""
    client = await make_client(db)
    pkg = await make_package(db, client)
    tl = await make_timeline(db, pkg)
    await _assert_no_duplicate_common_name_in_timeline(
        db, timeline_id=tl.id, l1_type="PESTICIDE",
        elements=[ElementIn(element_type="DOSAGE", value="50")],
    )


@requires_docker
@pytest.mark.asyncio
async def test_cn_different_l1_same_cn_same_timeline_allowed(db):
    """Cross-L1 duplicate is allowed — PESTICIDE+FERTILIZER scope is
    distinct per L1. A pesticide and a fertilizer named the same CN
    (rare but legal) won't collide."""
    client = await make_client(db)
    pkg = await make_package(db, client)
    tl = await make_timeline(db, pkg)
    p_pest = await make_practice(db, tl, l1="PESTICIDE", l2="CONTACT_FUNGICIDE")
    await make_element(db, p_pest, element_type="COMMON_NAME", cosh_ref="cn:dual")
    await db.commit()

    # Same CN, different L1 — must NOT raise.
    await _assert_no_duplicate_common_name_in_timeline(
        db, timeline_id=tl.id, l1_type="FERTILIZER",
        elements=[ElementIn(element_type="COMMON_NAME", cosh_ref="cn:dual")],
    )


# ── Rule 3: PracticeConditional / RelationConditional uniqueness ──────────

@requires_docker
@pytest.mark.asyncio
async def test_practice_conditional_db_constraint_blocks_second_link(db):
    """Two PracticeConditional rows for the same practice (different
    CQs OR same CQ different answer) must trip the new DB constraint."""
    client = await make_client(db)
    pkg = await make_package(db, client)
    tl = await make_timeline(db, pkg)
    p = await make_practice(db, tl)
    q1 = await make_conditional_question(db, tl, text="Q1")
    q2 = await make_conditional_question(db, tl, text="Q2")
    db.add(PracticeConditional(
        practice_id=p.id, question_id=q1.id, answer=ConditionalAnswer.YES,
    ))
    await db.commit()

    # Second row for the SAME practice but DIFFERENT CQ — must fail.
    db.add(PracticeConditional(
        practice_id=p.id, question_id=q2.id, answer=ConditionalAnswer.YES,
    ))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@requires_docker
@pytest.mark.asyncio
async def test_practice_conditional_db_constraint_blocks_yes_plus_no(db):
    """Same Practice + same CQ + opposite answer (YES then NO) used
    to slip through (no DB constraint). New uq blocks it."""
    client = await make_client(db)
    pkg = await make_package(db, client)
    tl = await make_timeline(db, pkg)
    p = await make_practice(db, tl)
    q = await make_conditional_question(db, tl)
    db.add(PracticeConditional(
        practice_id=p.id, question_id=q.id, answer=ConditionalAnswer.YES,
    ))
    await db.commit()

    db.add(PracticeConditional(
        practice_id=p.id, question_id=q.id, answer=ConditionalAnswer.NO,
    ))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@requires_docker
@pytest.mark.asyncio
async def test_practice_conditional_different_practices_allowed(db):
    """Two different Practices on the same CQ stays legal — the
    constraint is per practice_id, not per question_id."""
    client = await make_client(db)
    pkg = await make_package(db, client)
    tl = await make_timeline(db, pkg)
    p_a = await make_practice(db, tl, display_order=0)
    p_b = await make_practice(db, tl, display_order=1)
    q = await make_conditional_question(db, tl)
    db.add(PracticeConditional(
        practice_id=p_a.id, question_id=q.id, answer=ConditionalAnswer.YES,
    ))
    db.add(PracticeConditional(
        practice_id=p_b.id, question_id=q.id, answer=ConditionalAnswer.NO,
    ))
    await db.commit()  # both should land cleanly


@requires_docker
@pytest.mark.asyncio
async def test_relation_conditional_db_constraint_blocks_second_link(db):
    """RelationConditional mirror of the Practice constraint —
    a Relation can be on at most one CQ."""
    client = await make_client(db)
    pkg = await make_package(db, client)
    tl = await make_timeline(db, pkg)
    rel = await make_relation(db, tl)
    q1 = await make_conditional_question(db, tl, text="Q1")
    q2 = await make_conditional_question(db, tl, text="Q2")
    db.add(RelationConditional(
        relation_id=rel.id, question_id=q1.id, answer=ConditionalAnswer.YES,
    ))
    await db.commit()

    db.add(RelationConditional(
        relation_id=rel.id, question_id=q2.id, answer=ConditionalAnswer.YES,
    ))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@requires_docker
@pytest.mark.asyncio
async def test_relation_conditional_blocks_yes_plus_no_same_cq(db):
    """Old constraint only covered (relation_id, question_id) pair,
    so YES + NO for the same (rel, cq) used to slip. New constraint
    on relation_id alone blocks any second row."""
    client = await make_client(db)
    pkg = await make_package(db, client)
    tl = await make_timeline(db, pkg)
    rel = await make_relation(db, tl)
    q = await make_conditional_question(db, tl)
    db.add(RelationConditional(
        relation_id=rel.id, question_id=q.id, answer=ConditionalAnswer.YES,
    ))
    await db.commit()

    db.add(RelationConditional(
        relation_id=rel.id, question_id=q.id, answer=ConditionalAnswer.NO,
    ))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
