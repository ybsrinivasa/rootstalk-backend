"""Direct-AI diagnosis path (2026-05-25).

The farmer skips BL-08 Q&A; uploads N photos; Claude is constrained
to pick from the crop+stage's known catalogue or signal needs_expert.
Tests run with ANTHROPIC_API_KEY unset so the Claude call hits the
graceful-degrade branch (returns needs_expert=True without a network
call). This exercises the wiring — endpoint contract, ownership gate,
session creation, payload shape — without depending on live Claude
responses.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.diagnosis.models import DiagnosisSession
from app.modules.diagnosis.router import ai_direct_diagnose
from app.modules.diagnosis.schemas import (
    AIDirectDiagnoseRequest, AIDirectImage,
)
from app.modules.subscriptions.models import TriggeredCHAEntry
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)


async def _seed_subscription(db, farmer):
    client = await make_client(db)
    package = await make_package(db, client, crop_cosh_id="crop:tomato")
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=package,
    )
    await db.commit()
    return sub


@requires_docker
@pytest.mark.asyncio
async def test_ai_direct_diagnose_rejects_other_farmers_subscription(db):
    """Subscription ownership gate — same protection BL-08 start has."""
    other = await make_user(db)
    me = await make_user(db)
    sub = await _seed_subscription(db, other)

    with pytest.raises(HTTPException) as exc:
        await ai_direct_diagnose(
            request=AIDirectDiagnoseRequest(
                subscription_id=sub.id,
                crop_cosh_id="crop:tomato",
                images=[AIDirectImage(base64="x", media_type="image/jpeg")],
            ),
            db=db, current_user=me,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_ai_direct_diagnose_needs_expert_when_claude_unavailable(db, monkeypatch):
    """With ANTHROPIC_API_KEY unset, the service degrades gracefully
    to needs_expert=True. The endpoint must surface that honestly
    without seeding a session."""
    from app.config import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)

    out = await ai_direct_diagnose(
        request=AIDirectDiagnoseRequest(
            subscription_id=sub.id,
            crop_cosh_id="crop:tomato",
            images=[AIDirectImage(base64="x", media_type="image/jpeg")],
        ),
        db=db, current_user=farmer,
    )
    assert out["needs_expert"] is True
    assert out["analysis"]["problem_cosh_id"] is None
    # No session created on the needs-expert branch.
    sessions = (await db.execute(
        select(DiagnosisSession).where(
            DiagnosisSession.subscription_id == sub.id,
        )
    )).scalars().all()
    assert sessions == []


@requires_docker
@pytest.mark.asyncio
async def test_ai_direct_diagnose_no_images_returns_needs_expert(db, monkeypatch):
    """Empty images list is treated as needs_expert with a clear copy
    — the PWA shouldn't reach this, but the backend defends."""
    from app.config import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)
    out = await ai_direct_diagnose(
        request=AIDirectDiagnoseRequest(
            subscription_id=sub.id,
            crop_cosh_id="crop:tomato",
            images=[],
        ),
        db=db, current_user=farmer,
    )
    assert out["needs_expert"] is True


@requires_docker
@pytest.mark.asyncio
async def test_ai_direct_diagnose_match_seeds_diagnosed_session(db, monkeypatch):
    """When Claude returns a confident in-list match, the endpoint
    seeds a DiagnosisSession in DIAGNOSED state so the existing
    /commit-to-advisory endpoint can trigger CHA the same way the
    BL-08 path does. No TriggeredCHAEntry created until commit."""
    from app.services import claude_service
    from app.services.claude_service import ImageAnalysisResult

    async def fake_analyze(
        images, crop_name, crop_stage_name,
        known_problem_ids, known_problem_names, language_code="en",
    ):
        # Pretend Claude confidently picked the first catalogue entry.
        pid = known_problem_ids[0] if known_problem_ids else None
        return ImageAnalysisResult(
            problem_name="Tomato - Fake Pest",
            problem_cosh_id=pid,
            confidence="HIGH",
            description="Brown spots on the lower leaves. Caused by fake pest; moderate severity.",
            symptoms_observed=["brown spots", "wilting"],
            needs_expert=False,
        )

    # Patch the imported reference inside the router module too —
    # the endpoint imports the function by name.
    from app.modules.diagnosis import router as diag_router
    monkeypatch.setattr(diag_router, "analyze_crop_images_constrained", fake_analyze)
    monkeypatch.setattr(claude_service, "analyze_crop_images_constrained", fake_analyze)

    # Patch list_candidates so we don't depend on Cosh data being
    # seeded — we control what catalogue Claude sees.
    from app.services import pest_diagnosis_view
    async def fake_list_candidates(db, *, crop_cosh_id, **kwargs):
        return [
            {"pest_cosh_id": "pest:fake-1", "pest_name": "Fake Pest 1",
             "pest_stage_name": "Adult", "priority_rank": 0, "image_urls": []},
            {"pest_cosh_id": "pest:fake-2", "pest_name": "Fake Pest 2",
             "pest_stage_name": "Larva", "priority_rank": 1, "image_urls": []},
        ]
    monkeypatch.setattr(pest_diagnosis_view, "list_candidates", fake_list_candidates)

    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)

    out = await ai_direct_diagnose(
        request=AIDirectDiagnoseRequest(
            subscription_id=sub.id,
            crop_cosh_id="crop:tomato",
            images=[AIDirectImage(base64="x", media_type="image/jpeg")],
        ),
        db=db, current_user=farmer,
    )

    assert out["needs_expert"] is False
    assert out["session_id"] is not None
    assert out["analysis"]["problem_cosh_id"] == "pest:fake-1"
    assert out["committed_to_advisory"] is False

    # Session is DIAGNOSED, not yet committed.
    session = (await db.execute(
        select(DiagnosisSession).where(
            DiagnosisSession.id == out["session_id"],
        )
    )).scalar_one()
    assert session.status == "DIAGNOSED"
    assert session.diagnosed_problem_cosh_id == "pest:fake-1"
    assert session.committed_at is None

    # No TriggeredCHAEntry yet — opt-in commit owns that.
    entries = (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.subscription_id == sub.id,
        )
    )).scalars().all()
    assert entries == []


@requires_docker
@pytest.mark.asyncio
async def test_ai_direct_diagnose_rejects_claude_out_of_list_pick(db, monkeypatch):
    """Defence in depth: even if Claude returns a cosh_id outside the
    catalogue, the endpoint's helper coerces it to needs_expert via
    the service layer's set-membership check. End-to-end: no session
    is seeded."""
    from app.services import claude_service
    from app.services.claude_service import ImageAnalysisResult

    async def claude_returns_phantom(
        images, crop_name, crop_stage_name,
        known_problem_ids, known_problem_names, language_code="en",
    ):
        # The service's defence-in-depth check would already coerce
        # this; here we just simulate the post-coercion shape so the
        # endpoint sees needs_expert=True.
        return ImageAnalysisResult(
            problem_name="Outside known catalogue",
            problem_cosh_id=None,
            confidence="LOW",
            description="The photos didn't match anything in the catalogue.",
            symptoms_observed=[],
            needs_expert=True,
        )

    from app.modules.diagnosis import router as diag_router
    from app.services import pest_diagnosis_view
    monkeypatch.setattr(diag_router, "analyze_crop_images_constrained", claude_returns_phantom)
    monkeypatch.setattr(claude_service, "analyze_crop_images_constrained", claude_returns_phantom)
    async def fake_list_candidates(db, *, crop_cosh_id, **kwargs):
        return [{"pest_cosh_id": "pest:in-list", "pest_name": "In List"}]
    monkeypatch.setattr(pest_diagnosis_view, "list_candidates", fake_list_candidates)

    farmer = await make_user(db)
    sub = await _seed_subscription(db, farmer)

    out = await ai_direct_diagnose(
        request=AIDirectDiagnoseRequest(
            subscription_id=sub.id,
            crop_cosh_id="crop:tomato",
            images=[AIDirectImage(base64="x", media_type="image/jpeg")],
        ),
        db=db, current_user=farmer,
    )
    assert out["needs_expert"] is True
    sessions = (await db.execute(
        select(DiagnosisSession).where(
            DiagnosisSession.subscription_id == sub.id,
        )
    )).scalars().all()
    assert sessions == []
