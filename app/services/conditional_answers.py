"""Effective conditional-answer resolution.

BL-02 stores one ConditionalAnswer row per (subscription, question, date).
The naive read path — "load today's answers only" — meant a YES/NO given
on day N re-appeared as a pending question on day N+1. Once the farmer
has committed to a decision that shapes the timeline's practice set, the
answer stays sticky for the rest of the timeline's lifecycle; the farmer
doesn't re-answer every morning.

Stickiness rule (2026-08-22, anchor DE-26-000299):
  1. If today has an answer for a question, use it as-is (this lets the
     farmer revise a stale YES/NO on any given day).
  2. Otherwise, use the most recent prior YES or NO answer for that
     question if any exists.
  3. Otherwise, the question stays pending.

BLANK is deliberately excluded from prior-day carry-over — the farmer's
"BLANK" reads as "I'll answer later", so it must re-prompt each day
until the farmer commits.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.subscriptions.models import ConditionalAnswer


async def resolve_effective_answers(
    db: AsyncSession, subscription_id: str, today: date,
) -> dict[str, str]:
    """Return {question_id: answer} using the stickiness rule above.

    Answer values are normalised to strings ("YES" / "NO" / "BLANK")
    to match the shape the callers previously built from
    `ConditionalAnswer.answer`.
    """
    rows = (await db.execute(
        select(ConditionalAnswer)
        .where(ConditionalAnswer.subscription_id == subscription_id)
        .order_by(ConditionalAnswer.answer_date.desc())
    )).scalars().all()

    today_answers: dict[str, str] = {}
    prior_yesno: dict[str, str] = {}
    for r in rows:
        qid = r.question_id
        val = r.answer.value if hasattr(r.answer, "value") else str(r.answer)
        if r.answer_date == today:
            if qid not in today_answers:
                today_answers[qid] = val
        elif val in ("YES", "NO") and qid not in prior_yesno:
            prior_yesno[qid] = val

    return {**prior_yesno, **today_answers}
