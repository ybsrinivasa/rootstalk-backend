"""QA hub list endpoints (2026-05-10).

Tests the four-screen hub for the Q&A library (UCAT pipe-3):

  • qa_eligible_crops returns the CA's full shortlist (no
CHA-enabled intersection — different from SP).
  • qa_list_standard_responses chip-filters on crop, denormalised
    crop_name_en + timeline_count.
  • qa_list_timelines walks `pg_timelines` for rows with
    `standard_response_id IS NOT NULL` (excludes PG-rooted rows).
  • qa_list_practices: cross-timeline cross-cutting list.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.modules.advisory.models import (
Element,Practice,PGRecommendation,Timeline,
)
from app.modules.advisory.router import (
qa_eligible_crops,qa_list_practices,qa_list_standard_responses,
qa_list_timelines,
)
from app.modules.clients.models import ClientCrop
from app.modules.farmpundit.models import StandardResponse
from app.modules.sync.models import CoshCoreItem
from app.services.cosh_constants import COSH_BIOLOGICAL_NAMES_CORE
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


# ── eligible-crops ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_qa_eligible_crops_is_ca_shortlist(db):
    """Per user 2026-05-10: QA crops = CA's full shortlist. No CHA
    intersection (unlike SP). Soft-removed crops drop out."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    db.add_all([
CoshCoreItem(cosh_id="crop:tomato",core_type=COSH_BIOLOGICAL_NAMES_CORE,
translations={"en": "Tomato"},status="active"),
        CoshCoreItem(cosh_id="crop:onion",core_type=COSH_BIOLOGICAL_NAMES_CORE,
translations={"en": "Onion"},status="active"),
        CoshCoreItem(cosh_id="crop:papaya",core_type=COSH_BIOLOGICAL_NAMES_CORE,
translations={"en": "Papaya"},status="active"),
        ClientCrop(client_id=client.id, crop_cosh_id="crop:tomato"),
        ClientCrop(client_id=client.id, crop_cosh_id="crop:onion"),
        ClientCrop(
client_id=client.id,crop_cosh_id="crop:papaya",
removed_at=datetime.now(timezone.utc),  # CA removed
        ),
    ])
    await db.commit()

    out = await qa_eligible_crops(client_id=client.id, db=db, current_user=user)
    cosh_ids = {c["crop_cosh_id"] for c in out}
    assert cosh_ids == {"crop:tomato", "crop:onion"}
    # Friendly names came through, alphabetical order.
    assert [c["name_en"] for c in out] == ["Onion", "Tomato"]


# ── standard-responses (denormalised) ──────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_qa_list_standard_responses_filter_by_crop(db):
    client = await make_client(db)
    user = await make_user(db, name="SE")
    db.add(CoshCoreItem(cosh_id="crop:tomato",core_type=COSH_BIOLOGICAL_NAMES_CORE,
translations={"en": "Tomato"},status="active"))
    db.add_all([
StandardResponse(
client_id=client.id,crop_cosh_id="crop:tomato",
question_text="When should I irrigate tomato?",
),
        StandardResponse(
client_id=client.id,crop_cosh_id="crop:onion",
question_text="When should I irrigate onion?",
),
        StandardResponse(
client_id=client.id,crop_cosh_id=None,
question_text="What's the best fertiliser ratio?",
),
    ])
    await db.commit()

    # Filter to tomato.
    out = await qa_list_standard_responses(
client_id=client.id,crop_cosh_id="crop:tomato",
db=db,current_user=user,
)
    assert len(out) == 1
    assert out[0]["crop_name_en"] == "Tomato"
    assert "irrigate tomato" in out[0]["question_text"]

    # Filter to crop-agnostic via the special sentinel.
    out_a = await qa_list_standard_responses(
client_id=client.id,crop_cosh_id="__AGNOSTIC__",
db=db,current_user=user,
)
    assert len(out_a) == 1
    assert out_a[0]["crop_cosh_id"] is None
    assert out_a[0]["crop_name_en"] is None


@requires_docker
@pytest.mark.asyncio
async def test_qa_list_standard_responses_carries_timeline_count(db):
    client = await make_client(db)
    user = await make_user(db, name="SE")
    sr = StandardResponse(
client_id=client.id,crop_cosh_id="crop:tomato",
question_text="Q?",
)
    db.add(sr); await db.flush()
    db.add_all([
Timeline(standard_response_id=sr.id,name="W1",from_value=0,to_value=7),
        Timeline(standard_response_id=sr.id, name="W2", from_value=7, to_value=14),
    ])
    await db.commit()

    out = await qa_list_standard_responses(
client_id=client.id,db=db,current_user=user,
)
    assert len(out) == 1
    assert out[0]["timeline_count"] == 2


# ── timelines (cross-SR) ───────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_qa_timelines_excludes_pg_rooted_rows(db):
    """`pg_timelines` is polymorphic — both PG and QA timelines live
    in it. Defensive filter: only standard_response_id IS NOT NULL
    rows surface here."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    sr = StandardResponse(
client_id=client.id,crop_cosh_id="crop:tomato",
question_text="QA Q?",
)
    db.add(sr); await db.flush()
    db.add(Timeline(standard_response_id=sr.id, name="QA-W1", from_value=0, to_value=7))
    # PG-rooted timeline that MUST NOT appear in QA list.
    pg = PGRecommendation(
problem_group_cosh_id="pg:fungal_diseases",client_id=client.id,
area_or_plant="AREA_WISE",status="DRAFT",
)
    db.add(pg); await db.flush()
    db.add(Timeline(pg_recommendation_id=pg.id, name="PG-W1", from_value=0, to_value=7))
    await db.commit()

    out = await qa_list_timelines(client_id=client.id, db=db, current_user=user)
    assert len(out) == 1
    assert out[0]["name"] == "QA-W1"
    assert out[0]["standard_response_id"] == sr.id


@requires_docker
@pytest.mark.asyncio
async def test_qa_timelines_chip_filter_by_sr(db):
    client = await make_client(db)
    user = await make_user(db, name="SE")
    sr1 = StandardResponse(
client_id=client.id,crop_cosh_id="crop:tomato",question_text="Q1?",
)
    sr2 = StandardResponse(
client_id=client.id,crop_cosh_id="crop:tomato",question_text="Q2?",
)
    db.add_all([sr1, sr2]); await db.flush()
    db.add_all([
Timeline(standard_response_id=sr1.id,name="A",from_value=0,to_value=7),
        Timeline(standard_response_id=sr2.id, name="B", from_value=0, to_value=7),
    ])
    await db.commit()

    out = await qa_list_timelines(
client_id=client.id,standard_response_id=sr1.id,
db=db,current_user=user,
)
    assert len(out) == 1
    assert out[0]["name"] == "A"


# ── practices (cross-timeline) ─────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_qa_practices_breadcrumb(db):
    client = await make_client(db)
    user = await make_user(db, name="SE")
    db.add(CoshCoreItem(cosh_id="crop:tomato",core_type=COSH_BIOLOGICAL_NAMES_CORE,
translations={"en": "Tomato"},status="active"))
    sr = StandardResponse(
client_id=client.id,crop_cosh_id="crop:tomato",
question_text="When should I apply Mancozeb?",
)
    db.add(sr); await db.flush()
    tl = Timeline(standard_response_id=sr.id, name="W1", from_value=0, to_value=7)
    db.add(tl); await db.flush()
    practice = Practice(
timeline_id=tl.id,l0_type="INPUT",
l1_type="PESTICIDE",l2_type="CHEMICAL_PESTICIDES",
)
    db.add(practice); await db.flush()
    db.add_all([
Element(practice_id=practice.id,element_type="BRAND_NAME",
cosh_ref="brand:dithane-m45"),
        Element(practice_id=practice.id,element_type="DOSAGE",
value="2",unit_cosh_id="kg/ha"),
    ])
    await db.commit()

    out = await qa_list_practices(client_id=client.id, db=db, current_user=user)
    assert out["total"] == 1
    p = out["items"][0]
    assert p["crop_name_en"] == "Tomato"
    assert "Mancozeb" in p["question_text"]
    assert p["brand_cosh_id"] == "brand:dithane-m45"
    assert p["dosage_summary"] == "2 kg/ha"


@requires_docker
@pytest.mark.asyncio
async def test_qa_practices_excludes_pg_rooted(db):
    """Cross-cutting filter via the polymorphic table — PG practice
    must not leak into QA results, same as the timeline path."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    pg = PGRecommendation(
problem_group_cosh_id="pg:fungal_diseases",client_id=client.id,
area_or_plant="AREA_WISE",status="DRAFT",
)
    db.add(pg); await db.flush()
    pg_tl = Timeline(pg_recommendation_id=pg.id, name="PG-W1", from_value=0, to_value=7)
    db.add(pg_tl); await db.flush()
    db.add(Practice(
timeline_id=pg_tl.id,l0_type="INPUT",
l1_type="PESTICIDE",l2_type="CHEMICAL_PESTICIDES",
))
    await db.commit()

    out = await qa_list_practices(client_id=client.id, db=db, current_user=user)
    assert out["total"] == 0
