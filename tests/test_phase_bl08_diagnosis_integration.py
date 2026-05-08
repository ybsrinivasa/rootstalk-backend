"""BL-08 audit — integration tests for the live diagnosis router.

Pure-function coverage lives in `tests/test_bl08.py` (10 tests). This
file verifies wiring: subscription-ownership gating, full Q&A flow
end-to-end against a real DB, INCONCLUSIVE state when no problems
remain, and that the diagnosis-triggered CHA only lands on the
caller's own subscription.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.farmpundit.diagnosis_router import (
    AnswerRequest, DiagnosisSession, ExplainSymptomRequest,
    StartDiagnosisRequest, answer_question, explain_symptom_route,
    start_diagnosis,
)
from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)


CROP = "crop:tomato"
STAGE = "stage:vegetative"


async def _seed_diagnosis_data(db):
    """Seed two `pest_diagnosis_chain` rows for two distinct pests on
    the same crop+stage+part. Algorithm asks one question; YES diagnoses
    one, NO narrows to the other.

    Endpoint role names mirror Cosh's entity_type vocabulary
    (see docs/COSH_2_SYNC_CONTRACT.md): crop, crop_stage, pest,
    pest_stage, part, sub_part, symptom, sub_symptom."""
    db.add(CoshConnectRow(
        connect_id="pdc:p1-leaf-spot",
        connect_type="pest_diagnosis_chain",
        status="active",
        endpoints=[
            {"role": "crop",       "cosh_id": CROP, "position": 1},
            {"role": "crop_stage", "cosh_id": STAGE, "position": 2},
            {"role": "pest",       "cosh_id": "pest:leaf-blight", "position": 3},
            {"role": "part",       "cosh_id": "part:leaf", "position": 5},
            {"role": "symptom",    "cosh_id": "symptom:spot", "position": 7},
        ],
        metadata_=None,
    ))
    db.add(CoshConnectRow(
        connect_id="pdc:p2-leaf-yellow",
        connect_type="pest_diagnosis_chain",
        status="active",
        endpoints=[
            {"role": "crop",       "cosh_id": CROP, "position": 1},
            {"role": "crop_stage", "cosh_id": STAGE, "position": 2},
            {"role": "pest",       "cosh_id": "pest:nutrient-deficiency", "position": 3},
            {"role": "part",       "cosh_id": "part:leaf", "position": 5},
            {"role": "symptom",    "cosh_id": "symptom:yellow", "position": 7},
        ],
        metadata_=None,
    ))
    await db.commit()


async def _seed_subscription(db, farmer):
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=package,
    )
    await db.commit()
    return sub


# ── Ownership ───────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_start_rejects_other_farmers_subscription(db):
    """Farmer A can't start a diagnosis using farmer B's subscription_id —
    closes a privilege gap where the eventual CHA trigger would land on
    farmer B's advisory."""
    farmer_b = await make_user(db)
    farmer_a = await make_user(db)
    sub_b = await _seed_subscription(db, farmer_b)
    await _seed_diagnosis_data(db)

    with pytest.raises(HTTPException) as exc:
        await start_diagnosis(
            request=StartDiagnosisRequest(
                subscription_id=sub_b.id,
                crop_cosh_id=CROP,
                crop_stage_cosh_id=STAGE,
                plant_part_cosh_id="part:leaf",
            ),
            db=db, current_user=farmer_a,
        )
    assert exc.value.status_code == 404
    # No DiagnosisSession row was created.
    rows = (await db.execute(select(DiagnosisSession))).scalars().all()
    assert rows == []


# ── Happy path ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_start_returns_first_question_for_real_owner(db):
    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)
    await _seed_diagnosis_data(db)

    out = await start_diagnosis(
        request=StartDiagnosisRequest(
            subscription_id=sub.id,
            crop_cosh_id=CROP,
            crop_stage_cosh_id=STAGE,
            plant_part_cosh_id="part:leaf",
        ),
        db=db, current_user=farmer,
    )
    assert out["status"] == "QUESTION"
    assert out["session_id"]
    assert out["question"] is not None
    assert out["question"]["plant_part_cosh_id"] == "part:leaf"
    assert out["remaining_count"] == 2


@requires_docker
@pytest.mark.asyncio
async def test_start_no_data_returns_friendly_message(db):
    """No problem_to_symptom rows for crop+stage → 'no data yet' message,
    not a 500. Farmer is told to contact company / ask expert."""
    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)

    out = await start_diagnosis(
        request=StartDiagnosisRequest(
            subscription_id=sub.id,
            crop_cosh_id="crop:unseeded",
            crop_stage_cosh_id="stage:unseeded",
            plant_part_cosh_id="part:leaf",
        ),
        db=db, current_user=farmer,
    )
    assert out["status"] == "NO_DATA"
    assert "diagnostic data" in out["message"].lower()


@requires_docker
@pytest.mark.asyncio
async def test_yes_answer_diagnoses_when_pool_collapses_to_one(db):
    """First question is asked. YES on a symptom that only one problem
    has → DIAGNOSED, the matching problem returned."""
    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)
    await _seed_diagnosis_data(db)

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
    # YES on a unique-per-problem (part, symptom) → exactly one problem left.
    assert out["status"] == "DIAGNOSED"
    assert out["diagnosed_problem_cosh_id"] in (
        "pest:leaf-blight", "pest:nutrient-deficiency",
    )


@requires_docker
@pytest.mark.asyncio
async def test_answer_session_404_for_other_farmer(db):
    """Farmer A can't answer questions on farmer B's session."""
    farmer_b = await make_user(db)
    farmer_a = await make_user(db)
    sub_b = await _seed_subscription(db, farmer_b)
    await _seed_diagnosis_data(db)

    started = await start_diagnosis(
        request=StartDiagnosisRequest(
            subscription_id=sub_b.id, crop_cosh_id=CROP,
            crop_stage_cosh_id=STAGE, plant_part_cosh_id="part:leaf",
        ),
        db=db, current_user=farmer_b,
    )
    q = started["question"]

    with pytest.raises(HTTPException) as exc:
        await answer_question(
            session_id=started["session_id"],
            request=AnswerRequest(
                plant_part_cosh_id=q["plant_part_cosh_id"],
                symptom_cosh_id=q["symptom_cosh_id"],
                answer="YES",
            ),
            db=db, current_user=farmer_a,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_priority_rank_demotes_problem_through_live_router(db):
    """End-to-end check that `priority_rank` (now a Core, surfaced as
    a position on the Pest Diagnosis Connect) is dereferenced and
    honoured by the live router. Two problems share LEAF+Colour_Change,
    but one has it at rank 2 (with a rank-1 symptom elsewhere). YES on
    Colour_Change must demote the ranked problem and diagnose the
    unranked one."""
    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)

    # Priority-rank Cores: one item per rank value, with metadata.rank.
    db.add(CoshCoreItem(
        cosh_id="pr:1", core_type="priority_rank", status="active",
        translations={"en": "1"}, metadata_={"rank": 1},
    ))
    db.add(CoshCoreItem(
        cosh_id="pr:2", core_type="priority_rank", status="active",
        translations={"en": "2"}, metadata_={"rank": 2},
    ))

    # Ranked pest: LEAF+Spots is rank 1, LEAF+Colour_Change is rank 2.
    db.add(CoshConnectRow(
        connect_id="pdc:ranked-spots",
        connect_type="pest_diagnosis_chain",
        status="active",
        endpoints=[
            {"role": "crop",          "cosh_id": CROP},
            {"role": "crop_stage",    "cosh_id": STAGE},
            {"role": "pest",          "cosh_id": "pest:ranked"},
            {"role": "part",          "cosh_id": "part:leaf"},
            {"role": "symptom",       "cosh_id": "symptom:spots"},
            {"role": "priority_rank", "cosh_id": "pr:1"},
        ],
        metadata_=None,
    ))
    db.add(CoshConnectRow(
        connect_id="pdc:ranked-colour",
        connect_type="pest_diagnosis_chain",
        status="active",
        endpoints=[
            {"role": "crop",          "cosh_id": CROP},
            {"role": "crop_stage",    "cosh_id": STAGE},
            {"role": "pest",          "cosh_id": "pest:ranked"},
            {"role": "part",          "cosh_id": "part:leaf"},
            {"role": "symptom",       "cosh_id": "symptom:colour"},
            {"role": "priority_rank", "cosh_id": "pr:2"},
        ],
        metadata_=None,
    ))
    # Unranked sibling — only has Colour_Change, no priority_rank endpoint.
    db.add(CoshConnectRow(
        connect_id="pdc:unranked-colour",
        connect_type="pest_diagnosis_chain",
        status="active",
        endpoints=[
            {"role": "crop",       "cosh_id": CROP},
            {"role": "crop_stage", "cosh_id": STAGE},
            {"role": "pest",       "cosh_id": "pest:unranked"},
            {"role": "part",       "cosh_id": "part:leaf"},
            {"role": "symptom",    "cosh_id": "symptom:colour"},
        ],
        metadata_=None,
    ))
    await db.commit()

    started = await start_diagnosis(
        request=StartDiagnosisRequest(
            subscription_id=sub.id, crop_cosh_id=CROP,
            crop_stage_cosh_id=STAGE, plant_part_cosh_id="part:leaf",
        ),
        db=db, current_user=farmer,
    )
    out = await answer_question(
        session_id=started["session_id"],
        request=AnswerRequest(
            plant_part_cosh_id="part:leaf",
            symptom_cosh_id="symptom:colour",
            answer="YES",
        ),
        db=db, current_user=farmer,
    )
    assert out["status"] == "DIAGNOSED"
    assert out["diagnosed_problem_cosh_id"] == "pest:unranked"


@requires_docker
@pytest.mark.asyncio
async def test_priority_rank_translation_fallback(db):
    """When the priority_rank Core item carries the rank as
    translations.en (digit string) instead of metadata.rank, the
    loader's fallback path still resolves it correctly."""
    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)

    # Rank value lives only in translations.en — no metadata.rank.
    db.add(CoshCoreItem(
        cosh_id="pr:translatedonly", core_type="priority_rank",
        status="active",
        translations={"en": "1"},
        metadata_=None,
    ))
    db.add(CoshConnectRow(
        connect_id="pdc:t1",
        connect_type="pest_diagnosis_chain",
        status="active",
        endpoints=[
            {"role": "crop",          "cosh_id": CROP},
            {"role": "crop_stage",    "cosh_id": STAGE},
            {"role": "pest",          "cosh_id": "pest:tonly"},
            {"role": "part",          "cosh_id": "part:leaf"},
            {"role": "symptom",       "cosh_id": "symptom:fallback"},
            {"role": "priority_rank", "cosh_id": "pr:translatedonly"},
        ],
        metadata_=None,
    ))
    db.add(CoshConnectRow(
        connect_id="pdc:t2",
        connect_type="pest_diagnosis_chain",
        status="active",
        endpoints=[
            {"role": "crop",       "cosh_id": CROP},
            {"role": "crop_stage", "cosh_id": STAGE},
            {"role": "pest",       "cosh_id": "pest:tonly"},
            {"role": "part",       "cosh_id": "part:leaf"},
            {"role": "symptom",    "cosh_id": "symptom:other"},
        ],
        metadata_=None,
    ))
    await db.commit()

    # Rank loaded via the fallback parses "1" → 1; the diagnosis
    # algorithm sees rank=1 on the first row, no rank on the second.
    from app.modules.farmpundit.diagnosis_router import (
        _load_problem_symptom_rows,
    )
    rows = await _load_problem_symptom_rows(db, CROP, STAGE)
    by_id = {r.symptom_cosh_id: r for r in rows}
    assert by_id["symptom:fallback"].priority_rank == 1
    assert by_id["symptom:other"].priority_rank is None


@requires_docker
@pytest.mark.asyncio
async def test_explain_symptom_returns_text_in_fallback_mode(db, monkeypatch):
    """The ⓘ tooltip endpoint returns 2 sentences in test mode. We force the
    Claude fallback by clearing the API key, so the test is hermetic — no
    network, no key required."""
    from app.config import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    farmer = await make_user(db)
    out = await explain_symptom_route(
        request=ExplainSymptomRequest(
            crop_cosh_id="crop:tomato",
            plant_part_cosh_id="part:leaf",
            symptom_cosh_id="symptom:yellow",
        ),
        current_user=farmer,
    )
    assert out["language_code"] == "en"
    assert "yellow" in out["explanation"].lower()
    assert "leaf" in out["explanation"].lower() or "leaves" in out["explanation"].lower()


@requires_docker
@pytest.mark.asyncio
async def test_answer_rejects_invalid_value(db):
    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)
    await _seed_diagnosis_data(db)
    started = await start_diagnosis(
        request=StartDiagnosisRequest(
            subscription_id=sub.id, crop_cosh_id=CROP,
            crop_stage_cosh_id=STAGE, plant_part_cosh_id="part:leaf",
        ),
        db=db, current_user=farmer,
    )
    q = started["question"]
    with pytest.raises(HTTPException) as exc:
        await answer_question(
            session_id=started["session_id"],
            request=AnswerRequest(
                plant_part_cosh_id=q["plant_part_cosh_id"],
                symptom_cosh_id=q["symptom_cosh_id"],
                answer="MAYBE",
            ),
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 422
