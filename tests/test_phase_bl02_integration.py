"""BL-02 — integration tests for the live conditional flow.

Pure-function coverage lives in `tests/test_bl02.py` (9 tests).
This file verifies the wiring: submit endpoint stores the answer
correctly + the today-render path picks it up + ownership is gated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    ConditionalAnswer as ConditionalAnswerEnum, PracticeL0, RelationType,
    TimelineFromType,
)
from app.modules.subscriptions.models import ConditionalAnswer
from app.modules.subscriptions.router import (
    ConditionalAnswerRequest, get_today_advisory, submit_conditional_answer,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_conditional_question, make_element, make_package,
    make_practice, make_practice_conditional, make_subscription, make_timeline,
    make_user,
)


async def _seed_conditional_setup(db, *, day_offset: int = 5):
    """User + sub + DAS timeline 0..30 + 2 practices: one always-shown,
    one conditional on YES to a question. Returns the seeded objects."""
    user = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=package,
    )
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=day_offset)
    await db.commit()

    tl = await make_timeline(
        db, package, name="TL_BL02",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p_always = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2="UREA",
    )
    await make_element(db, p_always, value="50", unit_cosh_id="kg_per_acre")
    p_conditional = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="MANCOZEB",
        display_order=1,
    )
    await make_element(db, p_conditional, value="2", unit_cosh_id="kg_per_acre")
    q = await make_conditional_question(db, tl, text="Is rainfall expected?")
    await make_practice_conditional(
        db, p_conditional, q, answer=ConditionalAnswerEnum.YES,
    )
    await db.commit()

    return {
        "user": user, "sub": sub, "tl": tl,
        "p_always": p_always, "p_conditional": p_conditional, "q": q,
    }


# ── Submit endpoint — happy path + ownership ───────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_submit_conditional_answer_stores_today(db):
    s = await _seed_conditional_setup(db)
    out = await submit_conditional_answer(
        request=ConditionalAnswerRequest(
            subscription_id=s["sub"].id,
            question_id=s["q"].id,
            answer="YES",
        ),
        db=db, current_user=s["user"],
    )
    assert out["answer"] == "YES"

    rows = (await db.execute(
        select(ConditionalAnswer).where(
            ConditionalAnswer.subscription_id == s["sub"].id,
            ConditionalAnswer.question_id == s["q"].id,
        )
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].answer == "YES"


@requires_docker
@pytest.mark.asyncio
async def test_submit_replaces_today_answer_idempotent(db):
    """Second submit for same (sub, question, today) overwrites — no
    duplicate row."""
    s = await _seed_conditional_setup(db)
    await submit_conditional_answer(
        request=ConditionalAnswerRequest(
            subscription_id=s["sub"].id, question_id=s["q"].id, answer="YES",
        ),
        db=db, current_user=s["user"],
    )
    await submit_conditional_answer(
        request=ConditionalAnswerRequest(
            subscription_id=s["sub"].id, question_id=s["q"].id, answer="NO",
        ),
        db=db, current_user=s["user"],
    )
    rows = (await db.execute(
        select(ConditionalAnswer).where(
            ConditionalAnswer.subscription_id == s["sub"].id,
            ConditionalAnswer.question_id == s["q"].id,
        )
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].answer == "NO"


@requires_docker
@pytest.mark.asyncio
async def test_submit_rejects_invalid_answer_value(db):
    s = await _seed_conditional_setup(db)
    with pytest.raises(HTTPException) as exc:
        await submit_conditional_answer(
            request=ConditionalAnswerRequest(
                subscription_id=s["sub"].id, question_id=s["q"].id,
                answer="MAYBE",
            ),
            db=db, current_user=s["user"],
        )
    assert exc.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_submit_rejects_other_farmers_subscription(db):
    """Ownership gate — farmer A cannot submit answers for farmer B's
    subscription. Returns 404 (doesn't leak the existence of the row)."""
    s = await _seed_conditional_setup(db)
    intruder = await make_user(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await submit_conditional_answer(
            request=ConditionalAnswerRequest(
                subscription_id=s["sub"].id, question_id=s["q"].id, answer="YES",
            ),
            db=db, current_user=intruder,
        )
    assert exc.value.status_code == 404


# ── End-to-end through the today route ─────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_today_renders_conditional_practice_after_yes_answer(db):
    """While the question is pending, ALL practices are hidden (BL-02
    UX: the farmer must answer first). After YES is submitted, both the
    conditional and the non-conditional practices render."""
    s = await _seed_conditional_setup(db)

    # Before answering: nothing visible; question pending.
    out_before = await get_today_advisory(db=db, current_user=s["user"])
    rt = next(r for r in out_before[0]["timelines"] if r["id"] == s["tl"].id)
    assert rt["practices"] == []
    assert rt.get("has_pending_question") is True
    assert rt["pending_conditional_question"]["question_id"] == s["q"].id

    # Submit YES.
    await submit_conditional_answer(
        request=ConditionalAnswerRequest(
            subscription_id=s["sub"].id, question_id=s["q"].id, answer="YES",
        ),
        db=db, current_user=s["user"],
    )

    # After answering: both practices visible; no pending question.
    out_after = await get_today_advisory(db=db, current_user=s["user"])
    rt = next(r for r in out_after[0]["timelines"] if r["id"] == s["tl"].id)
    practice_l2s = {p["l2_type"] for p in rt["practices"]}
    assert "UREA" in practice_l2s
    assert "MANCOZEB" in practice_l2s
    assert rt.get("has_pending_question") is None or rt.get("has_pending_question") is False


@requires_docker
@pytest.mark.asyncio
async def test_today_blank_path_surfaces_warm_message(db):
    """BLANK answer → linked practice hidden, but always-shown practice
    still renders, AND the route surfaces the question in
    `blank_path_questions` so the PWA can show the warm message."""
    s = await _seed_conditional_setup(db)

    await submit_conditional_answer(
        request=ConditionalAnswerRequest(
            subscription_id=s["sub"].id, question_id=s["q"].id, answer="BLANK",
        ),
        db=db, current_user=s["user"],
    )

    out = await get_today_advisory(db=db, current_user=s["user"])
    rt = next(r for r in out[0]["timelines"] if r["id"] == s["tl"].id)
    practice_l2s = {p["l2_type"] for p in rt["practices"]}
    # Always-shown practice still visible despite BLANK answer.
    assert "UREA" in practice_l2s
    # Conditional practice hidden.
    assert "MANCOZEB" not in practice_l2s
    # Warm-message data is in the response for the PWA to display.
    blank_paths = rt.get("blank_path_questions") or []
    assert any(bp["question_id"] == s["q"].id for bp in blank_paths)
    bp = next(bp for bp in blank_paths if bp["question_id"] == s["q"].id)
    assert bp["farmer_answer"] == "BLANK"
