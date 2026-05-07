"""CCA Step 4 / Batch 4E — publish-time conditional-question gate.

Verifies that `assert_package_publish_ready` blocks a publish when
any ConditionalQuestion under the package's timelines is dangling
(no PracticeConditional and no RelationConditional pointing at it).

Coexists cleanly with the existing 2C publish-readiness checks: the
route returns one consolidated 422 listing every reason at once.
"""
from __future__ import annotations

import pytest

from app.modules.advisory.models import (
    ConditionalAnswer, ConditionalQuestion, PracticeConditional, PracticeL0,
    RelationConditional, RelationType, Relation, TimelineFromType,
)
from app.modules.advisory.router import create_relation
from app.modules.advisory.schemas import RelationCreate
from app.services.publish_validation import (
    PublishBlockedError, assert_package_publish_ready,
    find_dangling_conditional_questions,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_crop_reference, make_package, make_timeline, make_user,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _setup(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await make_crop_reference(db, "crop:test", name="Test Crop")
    pkg = await make_package(db, client, name="P", crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, name="TL",
                             from_type=TimelineFromType.DAS,
                             from_value=0, to_value=15)
    return client, user, pkg, tl


async def _question(db, timeline, text="Did it rain?"):
    q = ConditionalQuestion(
        timeline_id=timeline.id, question_text=text, display_order=0,
    )
    db.add(q)
    await db.flush()
    return q


# ── find_dangling_conditional_questions ─────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_no_questions_means_no_dangling(db):
    client, user, pkg, tl = await _setup(db)
    await db.commit()
    out = await find_dangling_conditional_questions(db, package_id=pkg.id)
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_unlinked_question_is_dangling(db):
    client, user, pkg, tl = await _setup(db)
    q = await _question(db, tl, text="Has the field been ploughed?")
    await db.commit()

    out = await find_dangling_conditional_questions(db, package_id=pkg.id)
    assert len(out) == 1
    assert out[0].code == "conditional_question_no_links"
    assert out[0].extra["question_id"] == q.id
    assert out[0].extra["question_text"] == "Has the field been ploughed?"


@requires_docker
@pytest.mark.asyncio
async def test_question_with_practice_link_not_dangling(db):
    """A YES or NO link on a single PracticeConditional is enough."""
    from app.modules.advisory.models import Practice
    client, user, pkg, tl = await _setup(db)
    p = Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
        common_name_cosh_id="cn:1",
    )
    db.add(p)
    await db.flush()

    q = await _question(db, tl)
    db.add(PracticeConditional(
        practice_id=p.id, question_id=q.id, answer=ConditionalAnswer.YES,
    ))
    await db.commit()

    out = await find_dangling_conditional_questions(db, package_id=pkg.id)
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_question_with_relation_link_not_dangling(db):
    """A RelationConditional link on the question also satisfies the gate."""
    from app.modules.advisory.models import Practice
    client, user, pkg, tl = await _setup(db)
    p1 = Practice(timeline_id=tl.id, l0_type=PracticeL0.INPUT,
                  l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
                  common_name_cosh_id="cn:1")
    p2 = Practice(timeline_id=tl.id, l0_type=PracticeL0.INPUT,
                  l1_type="FERTILIZER", l2_type="MANURES",
                  common_name_cosh_id="cn:2")
    db.add_all([p1, p2])
    await db.flush()

    rel = Relation(timeline_id=tl.id, relation_type=RelationType.AND,
                   expression="p1 AND p2")
    db.add(rel)
    await db.flush()

    q = await _question(db, tl)
    db.add(RelationConditional(
        relation_id=rel.id, question_id=q.id, answer=ConditionalAnswer.NO,
    ))
    await db.commit()

    out = await find_dangling_conditional_questions(db, package_id=pkg.id)
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_mixed_some_linked_some_dangling(db):
    """Two questions; one has a link, the other doesn't. Only the
    dangling one shows up."""
    from app.modules.advisory.models import Practice
    client, user, pkg, tl = await _setup(db)
    p = Practice(timeline_id=tl.id, l0_type=PracticeL0.INPUT,
                 l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
                 common_name_cosh_id="cn:1")
    db.add(p)
    await db.flush()

    linked = await _question(db, tl, text="Linked question")
    dangling = await _question(db, tl, text="Dangling question")
    db.add(PracticeConditional(
        practice_id=p.id, question_id=linked.id,
        answer=ConditionalAnswer.YES,
    ))
    await db.commit()

    out = await find_dangling_conditional_questions(db, package_id=pkg.id)
    assert len(out) == 1
    assert out[0].extra["question_id"] == dangling.id


@requires_docker
@pytest.mark.asyncio
async def test_question_in_other_package_not_reported(db):
    """The gate is package-scoped: a dangling question in a different
    package doesn't pollute this package's missing list."""
    client, user, pkg_a, tl_a = await _setup(db)
    pkg_b = await make_package(db, client, name="OtherPoP",
                               crop_cosh_id="crop:test")
    tl_b = await make_timeline(db, pkg_b, name="TL-B",
                               from_type=TimelineFromType.DAS,
                               from_value=0, to_value=15)
    await _question(db, tl_b, text="dangling but in package B")
    await db.commit()

    out = await find_dangling_conditional_questions(db, package_id=pkg_a.id)
    assert out == []


# ── End-to-end via assert_package_publish_ready ─────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_publish_blocked_by_dangling_question(db):
    client, user, pkg, tl = await _setup(db)
    await _question(db, tl, text="Did it rain in the last 2 days?")
    await db.commit()

    with pytest.raises(PublishBlockedError) as ei:
        await assert_package_publish_ready(db, package=pkg)

    codes = {m.code for m in ei.value.missing}
    assert "conditional_question_no_links" in codes


@requires_docker
@pytest.mark.asyncio
async def test_publish_passes_when_all_questions_linked(db):
    """Happy path: a fully-linked package publishes without the
    conditional-question gate firing. Other 2C requirements (locations,
    authors, etc.) are already satisfied by the make_package factory."""
    from app.modules.advisory.models import Practice
    client, user, pkg, tl = await _setup(db)
    p = Practice(timeline_id=tl.id, l0_type=PracticeL0.INPUT,
                 l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
                 common_name_cosh_id="cn:1")
    db.add(p)
    await db.flush()
    q = await _question(db, tl)
    db.add(PracticeConditional(
        practice_id=p.id, question_id=q.id, answer=ConditionalAnswer.YES,
    ))
    await db.commit()

    # Should not raise. Other 2C checks (locations, authors, has_pv)
    # are satisfied by the make_package factory's defaults.
    await assert_package_publish_ready(db, package=pkg)
