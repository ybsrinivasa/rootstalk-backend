"""Regression guards for the CA-side CCA Practice delete path.

Two clusters of bugs landed here on 2026-05-25:

1. **FK violation on Practice with Elements.** Element.practice_id is
   NOT NULL with no ON DELETE CASCADE. The old handler called
   db.delete(practice) and tripped a FK violation as soon as any
   Element existed. Frontend had no try/catch so the user saw
   "nothing happens." Fixed by explicit Element delete first, matching
   the SA-Global / CA-PG / CA-SP / CA-QA pattern.

2. **Refuse-if-locked rule (user-stated 2026-05-25).** A Practice
   referenced by a Relation or by a Conditional Question must not be
   deleted silently — the user dismantles the containing construct
   first. Returns 422 with `practice_in_relation` or
   `practice_in_conditional_question` and the blocker's friendly
   label so the alert is useful.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    ConditionalAnswer, ConditionalQuestion, Element, Practice,
    PracticeConditional, PracticeL0, Relation, RelationType,
    TimelineFromType,
)
from app.modules.advisory.router import (
    create_relation, delete_practice, link_practice_conditional,
)
from app.modules.advisory.schemas import (
    PracticeConditionalCreate, RelationCreate,
)
from app.modules.clients.models import ClientUserRole
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_package, make_timeline, make_user,
)


async def _seed_practice(db, *, timeline, name="urea", order=0):
    p = Practice(
        timeline_id=timeline.id,
        l0_type=PracticeL0.INPUT,
        l1_type="FERTILIZER",
        l2_type="CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS",
        display_order=order,
        common_name_cosh_id=f"cn:{name}",
    )
    db.add(p)
    await db.flush()
    db.add(Element(
        practice_id=p.id, element_type="COMMON_NAME",
        cosh_ref=f"cn:{name}", display_order=0,
    ))
    return p


async def _se_for(db, *, client):
    user = await make_user(db, name=f"SE-{client.short_name}", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    return user


@requires_docker
@pytest.mark.asyncio
async def test_cca_delete_practice_drops_elements(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    practice = await _seed_practice(db, timeline=tl)
    db.add(Element(
        practice_id=practice.id, element_type="DOSAGE",
        value="2", unit_cosh_id="kg/ha", display_order=1,
    ))
    await db.commit()
    practice_id = practice.id

    await delete_practice(
        client_id=client.id, timeline_id=tl.id, practice_id=practice_id,
        db=db, current_user=se,
    )

    assert (await db.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one_or_none() is None
    assert (await db.execute(
        select(Element).where(Element.practice_id == practice_id)
    )).scalars().all() == []


# ── Refuse-if-locked rule (2026-05-25) ──────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_cca_delete_practice_refuses_when_in_relation(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    p1 = await _seed_practice(db, timeline=tl, name="urea", order=0)
    p2 = await _seed_practice(db, timeline=tl, name="dap", order=1)
    await db.commit()

    rel = await create_relation(
        client_id=client.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND,
            expression="Urea + DAP",
            parts=[[[p1.id]], [[p2.id]]],
        ),
        db=db, current_user=se,
    )

    with pytest.raises(HTTPException) as ei:
        await delete_practice(
            client_id=client.id, timeline_id=tl.id, practice_id=p1.id,
            db=db, current_user=se,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "practice_in_relation"
    # Friendly label flows through to the message so the user sees
    # which Relation is blocking the delete.
    assert "Urea + DAP" in ei.value.detail["message"]
    assert ei.value.detail["relation_id"] == rel["id"]

    # Practice still exists.
    assert (await db.execute(
        select(Practice).where(Practice.id == p1.id)
    )).scalar_one_or_none() is not None


@requires_docker
@pytest.mark.asyncio
async def test_cca_delete_practice_refuses_when_in_conditional_question(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    practice = await _seed_practice(db, timeline=tl)
    cq = ConditionalQuestion(
        timeline_id=tl.id, question_text="Is the soil sandy?",
    )
    db.add(cq)
    await db.flush()
    await db.commit()

    await link_practice_conditional(
        client_id=client.id, practice_id=practice.id,
        request=PracticeConditionalCreate(
            practice_id=practice.id, question_id=cq.id,
            answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=se,
    )

    with pytest.raises(HTTPException) as ei:
        await delete_practice(
            client_id=client.id, timeline_id=tl.id, practice_id=practice.id,
            db=db, current_user=se,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "practice_in_conditional_question"
    assert "Is the soil sandy?" in ei.value.detail["message"]
    assert ei.value.detail["answer_side"] == "YES"

    # Practice still exists.
    assert (await db.execute(
        select(Practice).where(Practice.id == practice.id)
    )).scalar_one_or_none() is not None


@requires_docker
@pytest.mark.asyncio
async def test_cca_delete_practice_succeeds_after_relation_removed(db):
    """Happy-path follow-up to the lock test: dismantle the Relation,
    then the Practice deletes cleanly."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    p1 = await _seed_practice(db, timeline=tl, name="urea", order=0)
    p2 = await _seed_practice(db, timeline=tl, name="dap", order=1)
    await db.commit()

    rel = await create_relation(
        client_id=client.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND,
            expression="U + D",
            parts=[[[p1.id]], [[p2.id]]],
        ),
        db=db, current_user=se,
    )

    from app.modules.advisory.router import delete_client_relation
    await delete_client_relation(
        client_id=client.id, relation_id=rel["id"],
        db=db, current_user=se,
    )

    # Now the Practice is unlocked and deletes cleanly.
    await delete_practice(
        client_id=client.id, timeline_id=tl.id, practice_id=p1.id,
        db=db, current_user=se,
    )
    assert (await db.execute(
        select(Practice).where(Practice.id == p1.id)
    )).scalar_one_or_none() is None
