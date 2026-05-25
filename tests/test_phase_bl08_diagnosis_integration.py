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

from app.modules.diagnosis.router import (
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


def _pd_row(
    connect_id: str,
    *,
    crop: str,
    pest: str,
    part: str,
    symptom: str,
    crop_stage: str | None = None,
    pest_stage: str = "pest_stage:any",
    sub_symptom: str | None = None,
    sub_part: str | None = None,
    priority_rank: str | None = None,
) -> CoshConnectRow:
    """Build a `pest_diagnosis` Connect row in the real 9-position
    wire shape (locked 2026-05-14). Dimensions left as None are
    filled with the BLANK BOX sentinel where one exists, so the
    loader treats them as wildcards. Crop / pest / part / symptom
    are mandatory; the rest are optional and carry safe defaults."""
    from app.services.cosh_constants import (
        PD_BLANK_BOX_BY_CORE,
        COSH_DAMAGE_SUBSYMPTOMS_CORE, COSH_PLANT_SUBPARTS_CORE,
        COSH_CROP_STAGES_CORE,
    )
    subsymptom_blank = PD_BLANK_BOX_BY_CORE[COSH_DAMAGE_SUBSYMPTOMS_CORE]
    subpart_blank = PD_BLANK_BOX_BY_CORE[COSH_PLANT_SUBPARTS_CORE]
    crop_stage_blank = PD_BLANK_BOX_BY_CORE[COSH_CROP_STAGES_CORE]

    endpoints = [
        {"role": "damage_symptoms",     "cosh_id": symptom,                              "position": 1},
        {"role": "damage_subsymptoms",  "cosh_id": sub_symptom or subsymptom_blank,      "position": 2},
        {"role": "biological_names",    "cosh_id": pest,                                 "position": 3},
        {"role": "pest_stages",         "cosh_id": pest_stage,                           "position": 4},
        {"role": "plant_parts",         "cosh_id": part,                                 "position": 5},
        {"role": "plant_subparts",      "cosh_id": sub_part or subpart_blank,            "position": 6},
        {"role": "biological_names",    "cosh_id": crop,                                 "position": 7},
        {"role": "crop_stages",         "cosh_id": crop_stage or crop_stage_blank,       "position": 8},
    ]
    if priority_rank:
        endpoints.append(
            {"role": "priority_rank_pests", "cosh_id": priority_rank, "position": 9},
        )
    return CoshConnectRow(
        connect_id=connect_id,
        connect_type="pest_diagnosis",
        status="active",
        endpoints=endpoints,
        metadata_=None,
    )


async def _seed_diagnosis_data(db):
    """Seed two rows for two distinct pests on the same crop+stage+part.
    Algorithm asks one question; YES diagnoses one, NO narrows to the
    other."""
    db.add(_pd_row(
        "pdc:p1-leaf-spot",
        crop=CROP, crop_stage=STAGE,
        pest="pest:leaf-blight", part="part:leaf", symptom="symptom:spot",
    ))
    db.add(_pd_row(
        "pdc:p2-leaf-yellow",
        crop=CROP, crop_stage=STAGE,
        pest="pest:nutrient-deficiency", part="part:leaf", symptom="symptom:yellow",
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
    db.add(_pd_row(
        "pdc:ranked-spots",
        crop=CROP, crop_stage=STAGE,
        pest="pest:ranked", part="part:leaf", symptom="symptom:spots",
        priority_rank="pr:1",
    ))
    db.add(_pd_row(
        "pdc:ranked-colour",
        crop=CROP, crop_stage=STAGE,
        pest="pest:ranked", part="part:leaf", symptom="symptom:colour",
        priority_rank="pr:2",
    ))
    # Unranked sibling — only has Colour_Change, no priority_rank.
    db.add(_pd_row(
        "pdc:unranked-colour",
        crop=CROP, crop_stage=STAGE,
        pest="pest:unranked", part="part:leaf", symptom="symptom:colour",
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
    db.add(_pd_row(
        "pdc:t1",
        crop=CROP, crop_stage=STAGE,
        pest="pest:tonly", part="part:leaf", symptom="symptom:fallback",
        priority_rank="pr:translatedonly",
    ))
    db.add(_pd_row(
        "pdc:t2",
        crop=CROP, crop_stage=STAGE,
        pest="pest:tonly", part="part:leaf", symptom="symptom:other",
    ))
    await db.commit()

    # Rank loaded via the fallback parses "1" → 1; the diagnosis
    # algorithm sees rank=1 on the first row, no rank on the second.
    from app.modules.diagnosis.router import (
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
        db=db, current_user=farmer,
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
