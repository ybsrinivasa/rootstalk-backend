"""CHA hub list endpoints (2026-05-10).

Mirror of /cca/* tests for Problem-Group recommendations.

Pins:
  • cha_list_problems returns the V1 hardcoded PG list with
    per-bundle status (area-wise + plant-wise) for the company.
  • cha_list_recommendations chip-filters on PG, area_or_plant,
    status. Each row carries denormalised PG name + timeline_count.
  • cha_list_timelines chip-filters on PG, recommendation,
    area_or_plant. Each row carries the bundle context + practice
    count. QA-rooted timelines (standard_response_id) are excluded.
  • cha_list_practices: cross-timeline cross-cutting list with full
    breadcrumb + brand + dosage summary. Paginated.
"""
from __future__ import annotations

import pytest

from app.modules.advisory.models import (
    PGElement, PGPractice, PGRecommendation, PGTimeline,
)
from app.modules.advisory.router import (
    cha_list_practices, cha_list_problems, cha_list_recommendations,
    cha_list_timelines,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


# ── Helpers ────────────────────────────────────────────────────────────────

async def _seed_two_pg_bundles(db):
    """Two PG recommendations under one client:
       - Fungal Diseases × AREA_WISE (DRAFT, 2 timelines, 2 practices)
       - Fungal Diseases × PLANT_WISE (ACTIVE, 1 timeline, 1 practice)
       - Sucking Pests × AREA_WISE (DRAFT, no timelines yet)
    Plus one global PG that mustn't appear (client_id=None).
    """
    client = await make_client(db)
    user = await make_user(db, name="SE")

    pg_fungal_aw = PGRecommendation(
        problem_group_cosh_id="pg:fungal_diseases", client_id=client.id,
        area_or_plant="AREA_WISE", status="DRAFT",
    )
    pg_fungal_pw = PGRecommendation(
        problem_group_cosh_id="pg:fungal_diseases", client_id=client.id,
        area_or_plant="PLANT_WISE", status="ACTIVE",
    )
    pg_sucking_aw = PGRecommendation(
        problem_group_cosh_id="pg:sucking_pests", client_id=client.id,
        area_or_plant="AREA_WISE", status="DRAFT",
    )
    pg_global = PGRecommendation(
        problem_group_cosh_id="pg:fungal_diseases", client_id=None,
        area_or_plant="AREA_WISE", status="ACTIVE",
    )
    db.add_all([pg_fungal_aw, pg_fungal_pw, pg_sucking_aw, pg_global])
    await db.flush()

    tl_fa1 = PGTimeline(
        pg_recommendation_id=pg_fungal_aw.id, name="W1",
        from_value=0, to_value=7,
    )
    tl_fa2 = PGTimeline(
        pg_recommendation_id=pg_fungal_aw.id, name="W2",
        from_value=7, to_value=14,
    )
    tl_fp1 = PGTimeline(
        pg_recommendation_id=pg_fungal_pw.id, name="P-W1",
        from_value=0, to_value=10,
    )
    db.add_all([tl_fa1, tl_fa2, tl_fp1])
    await db.flush()

    db.add_all([
        PGPractice(timeline_id=tl_fa1.id, l0_type="INPUT",
                   l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
                   display_order=0),
        PGPractice(timeline_id=tl_fa2.id, l0_type="INPUT",
                   l1_type="FERTILIZER", l2_type="MANURES",
                   display_order=0),
        PGPractice(timeline_id=tl_fp1.id, l0_type="INPUT",
                   l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
                   display_order=0),
    ])
    await db.commit()
    return client, user, pg_fungal_aw, pg_fungal_pw, pg_sucking_aw, tl_fa1


# ── Problems ───────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_cha_problems_returns_v1_hardcoded_list(db):
    client = await make_client(db)
    user = await make_user(db, name="SE")
    out = await cha_list_problems(client_id=client.id, db=db, current_user=user)
    cosh_ids = {p["cosh_id"] for p in out}
    # Sample of the V1 hardcoded list.
    assert "pg:fungal_diseases" in cosh_ids
    assert "pg:nutrient_deficiency" in cosh_ids
    # All entries default to no bundle started yet.
    assert all(p["area_wise_status"] is None and p["plant_wise_status"] is None
               for p in out)


@requires_docker
@pytest.mark.asyncio
async def test_cha_problems_carries_per_bundle_status(db):
    client, user, *_ = await _seed_two_pg_bundles(db)
    out = await cha_list_problems(client_id=client.id, db=db, current_user=user)
    by_id = {p["cosh_id"]: p for p in out}
    # Fungal has both bundles seeded — DRAFT area, ACTIVE plant.
    assert by_id["pg:fungal_diseases"]["area_wise_status"] == "DRAFT"
    assert by_id["pg:fungal_diseases"]["plant_wise_status"] == "ACTIVE"
    # Sucking has area-wise only.
    assert by_id["pg:sucking_pests"]["area_wise_status"] == "DRAFT"
    assert by_id["pg:sucking_pests"]["plant_wise_status"] is None
    # Untouched PGs stay None on both sides.
    assert by_id["pg:water_stress"]["area_wise_status"] is None


# ── Recommendations ────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_cha_recommendations_excludes_global(db):
    """Global PG rows (client_id=NULL) must not surface on a client's list."""
    client, user, *_ = await _seed_two_pg_bundles(db)
    out = await cha_list_recommendations(
        client_id=client.id, db=db, current_user=user,
    )
    # 3 client-local rows; the global row dropped.
    assert len(out) == 3
    assert all(r["problem_group_cosh_id"] for r in out)


@requires_docker
@pytest.mark.asyncio
async def test_cha_recommendations_filters_by_pg(db):
    client, user, *_ = await _seed_two_pg_bundles(db)
    out = await cha_list_recommendations(
        client_id=client.id, problem_group_cosh_id="pg:fungal_diseases",
        db=db, current_user=user,
    )
    assert {r["area_or_plant"] for r in out} == {"AREA_WISE", "PLANT_WISE"}
    assert {r["status"] for r in out} == {"DRAFT", "ACTIVE"}


@requires_docker
@pytest.mark.asyncio
async def test_cha_recommendations_filters_by_area_or_plant(db):
    client, user, *_ = await _seed_two_pg_bundles(db)
    out = await cha_list_recommendations(
        client_id=client.id, area_or_plant="PLANT_WISE",
        db=db, current_user=user,
    )
    assert len(out) == 1
    assert out[0]["area_or_plant"] == "PLANT_WISE"
    assert out[0]["timeline_count"] == 1


@requires_docker
@pytest.mark.asyncio
async def test_cha_recommendations_friendly_name(db):
    client, user, *_ = await _seed_two_pg_bundles(db)
    out = await cha_list_recommendations(
        client_id=client.id, db=db, current_user=user,
    )
    by_pg = {(r["problem_group_cosh_id"], r["area_or_plant"]): r for r in out}
    assert by_pg[("pg:fungal_diseases", "AREA_WISE")]["problem_group_name_en"] == "Fungal Diseases"
    assert by_pg[("pg:sucking_pests", "AREA_WISE")]["problem_group_name_en"] == "Sucking Pests"


# ── Timelines ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_cha_timelines_breadcrumb_and_practice_counts(db):
    client, user, *_ = await _seed_two_pg_bundles(db)
    out = await cha_list_timelines(client_id=client.id, db=db, current_user=user)
    # 2 timelines on Fungal-Area + 1 on Fungal-Plant = 3.
    assert len(out) == 3
    for t in out:
        assert t["problem_group_cosh_id"]
        assert t["area_or_plant"] in {"AREA_WISE", "PLANT_WISE"}
        assert t["practice_count"] >= 0
        assert t["recommendation_status"] in {"DRAFT", "ACTIVE", "INACTIVE"}


@requires_docker
@pytest.mark.asyncio
async def test_cha_timelines_filters_by_recommendation(db):
    client, user, _, pg_fungal_pw, *_ = await _seed_two_pg_bundles(db)
    out = await cha_list_timelines(
        client_id=client.id, recommendation_id=pg_fungal_pw.id,
        db=db, current_user=user,
    )
    assert len(out) == 1
    assert out[0]["area_or_plant"] == "PLANT_WISE"


# ── Practices ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_cha_practices_full_breadcrumb(db):
    client, user, *_ = await _seed_two_pg_bundles(db)
    out = await cha_list_practices(client_id=client.id, db=db, current_user=user)
    assert out["total"] == 3
    for p in out["items"]:
        assert p["problem_group_cosh_id"]
        assert p["timeline_id"]
        assert p["recommendation_id"]
        assert p["area_or_plant"] in {"AREA_WISE", "PLANT_WISE"}


@requires_docker
@pytest.mark.asyncio
async def test_cha_practices_l1_filter_cross_cutting(db):
    """Cross-cutting query: every PESTICIDE practice across the
    company's CHA recommendations regardless of PG / bundle."""
    client, user, *_ = await _seed_two_pg_bundles(db)
    out = await cha_list_practices(
        client_id=client.id, l1="PESTICIDE",
        db=db, current_user=user,
    )
    assert out["total"] == 2
    assert all(p["l1_type"] == "PESTICIDE" for p in out["items"])


@requires_docker
@pytest.mark.asyncio
async def test_cha_practices_brand_summary_when_present(db):
    client, user, *_, tl = await _seed_two_pg_bundles(db)
    practice = (await db.execute(
        __import__("sqlalchemy").select(PGPractice).where(
            PGPractice.timeline_id == tl.id,
        )
    )).scalar_one()
    db.add_all([
        PGElement(practice_id=practice.id, element_type="BRAND_NAME",
                  cosh_ref="brand:dithane-m45"),
        PGElement(practice_id=practice.id, element_type="DOSAGE",
                  value="2", unit_cosh_id="kg/ha"),
    ])
    await db.commit()

    out = await cha_list_practices(client_id=client.id, db=db, current_user=user)
    rich = next(p for p in out["items"] if p["id"] == practice.id)
    assert rich["brand_cosh_id"] == "brand:dithane-m45"
    assert rich["dosage_summary"] == "2 kg/ha"


@requires_docker
@pytest.mark.asyncio
async def test_cha_practices_excludes_qa_rooted_timelines(db):
    """A PGTimeline rooted at standard_response_id (Q&A pipe-3) sits
    in the same physical table but must NOT appear on the CHA
    practices list — it belongs to QA. Defensive filter."""
    from app.modules.farmpundit.router import create_standard_response
    from tests.factories import make_client_user

    client, user, *_ = await _seed_two_pg_bundles(db)
    # Make user a portal-member so QA endpoints accept them.
    await make_client_user(db, user=user, client=client)
    sr = await create_standard_response(
        client_id=client.id,
        data={"question_text": "Q?", "crop_cosh_id": None},
        db=db, current_user=user,
    )
    qa_tl = PGTimeline(
        standard_response_id=sr["id"], name="QA-W1",
        from_value=0, to_value=7,
    )
    db.add(qa_tl)
    await db.flush()
    db.add(PGPractice(
        timeline_id=qa_tl.id, l0_type="INPUT",
        l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
        display_order=0,
    ))
    await db.commit()

    out = await cha_list_practices(client_id=client.id, db=db, current_user=user)
    # The QA practice is not in the CHA list.
    assert all(p["recommendation_id"] for p in out["items"])
    assert out["total"] == 3  # the 3 CHA practices, no QA one
