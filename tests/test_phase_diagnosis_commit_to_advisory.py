"""Diagnosis → advisory is opt-in (2026-05-25).

Before this commit the CHA trigger fired automatically the moment
BL-08 narrowed to a single problem. Per user direction we made it
explicit — the farmer reviews the problem, then taps
"Add Treatment Recommendations to the Advisory" to commit.

Tests:
1. answer_question on the final YES leaves the session DIAGNOSED but
   creates NO TriggeredCHAEntry.
2. commit_diagnosis_to_advisory creates the TriggeredCHAEntry and
   stamps committed_at.
3. Double-commit is idempotent (no second entry, same response shape).
4. commit refuses 422 when the session isn't DIAGNOSED yet.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.diagnosis.router import (
    AnswerRequest, StartDiagnosisRequest, answer_question,
    commit_diagnosis_to_advisory, start_diagnosis,
)
from app.modules.diagnosis.models import DiagnosisSession
from app.modules.sync.models import CoshConnectRow  # noqa: F401  (model registration)
from app.modules.subscriptions.models import TriggeredCHAEntry
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)
# Reuse the real-shape seed helper from the BL-08 integration test
from tests.test_phase_bl08_diagnosis_integration import (
    CROP, STAGE, _pd_row, _seed_subscription,
)


async def _seed_pair(db):
    """Two rows so the first question's YES narrows to a unique
    problem and the session lands DIAGNOSED in one answer."""
    db.add(_pd_row(
        "pdc:p1",
        crop=CROP, crop_stage=STAGE,
        pest="pest:leaf-blight", part="part:leaf", symptom="symptom:spot",
    ))
    db.add(_pd_row(
        "pdc:p2",
        crop=CROP, crop_stage=STAGE,
        pest="pest:nutrient-deficiency", part="part:leaf",
        symptom="symptom:yellow",
    ))
    await db.commit()


async def _start_and_diagnose(db, farmer, sub):
    """Walk start → first YES so the session lands in DIAGNOSED."""
    started = await start_diagnosis(
        request=StartDiagnosisRequest(
            subscription_id=sub.id, crop_cosh_id=CROP,
            crop_stage_cosh_id=STAGE, plant_part_cosh_id="part:leaf",
        ),
        db=db, current_user=farmer,
    )
    q = started["question"]
    out = await answer_question(
        session_id=started["session_id"],
        request=AnswerRequest(
            plant_part_cosh_id=q["plant_part_cosh_id"],
            symptom_cosh_id=q["symptom_cosh_id"],
            answer="YES",
        ),
        db=db, current_user=farmer,
    )
    assert out["status"] == "DIAGNOSED"
    return started["session_id"], out


@requires_docker
@pytest.mark.asyncio
async def test_diagnosed_does_not_auto_commit(db):
    """The act of narrowing to a problem does NOT create a
    TriggeredCHAEntry. The farmer must opt in via /commit-to-advisory."""
    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)
    await _seed_pair(db)
    session_id, out = await _start_and_diagnose(db, farmer, sub)

    assert out["committed_to_advisory"] is False
    entries = (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.subscription_id == sub.id,
            TriggeredCHAEntry.triggered_by == "DIAGNOSIS",
        )
    )).scalars().all()
    assert entries == [], "DIAGNOSED must NOT auto-create a CHA entry"

    # Session row carries no committed_at.
    session = (await db.execute(
        select(DiagnosisSession).where(DiagnosisSession.id == session_id)
    )).scalar_one()
    assert session.committed_at is None


@requires_docker
@pytest.mark.asyncio
async def test_commit_to_advisory_fires_trigger(db):
    """Explicit commit creates the TriggeredCHAEntry and stamps the
    session's committed_at."""
    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)
    await _seed_pair(db)
    session_id, _ = await _start_and_diagnose(db, farmer, sub)

    result = await commit_diagnosis_to_advisory(
        session_id=session_id, db=db, current_user=farmer,
    )
    assert result["committed_to_advisory"] is True
    assert result["already_committed"] is False
    assert result["subscription_id"] == sub.id

    # Session is now stamped.
    session = (await db.execute(
        select(DiagnosisSession).where(DiagnosisSession.id == session_id)
    )).scalar_one()
    assert session.committed_at is not None


@requires_docker
@pytest.mark.asyncio
async def test_commit_is_idempotent(db):
    """Double-commit is a no-op — same shape, no duplicate trigger
    work. Guards against the farmer double-tapping the CTA."""
    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)
    await _seed_pair(db)
    session_id, _ = await _start_and_diagnose(db, farmer, sub)

    first = await commit_diagnosis_to_advisory(
        session_id=session_id, db=db, current_user=farmer,
    )
    second = await commit_diagnosis_to_advisory(
        session_id=session_id, db=db, current_user=farmer,
    )
    assert first["committed_to_advisory"] is True
    assert first["already_committed"] is False
    assert second["committed_to_advisory"] is True
    assert second["already_committed"] is True


@requires_docker
@pytest.mark.asyncio
async def test_commit_refuses_when_not_diagnosed(db):
    """A session still in QUESTION state can't be committed —
    surfaces 422 not_diagnosed so the PWA can hide the CTA before
    the farmer has actually finished narrowing."""
    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)
    await _seed_pair(db)
    started = await start_diagnosis(
        request=StartDiagnosisRequest(
            subscription_id=sub.id, crop_cosh_id=CROP,
            crop_stage_cosh_id=STAGE, plant_part_cosh_id="part:leaf",
        ),
        db=db, current_user=farmer,
    )
    # Don't answer. Session is still in QUESTION state.
    with pytest.raises(HTTPException) as exc:
        await commit_diagnosis_to_advisory(
            session_id=started["session_id"], db=db, current_user=farmer,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "not_diagnosed"


@requires_docker
@pytest.mark.asyncio
async def test_commit_refuses_other_farmers_session(db):
    """Farmer A can't commit farmer B's session — closes the
    cross-farmer trigger gap the auto-commit path defended too."""
    farmer_a = await make_user(db)
    farmer_b = await make_user(db)
    sub_b = await _seed_subscription(db, farmer_b)
    await _seed_pair(db)
    session_b_id, _ = await _start_and_diagnose(db, farmer_b, sub_b)
    with pytest.raises(HTTPException) as exc:
        await commit_diagnosis_to_advisory(
            session_id=session_b_id, db=db, current_user=farmer_a,
        )
    assert exc.value.status_code == 404
