"""Orders V2 Batch 22 — BL-02 conditional-answer filtering on the bundle.

The 2026-05-31 BL audit: practices the farmer answered "NO" to
were still flowing into compute_bundle and the DBS resolver.
The advisory walk filters them via BL-02 step 9; the bundle now
does the same.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.modules.advisory.models import (
    ConditionalAnswer as CAEnum, PracticeL0, TimelineFromType,
)
from app.modules.subscriptions.models import ConditionalAnswer
from app.services.order_bundle import (
    CATEGORY_PESTICIDE, compute_bundle,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_conditional_question, make_element, make_package,
    make_practice, make_practice_conditional, make_subscription,
    make_timeline, make_user,
)


async def _sub_with_one_conditional_practice(db, *, link_answer=CAEnum.YES):
    user = await make_user(db, name="Farmer Cond")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=2)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_C",
        from_type=TimelineFromType.DAS, from_value=0, to_value=20,
    )
    p_uncond = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_uncond, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:mancozeb")

    p_cond = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p_cond, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:carbendazim")

    q = await make_conditional_question(db, tl, text="Is rainfall expected?")
    await make_practice_conditional(db, p_cond, q, answer=link_answer)
    await db.commit()
    return user, sub, q, p_uncond, p_cond


async def _record_answer(db, sub, q, answer_str):
    db.add(ConditionalAnswer(
        subscription_id=sub.id, question_id=q.id,
        answer_date=date.today(), answer=answer_str,
    ))
    await db.commit()


@requires_docker
@pytest.mark.asyncio
async def test_unconditional_practice_always_in_bundle(db):
    _, sub, _, p_uncond, p_cond = await _sub_with_one_conditional_practice(db)
    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=30), today=date.today(),
    )
    ids = {row["id"] for row in bundle["practices"]}
    assert p_uncond.id in ids


@requires_docker
@pytest.mark.asyncio
async def test_conditional_practice_excluded_until_answered(db):
    _, sub, _, _, p_cond = await _sub_with_one_conditional_practice(db)
    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=30), today=date.today(),
    )
    ids = {row["id"] for row in bundle["practices"]}
    # No answer recorded → blank path → suppressed (BL-02 step 12).
    assert p_cond.id not in ids


@requires_docker
@pytest.mark.asyncio
async def test_conditional_practice_included_when_yes_matches(db):
    _, sub, q, _, p_cond = await _sub_with_one_conditional_practice(db, link_answer=CAEnum.YES)
    await _record_answer(db, sub, q, "YES")
    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=30), today=date.today(),
    )
    ids = {row["id"] for row in bundle["practices"]}
    assert p_cond.id in ids


@requires_docker
@pytest.mark.asyncio
async def test_conditional_practice_excluded_when_answer_does_not_match(db):
    _, sub, q, _, p_cond = await _sub_with_one_conditional_practice(db, link_answer=CAEnum.YES)
    await _record_answer(db, sub, q, "NO")
    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=30), today=date.today(),
    )
    ids = {row["id"] for row in bundle["practices"]}
    assert p_cond.id not in ids


@requires_docker
@pytest.mark.asyncio
async def test_both_answer_means_always_in_bundle(db):
    _, sub, q, _, p_cond = await _sub_with_one_conditional_practice(db, link_answer=CAEnum.BOTH)
    # No answer recorded — but BOTH means show regardless.
    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=date.today() + timedelta(days=30), today=date.today(),
    )
    ids = {row["id"] for row in bundle["practices"]}
    assert p_cond.id in ids
