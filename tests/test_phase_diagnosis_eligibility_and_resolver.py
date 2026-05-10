"""Diagnosis eligibility + bundle-aware CHA resolver (2026-05-10).

Audit-driven fixes after the CHA-PG/SP/QA hub work landed:

  Finding 2 — Diagnose button gating
    The farmer-PWA Diagnose button checked only `hasStartDate`
    pre-fix; nothing checked the crop was still on the CA's belt
    OR that CHA was enabled at the platform level. The new
    `GET /diagnosis/eligibility/{subscription_id}` returns
    `{eligible, reason_code?, message?}` covering both gates.

  Finding 3 — Bundle-aware resolver
    `resolve_cha_recommendation` didn't filter by `area_or_plant`.
    With a single (client, problem_group) hosting both bundles,
    DB ordering decided which one landed in the farmer's advisory.
    Now scopes PG by `area_or_plant` matching the crop's typing
    (and SP by `crop_cosh_id` for symmetry).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from app.modules.advisory.models import (
    PGRecommendation, SPRecommendation,
)
from app.modules.clients.models import ClientCrop
from app.modules.farmpundit.diagnosis_router import (
    get_diagnosis_eligibility,
)
from app.modules.subscriptions.models import (
    Subscription, SubscriptionStatus, SubscriptionType,
)
from app.modules.sync.models import CropHealthCrop
from app.services.cha_hierarchy import resolve_cha_recommendation
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_crop_reference, make_package, make_user,
)


# ── Diagnosis eligibility ──────────────────────────────────────────────────

async def _seed_subscribed_farmer(db, *, crop_cosh_id="crop:test"):
    """Make a client + farmer + package + subscription. Returns
    (client, farmer, sub). Crop is `crop_cosh_id` (factory seeds it
    on the belt + with AREA_WISE measure)."""
    client = await make_client(db)
    farmer = await make_user(db, name="Farmer")
    await make_crop_reference(db, crop_cosh_id, name="Tomato", measure="AREA_WISE")
    package = await make_package(db, client, crop_cosh_id=crop_cosh_id)
    sub = Subscription(
        farmer_user_id=farmer.id, client_id=client.id, package_id=package.id,
        subscription_type=SubscriptionType.SELF, status=SubscriptionStatus.ACTIVE,
    )
    db.add(sub); await db.commit()
    return client, farmer, sub


@requires_docker
@pytest.mark.asyncio
async def test_eligibility_true_when_belt_and_cha_both_active(db):
    from app.modules.farmpundit.diagnosis_router import get_diagnosis_eligibility
    client, farmer, sub = await _seed_subscribed_farmer(db)
    # Enable CHA on the crop.
    db.add(CropHealthCrop(crop_cosh_id="crop:test", status="ACTIVE"))
    await db.commit()

    out = await get_diagnosis_eligibility(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    assert out["eligible"] is True


@requires_docker
@pytest.mark.asyncio
async def test_eligibility_blocks_when_crop_off_belt(db):
    """CA soft-removes the crop after the farmer subscribes — the
    subscription stays per Batch 1A, but the Diagnose button
    must grey on the farmer's app."""
    from app.modules.farmpundit.diagnosis_router import get_diagnosis_eligibility
    from sqlalchemy import select as _sel
    client, farmer, sub = await _seed_subscribed_farmer(db)
    db.add(CropHealthCrop(crop_cosh_id="crop:test", status="ACTIVE"))
    cc = (await db.execute(
        _sel(ClientCrop).where(
            ClientCrop.client_id == client.id,
            ClientCrop.crop_cosh_id == "crop:test",
        )
    )).scalar_one()
    cc.removed_at = datetime.now(timezone.utc)
    await db.commit()

    out = await get_diagnosis_eligibility(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    assert out["eligible"] is False
    assert out["reason_code"] == "crop_not_on_belt"


@requires_docker
@pytest.mark.asyncio
async def test_eligibility_blocks_when_cha_not_enabled(db):
    """Crop is on the belt but RootsTalk hasn't enabled CHA on it."""
    from app.modules.farmpundit.diagnosis_router import get_diagnosis_eligibility
    client, farmer, sub = await _seed_subscribed_farmer(db)
    # No CropHealthCrop row at all → cha_not_enabled.
    out = await get_diagnosis_eligibility(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    assert out["eligible"] is False
    assert out["reason_code"] == "cha_not_enabled"


@requires_docker
@pytest.mark.asyncio
async def test_eligibility_404s_for_other_farmers_subscription(db):
    """Defence: a farmer can't query eligibility for someone else's
    subscription_id. Symmetry with start_diagnosis's ownership gate."""
    from fastapi import HTTPException
    from app.modules.farmpundit.diagnosis_router import get_diagnosis_eligibility
    _, farmer1, sub = await _seed_subscribed_farmer(db)
    farmer2 = await make_user(db, name="Other Farmer")
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await get_diagnosis_eligibility(
            subscription_id=sub.id, db=db, current_user=farmer2,
        )
    assert exc.value.status_code == 404


# ── Bundle-aware resolver ──────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_resolver_picks_pg_bundle_matching_crop_typing(db):
    """Pre-fix: a (client, PG) with both bundles returned whichever
    DB row came first. Post-fix: filters on `area_or_plant` matching
    the crop's typing.

    Setup: client has BOTH bundles for pg:fungal_diseases. Tomato
    is AREA_WISE → resolver must pick the area-wise bundle."""
    from app.services.cha_hierarchy import resolve_cha_recommendation
    client = await make_client(db)
    await make_crop_reference(db, "crop:tomato", name="Tomato", measure="AREA_WISE")
    pg_aw = PGRecommendation(
        problem_group_cosh_id="pg:fungal_diseases", client_id=client.id,
        area_or_plant="AREA_WISE", status="ACTIVE",
    )
    pg_pw = PGRecommendation(
        problem_group_cosh_id="pg:fungal_diseases", client_id=client.id,
        area_or_plant="PLANT_WISE", status="ACTIVE",
    )
    db.add_all([pg_aw, pg_pw])
    await db.commit()

    out = await resolve_cha_recommendation(
        db, client.id, "pg:fungal_diseases", crop_cosh_id="crop:tomato",
    )
    assert out is not None
    assert out.recommendation_id == pg_aw.id
    assert out.recommendation_type == "PG"


@requires_docker
@pytest.mark.asyncio
async def test_resolver_picks_pg_plant_bundle_for_plantwise_crop(db):
    """Mirror: a PLANT_WISE crop must land on the plant-wise bundle."""
    from app.services.cha_hierarchy import resolve_cha_recommendation
    client = await make_client(db)
    await make_crop_reference(db, "crop:apple", name="Apple", measure="PLANT_WISE")
    pg_aw = PGRecommendation(
        problem_group_cosh_id="pg:fungal_diseases", client_id=client.id,
        area_or_plant="AREA_WISE", status="ACTIVE",
    )
    pg_pw = PGRecommendation(
        problem_group_cosh_id="pg:fungal_diseases", client_id=client.id,
        area_or_plant="PLANT_WISE", status="ACTIVE",
    )
    db.add_all([pg_aw, pg_pw])
    await db.commit()

    out = await resolve_cha_recommendation(
        db, client.id, "pg:fungal_diseases", crop_cosh_id="crop:apple",
    )
    assert out.recommendation_id == pg_pw.id


@requires_docker
@pytest.mark.asyncio
async def test_resolver_with_no_crop_returns_any_pg(db):
    """Backward compat: callers that don't supply crop_cosh_id (e.g.
    legacy code) get the unfiltered behaviour. We don't break them
    while warning: pass crop_cosh_id when you know it."""
    from app.services.cha_hierarchy import resolve_cha_recommendation
    client = await make_client(db)
    pg = PGRecommendation(
        problem_group_cosh_id="pg:fungal_diseases", client_id=client.id,
        area_or_plant="AREA_WISE", status="ACTIVE",
    )
    db.add(pg); await db.commit()

    out = await resolve_cha_recommendation(
        db, client.id, "pg:fungal_diseases",  # no crop_cosh_id
    )
    assert out is not None
    assert out.recommendation_id == pg.id


@requires_docker
@pytest.mark.asyncio
async def test_resolver_sp_filters_on_crop(db):
    """Defensive: SP rows are crop-bound by Cosh modelling, but the
    resolver's filter mirrors the PG bundle pattern. Guards against
    duplicate SPs across crops."""
    from app.services.cha_hierarchy import resolve_cha_recommendation
    from app.modules.sync.models import CoshCoreItem
    client = await make_client(db)
    await make_crop_reference(db, "crop:tomato", name="Tomato", measure="AREA_WISE")
    # Seed Cosh entry tagging this as a specific_problem with a parent PG.
    db.add(CoshCoreItem(
        cosh_id="sp:tomato_late_blight", core_type="specific_problem",
        parent_cosh_id="pg:fungal_diseases",
        translations={"en": "Tomato Late Blight"}, status="active",
    ))
    sp_correct = SPRecommendation(
        specific_problem_cosh_id="sp:tomato_late_blight",
        client_id=client.id, crop_cosh_id="crop:tomato", status="ACTIVE",
    )
    # A defensive duplicate that shouldn't exist (same sp_cosh_id but
    # claims a different crop). The crop filter must skip it.
    sp_wrong = SPRecommendation(
        specific_problem_cosh_id="sp:tomato_late_blight",
        client_id=client.id, crop_cosh_id="crop:other", status="ACTIVE",
    )
    db.add_all([sp_correct, sp_wrong])
    await db.commit()

    out = await resolve_cha_recommendation(
        db, client.id, "sp:tomato_late_blight", crop_cosh_id="crop:tomato",
    )
    assert out is not None
    assert out.recommendation_id == sp_correct.id
    assert out.recommendation_type == "SP"
