"""Round 1 — element-authoring parity across CCA/CHA/QA (2026-05-09).

Backfills the 4C-i.D L2-validator wiring into the three Practice
authoring routes that were skipped at the time:

  • `add_qa_practice`               — Q&A library (L4-real Sub-batch 2)
  • `add_client_pg_practice`        — local PG recommendation
  • `add_sp_practice`               — local SP recommendation

For each route: one happy path (pesticide rule book satisfied →
practice + elements persisted) and one failure path (mandatory
element absent → 422 with stable `MISSING_MANDATORY` codes).

Pure-function validator coverage already lives in
`tests/test_l2_element_validator.py`; CCA + global-PG wiring in
`tests/test_phase_cca_step4ci_l2_validator_wiring.py`. This file
plugs the three remaining holes.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
Element,Practice,PGRecommendation,Timeline,
SPRecommendation,
)
from app.modules.advisory.router import (
add_client_pg_practice,add_qa_practice,add_qa_timeline,
add_sp_practice,
)
from app.modules.advisory.schemas import (
ElementIn,PGPracticeCreate,QAPracticeCreate,QATimelineCreate,
SPPracticeCreate,
)
from app.modules.farmpundit.router import create_standard_response
from app.modules.sync.models import CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import (
make_client,make_client_user,make_sp_recommendation,make_sp_timeline,
make_user,
)


# ── Shared cosh seeding ─────────────────────────────────────────────────────

async def _seed_pesticide_cosh(db) -> None:
    """Same seed shape as the 4C-i.D wiring tests — Imidacloprid /
    Confidor / Foliar Spray. Lets the cascade service walk live data."""
    rows = [
        CoshCoreItem(cosh_id="cn:imida",core_type="common_names_of_inputs",
translations={"en": "Imidacloprid"},status="active"),
        CoshCoreItem(cosh_id="am:foliar_spray",core_type="application_methods",
translations={"en": "Foliar spray"},status="active"),
        CoshCoreItem(cosh_id="du:ml_per_l",core_type="units_data",
translations={"en": "ml/L"},status="active"),
        CoshCoreItem(
cosh_id="brand:confidor",core_type="brand",
parent_cosh_id="cn:imida",
translations={"en": "Confidor"},
metadata_={
"manufacturer_name": "Bayer",
"formulation_cosh_id": "form:SC",
"ai_concentration": "17.8% SL",
},
status="active",
),
        CoshCoreItem(cosh_id="form:SC",core_type="formulations",
translations={"en": "SC"},status="active"),
    ]
    for r in rows:
        db.add(r)
    await db.flush()


def _full_pesticide_elements() -> list[ElementIn]:
    """Element list that satisfies CHEMICAL_PESTICIDES rule book."""
    return [
        ElementIn(element_type="COMMON_NAME", cosh_ref="cn:imida"),
        ElementIn(element_type="MANUFACTURER", cosh_ref="Bayer"),
        ElementIn(element_type="BRAND_NAME", cosh_ref="brand:confidor"),
        ElementIn(element_type="FORMULATION", cosh_ref="form:SC"),
        ElementIn(element_type="AI_CONCENTRATION", cosh_ref="17.8% SL"),
        ElementIn(element_type="APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        ElementIn(element_type="DOSAGE", value="0.5"),
        ElementIn(element_type="DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
    ]


# ── Q&A authoring ───────────────────────────────────────────────────────────

async def _seed_qa_timeline(db):
    client = await make_client(db)
    se = await make_user(db, name="SE")
    await make_client_user(db, user=se, client=client)
    sr = await create_standard_response(
client_id=client.id,
data={"question_text": "Q?","crop_cosh_id": None},
db=db,current_user=se,
)
    tl = await add_qa_timeline(
client_id=client.id,sr_id=sr["id"],
request=QATimelineCreate(name="W1",to_value=7),
        db=db, current_user=se,
    )
    return client, se, sr["id"], tl["id"]


@requires_docker
@pytest.mark.asyncio
async def test_qa_practice_happy_path_persists_elements(db):
    await _seed_pesticide_cosh(db)
    client, se, sr_id, tl_id = await _seed_qa_timeline(db)
    await db.commit()

    out = await add_qa_practice(
client_id=client.id,sr_id=sr_id,tl_id=tl_id,
request=QAPracticeCreate(
l0_type="INPUT",l1_type="PESTICIDE",
l2_type="CHEMICAL_PESTICIDES",
elements=_full_pesticide_elements(),
        ),
        db=db, current_user=se,
    )
    assert out["l2_type"] == "CHEMICAL_PESTICIDES"
    assert len(out["elements"]) == 8

    # Defence-in-depth: rows actually landed in pg_elements.
    rows = (await db.execute(
select(Element).where(Element.practice_id == out["id"])
    )).scalars().all()
    assert len(rows) == 8


@requires_docker
@pytest.mark.asyncio
async def test_qa_practice_missing_mandatory_returns_422(db):
    """Pre-Round-1 the QA route accepted ANY element list — including
    none — and silently produced a corrupt practice that volume calc
    would later fail on. This pins the new 422 behaviour."""
    await _seed_pesticide_cosh(db)
    client, se, sr_id, tl_id = await _seed_qa_timeline(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await add_qa_practice(
client_id=client.id,sr_id=sr_id,tl_id=tl_id,
request=QAPracticeCreate(
l0_type="INPUT",l1_type="PESTICIDE",
l2_type="CHEMICAL_PESTICIDES",
elements=[],# all mandatory missing
),
            db=db, current_user=se,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "l2_elements_validation_failed"
    missing = {e["field_name"] for e in exc.value.detail["errors"]
               if e["code"] == "MISSING_MANDATORY"}
    assert {"COMMON_NAME", "DOSAGE", "DOSAGE_UNIT"} <= missing


# ── Local PG authoring ──────────────────────────────────────────────────────

async def _seed_local_pg_timeline(db):
    client = await make_client(db)
    se = await make_user(db, name="SE-PG")
    await make_client_user(db, user=se, client=client)
    pg = PGRecommendation(
problem_group_cosh_id="pg:test",client_id=client.id,
area_or_plant="AREA_WISE",
)
    db.add(pg)
    await db.flush()
    tl = Timeline(
pg_recommendation_id=pg.id,name="PG-W1",
from_value=0,to_value=7,
)
    db.add(tl)
    await db.flush()
    return client, se, pg.id, tl.id


@requires_docker
@pytest.mark.asyncio
async def test_local_pg_practice_happy_path_persists_elements(db):
    """Pre-Round-1 add_client_pg_practice silently dropped the request's
    `elements` field. Round 1 wires it through; this verifies the rows
    land in pg_elements."""
    await _seed_pesticide_cosh(db)
    client, se, pg_id, tl_id = await _seed_local_pg_timeline(db)
    await db.commit()

    out = await add_client_pg_practice(
client_id=client.id,pg_id=pg_id,tl_id=tl_id,
request=PGPracticeCreate(
l0_type="INPUT",l1_type="PESTICIDE",
l2_type="CHEMICAL_PESTICIDES",
elements=_full_pesticide_elements(),
        ),
        db=db, current_user=se,
    )
    rows = (await db.execute(
select(Element).where(Element.practice_id == out.id)
    )).scalars().all()
    assert len(rows) == 8


@requires_docker
@pytest.mark.asyncio
async def test_local_pg_practice_missing_mandatory_returns_422(db):
    await _seed_pesticide_cosh(db)
    client, se, pg_id, tl_id = await _seed_local_pg_timeline(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await add_client_pg_practice(
client_id=client.id,pg_id=pg_id,tl_id=tl_id,
request=PGPracticeCreate(
l0_type="INPUT",l1_type="PESTICIDE",
l2_type="CHEMICAL_PESTICIDES",
elements=[],
),
            db=db, current_user=se,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "l2_elements_validation_failed"


# ── Local SP authoring ──────────────────────────────────────────────────────

async def _seed_sp_timeline(db):
    client = await make_client(db)
    se = await make_user(db, name="SE-SP")
    await make_client_user(db, user=se, client=client)
    sp = await make_sp_recommendation(db, client)
    tl = await make_sp_timeline(db, sp, name="SP-W1")
    return client, se, sp.id, tl.id


@requires_docker
@pytest.mark.asyncio
async def test_sp_practice_happy_path_persists_elements(db):
    """Pre-Round-1 add_sp_practice silently dropped the request's
    `elements` field. Round 1 wires it through; this verifies rows
    land in sp_elements (NOT pg_elements — separate table for SP)."""
    await _seed_pesticide_cosh(db)
    client, se, sp_id, tl_id = await _seed_sp_timeline(db)
    await db.commit()

    out = await add_sp_practice(
client_id=client.id,sp_id=sp_id,tl_id=tl_id,
request=SPPracticeCreate(
l0_type="INPUT",l1_type="PESTICIDE",
l2_type="CHEMICAL_PESTICIDES",
elements=_full_pesticide_elements(),
        ),
        db=db, current_user=se,
    )
    rows = (await db.execute(
select(Element).where(Element.practice_id == out.id)
    )).scalars().all()
    assert len(rows) == 8


@requires_docker
@pytest.mark.asyncio
async def test_sp_practice_missing_mandatory_returns_422(db):
    await _seed_pesticide_cosh(db)
    client, se, sp_id, tl_id = await _seed_sp_timeline(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await add_sp_practice(
client_id=client.id,sp_id=sp_id,tl_id=tl_id,
request=SPPracticeCreate(
l0_type="INPUT",l1_type="PESTICIDE",
l2_type="CHEMICAL_PESTICIDES",
elements=[],
),
            db=db, current_user=se,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "l2_elements_validation_failed"


# ── Defensive: l2_type=None bypasses validator on all three routes ──────────

@requires_docker
@pytest.mark.asyncio
async def test_l2_none_bypass_qa(db):
    """l2_type=None means legacy / l0-only practice — validator no-ops.
    The existing QA test_add_practice_with_elements_inline relies on
    this; pinning it explicitly per route guards against accidental
    reordering of the bypass check."""
    client, se, sr_id, tl_id = await _seed_qa_timeline(db)
    await db.commit()

    out = await add_qa_practice(
client_id=client.id,sr_id=sr_id,tl_id=tl_id,
request=QAPracticeCreate(l0_type="INPUT",elements=[]),
        db=db, current_user=se,
    )
    assert out["l0_type"] == "INPUT"
    assert out["elements"] == []


@requires_docker
@pytest.mark.asyncio
async def test_l2_none_bypass_local_pg(db):
    client, se, pg_id, tl_id = await _seed_local_pg_timeline(db)
    await db.commit()

    out = await add_client_pg_practice(
client_id=client.id,pg_id=pg_id,tl_id=tl_id,
request=PGPracticeCreate(l0_type="INPUT",elements=[]),
        db=db, current_user=se,
    )
    assert out.l0_type == "INPUT"


@requires_docker
@pytest.mark.asyncio
async def test_l2_none_bypass_sp(db):
    client, se, sp_id, tl_id = await _seed_sp_timeline(db)
    await db.commit()

    out = await add_sp_practice(
client_id=client.id,sp_id=sp_id,tl_id=tl_id,
request=SPPracticeCreate(l0_type="INPUT",elements=[]),
        db=db, current_user=se,
    )
    assert out.l0_type == "INPUT"
