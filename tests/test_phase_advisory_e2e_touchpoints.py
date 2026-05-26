"""End-to-end advisory touch-point integration tests (2026-05-10).

Walks the seam from authoring to the farmer's `/farmer/advisory/today`
for each UCAT pipe. The pure-trigger tests in `test_phase_qa_trigger.py`
confirm `TriggeredCHAEntry` rows get created; this file confirms those
rows actually surface as timelines on the farmer's app, with the
right window anchoring, the right bundle selection, and dedup
behaviour across pipes.

Coverage:
  1. QA pipe — Pundit picks SR → /today renders QA timeline (source="QA")
  2. QA window anchors to triggered_at, NOT crop_start_date
  3. Pundit-CHA (PG path) — `respond_to_query` with problem_cosh_id →
     resolver → /today renders cha-pg-* (source="CHA")
  4. Pundit-CHA (SP path) — same flow, specific_problem branch
  5. Bundle correctness — area-wise crop picks the area-wise PG bundle
  6. Bundle correctness — plant-wise crop picks the plant-wise PG bundle
  7. Cross-pipe dedup — CCA's earlier urea suppresses an overlapping
     QA urea practice (suppressed_count flows correctly across pipes)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select

from app.modules.advisory.models import (
Element,Practice,PGRecommendation,Timeline,PracticeL0,
TimelineFromType,
)
from app.modules.farmpundit.models import (
ClientFarmPundit,FarmPunditProfile,PunditRole,Query,QueryStatus,
StandardResponse,
)
from app.modules.farmpundit.router import (
create_standard_response,publish_standard_response,respond_to_query,
)
from app.modules.subscriptions.models import (
Subscription,SubscriptionStatus,TriggeredCHAEntry,
)
from app.modules.subscriptions.router import get_today_advisory
from app.modules.sync.models import CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import (
make_client,make_client_user,make_crop_reference,make_element,
make_package,make_pg_element,make_pg_practice,make_practice,
make_subscription,make_timeline,make_user,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


COMMON_NAME_UREA = "cosh:input:urea"


async def _se_for(db, *, client):
    se = await make_user(db, name="SE")
    await make_client_user(db, user=se, client=client)
    return se


async def _seed_pundit_with_query(db, *, client, farmer, sub, title="Pest issue"):
    """Pundit + ClientFarmPundit + Query held by that pundit. Returns
    (pundit_user, profile, query). Mirrors the helper in
    test_phase_qa_trigger.py — kept inline because that helper isn't
    in tests/factories yet."""
    user = await make_user(db, name="Pundit")
    profile = FarmPunditProfile(user_id=user.id, declaration_accepted=True)
    db.add(profile)
    await db.flush()
    db.add(ClientFarmPundit(
client_id=client.id,pundit_id=profile.id,
role=PunditRole.PRIMARY,status="ACTIVE",round_robin_sequence=1,
))
    query = Query(
farmer_user_id=farmer.id,subscription_id=sub.id,client_id=client.id,
title=title,severity="MODERATE",status=QueryStatus.NEW,
current_holder_id=profile.id,
expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(query)
    await db.flush()
    return user, profile, query


async def _add_qa_timeline_to_sr(
db,*,sr_id: str,name: str = "QA-TL",
from_value: int = 0,to_value: int = 14,
l1_type: str = "PESTICIDE",l2_type: str = "NEEM_OIL",
common_name_cosh: str | None = None,
):
    """Author a Timeline+Practice+Element rooted at a Standard Response.
    `make_pg_timeline` only supports pg_recommendation_id parents; QA
    timelines need standard_response_id + the polymorphic CHECK is
    enforced by the DB."""
    tl = Timeline(
standard_response_id=sr_id,name=f"{name}-{uuid.uuid4().hex[:6]}",
        from_type="DAYS_AFTER_DETECTION",
        from_value=from_value, to_value=to_value,
    )
    db.add(tl)
    await db.flush()
    p = await make_pg_practice(db, tl, l1_type=l1_type)
    p.l2_type = l2_type
    if common_name_cosh:
        await make_pg_element(
db,p,element_type="common_name",value=None,
cosh_ref=common_name_cosh,
)
    else:
        await make_pg_element(db, p, element_type="DOSAGE", value="2.5")
    await db.flush()
    return tl, p


async def _add_pg_timeline(
db,*,pg_rec_id: str,name: str = "PG-TL",
from_value: int = 0,to_value: int = 14,
l2_type: str = "MANCOZEB",
):
    tl = Timeline(
pg_recommendation_id=pg_rec_id,name=f"{name}-{uuid.uuid4().hex[:6]}",
        from_type="DAYS_AFTER_DETECTION",
        from_value=from_value, to_value=to_value,
    )
    db.add(tl)
    await db.flush()
    p = await make_pg_practice(db, tl, l1_type="PESTICIDE")
    p.l2_type = l2_type
    await make_pg_element(db, p, element_type="DOSAGE", value="2.0")
    await db.flush()
    return tl, p


# ── Test 1: QA timeline reaches farmer's /today ──────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_qa_timeline_reaches_farmer_today(db):
    """SE authors a Standard Response with a Timeline. Pundit picks that
    SR while answering a farmer's query. The farmer's /today must
    surface the QA timeline keyed `cha-qa-{tl.id}` with source="QA".
    """
    client = await make_client(db)
    se = await _se_for(db, client=client)
    farmer = await make_user(db, name="Farmer")
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    sr = await create_standard_response(
client_id=client.id,
data={"question_text": "How to control aphids?"},
db=db,current_user=se,
)
    # Pundit can only pick ACTIVE SRs; respond_to_query's trigger
    # filters non-ACTIVE rows out as a defence-in-depth gate.
    await publish_standard_response(
client_id=client.id,sr_id=sr["id"],db=db,current_user=se,
)
    qa_tl, _ = await _add_qa_timeline_to_sr(
db,sr_id=sr["id"],name="QA-Aphids",
)
    pundit, _, query = await _seed_pundit_with_query(
db,client=client,farmer=farmer,sub=sub,
)
    await db.commit()

    await respond_to_query(
query_id=query.id,
data={"standard_response_id": sr["id"]},
db=db,current_user=pundit,
)

    out = await get_today_advisory(db=db, current_user=farmer)
    assert len(out) == 1
    timelines_by_id = {t["id"]: t for t in out[0]["timelines"]}
    qa_id = f"cha-qa-{qa_tl.id}"
    assert qa_id in timelines_by_id, (
f"QA timeline {qa_id} must appear on /today after Pundit responds. "
f"Got: {list(timelines_by_id)}"
    )
    rt = timelines_by_id[qa_id]
    assert rt["source"] == "QA", "Pundit-origin marker for the PWA"
    # QA window is anchored at the triggered_at date; covers today.
    assert rt["from_date"] <= datetime.now(timezone.utc).date().isoformat()
    assert any(
p["l2_type"] == "NEEM_OIL" for p in rt["practices"]
), "Authored QA practice must be present"

    # The PWA renders the problem name under the date band and uses
    # triggered_at to float fresh CHA/QA timelines to the top.
    assert rt.get("problem_name"), (
        f"QA timeline must expose problem_name; got: {rt.get('problem_name')!r}"
    )
    assert rt.get("triggered_at"), (
        f"QA timeline must expose triggered_at; got: {rt.get('triggered_at')!r}"
    )


# ── Test 2: QA window anchors to triggered_at, not crop_start ────────────────

@requires_docker
@pytest.mark.asyncio
async def test_qa_window_anchors_to_triggered_at_not_crop_start(db):
    """Like the existing CHA-window test but for QA: shifting
    crop_start_date AFTER the QA entry exists must not move the QA
    window. The QA window is anchored at triggered_at (Q&A is rooted
in a question,not the crop calendar)."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    farmer = await make_user(db, name="Farmer")
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    # Need an active CCA timeline so the today-route doesn't short-circuit
    # before reaching the CHA loop. Mirrors test_today_dbs_timeline_uses_*.
    cca_tl = await make_timeline(
db,pkg,name="CCA_FOR_QA_TEST",
from_type=TimelineFromType.DAS,from_value=0,to_value=120,
)
    await make_practice(db, cca_tl)

    sr = await create_standard_response(
client_id=client.id,
data={"question_text": "Bollworm control?"},
db=db,current_user=se,
)
    qa_tl, _ = await _add_qa_timeline_to_sr(
db,sr_id=sr["id"],name="QA-Bollworm",to_value=14,
)
    triggered_at_dt = datetime.now(timezone.utc) - timedelta(days=3)
    db.add(TriggeredCHAEntry(
subscription_id=sub.id,
farmer_user_id=farmer.id,
client_id=client.id,
problem_cosh_id=None,
recommendation_type="QA",
recommendation_id=sr["id"],
triggered_by="QUERY",
triggered_at=triggered_at_dt,
status="ACTIVE",
problem_name="Bollworm control?",
))
    await db.commit()

    qa_id = f"cha-qa-{qa_tl.id}"
    out1 = await get_today_advisory(db=db, current_user=farmer)
    rt1 = next((t for t in out1[0]["timelines"] if t["id"] == qa_id), None)
    assert rt1 is not None
    qa_from_1, qa_to_1 = rt1["from_date"], rt1["to_date"]
    expected_from = triggered_at_dt.date().isoformat()
    expected_to = (triggered_at_dt.date() + timedelta(days=14)).isoformat()
    assert qa_from_1 == expected_from
    assert qa_to_1 == expected_to

    # Shift crop_start way back. CCA windows would shift; QA must not.
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=90)
    await db.commit()

    out2 = await get_today_advisory(db=db, current_user=farmer)
    rt2 = next((t for t in out2[0]["timelines"] if t["id"] == qa_id), None)
    assert rt2 is not None, "QA still in window after crop_start shift"
    assert rt2["from_date"] == qa_from_1, (
"QA from_date must NOT shift with crop_start_date — "
"it's anchored at triggered_at"
)
    assert rt2["to_date"] == qa_to_1


# ── Test 3: Pundit-CHA (PG path) reaches farmer's /today ─────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pundit_pest_problem_via_pg_reaches_farmer_today(db):
    """Pundit responds with a problem_cosh_id that's a bare problem_group.
    `respond_to_query` calls `_trigger_cha_for_query` → resolver picks
    the client's PG recommendation (no SP exists) → /today shows
    `cha-pg-{tl.id}` with source="CHA"."""
    client = await make_client(db)
    farmer = await make_user(db, name="Farmer")
    await make_crop_reference(
db,"crop:tomato",name="Tomato",measure="AREA_WISE",
)
    pkg = await make_package(db, client, crop_cosh_id="crop:tomato")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    pg_rec = PGRecommendation(
problem_group_cosh_id="pg:fungal_diseases",client_id=client.id,
area_or_plant="AREA_WISE",status="ACTIVE",
)
    db.add(pg_rec)
    await db.flush()
    pg_tl, _ = await _add_pg_timeline(
db,pg_rec_id=pg_rec.id,l2_type="MANCOZEB",
)

    pundit, _, query = await _seed_pundit_with_query(
db,client=client,farmer=farmer,sub=sub,
)
    await db.commit()

    await respond_to_query(
query_id=query.id,
data={"problem_cosh_id": "pg:fungal_diseases"},
db=db,current_user=pundit,
)

    out = await get_today_advisory(db=db, current_user=farmer)
    timelines_by_id = {t["id"]: t for t in out[0]["timelines"]}
    cha_id = f"cha-pg-{pg_tl.id}"
    assert cha_id in timelines_by_id, (
f"PG timeline must reach /today via the resolver → "
f"TriggeredCHAEntry → today-route seam. "
f"Got: {list(timelines_by_id)}"
    )
    rt = timelines_by_id[cha_id]
    assert rt["source"] == "CHA"
    assert any(p["l2_type"] == "MANCOZEB" for p in rt["practices"])


# ── Test 4: Pundit-CHA (SP path) reaches farmer's /today ─────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pundit_pest_problem_via_sp_reaches_farmer_today(db):
    """Mirror of test 3 but for the SP branch. Cosh entry tags the
    problem as a specific_problem with a parent PG; client has both an
    SP recommendation (more specific) and a PG recommendation.
    Resolver picks SP per spec §8.7 priority. /today shows
    `cha-sp-{tl.id}`."""
    from app.modules.advisory.models import (
SPRecommendation,Timeline,
)
    from tests.factories import make_sp_element, make_sp_practice

    client = await make_client(db)
    farmer = await make_user(db, name="Farmer")
    await make_crop_reference(
db,"crop:tomato",name="Tomato",measure="AREA_WISE",
)
    pkg = await make_package(db, client, crop_cosh_id="crop:tomato")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    db.add(CoshCoreItem(
cosh_id="sp:tomato_late_blight",core_type="specific_problem",
parent_cosh_id="pg:fungal_diseases",
translations={"en": "Tomato Late Blight"},status="active",
))
    sp_rec = SPRecommendation(
specific_problem_cosh_id="sp:tomato_late_blight",
client_id=client.id,crop_cosh_id="crop:tomato",status="ACTIVE",
)
    db.add(sp_rec)
    await db.flush()
    sp_tl = Timeline(
sp_recommendation_id=sp_rec.id,name=f"SP-{uuid.uuid4().hex[:6]}",
        from_value=0, to_value=10,
    )
    db.add(sp_tl)
    await db.flush()
    sp_p = await make_sp_practice(db, sp_tl, l1_type="PESTICIDE")
    sp_p.l2_type = "COPPER_OXYCHLORIDE"
    await make_sp_element(db, sp_p, element_type="DOSAGE", value="3.0")

    pundit, _, query = await _seed_pundit_with_query(
db,client=client,farmer=farmer,sub=sub,
)
    await db.commit()

    await respond_to_query(
query_id=query.id,
data={"problem_cosh_id": "sp:tomato_late_blight"},
db=db,current_user=pundit,
)

    out = await get_today_advisory(db=db, current_user=farmer)
    timelines_by_id = {t["id"]: t for t in out[0]["timelines"]}
    cha_id = f"cha-sp-{sp_tl.id}"
    assert cha_id in timelines_by_id, (
"SP path must win over PG path when a Cosh-tagged "
"specific_problem has a client SP recommendation"
)
    rt = timelines_by_id[cha_id]
    assert rt["source"] == "CHA"
    assert any(p["l2_type"] == "COPPER_OXYCHLORIDE" for p in rt["practices"])


# ── Test 5: bundle correctness — area-wise crop picks area-wise bundle ──────

@requires_docker
@pytest.mark.asyncio
async def test_today_picks_area_wise_pg_bundle_for_area_wise_crop_e2e(db):
    """E2E of the resolver's bundle filter: client has BOTH bundles
    (area-wise + plant-wise) for the same problem_group. The farmer's
    crop is AREA_WISE. Only the area-wise bundle's timelines must
    surface in /today — the plant-wise bundle is invisible."""
    client = await make_client(db)
    farmer = await make_user(db, name="Farmer")
    await make_crop_reference(
db,"crop:tomato",name="Tomato",measure="AREA_WISE",
)
    pkg = await make_package(db, client, crop_cosh_id="crop:tomato")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    pg_aw = PGRecommendation(
problem_group_cosh_id="pg:fungal_diseases",client_id=client.id,
area_or_plant="AREA_WISE",status="ACTIVE",
)
    pg_pw = PGRecommendation(
problem_group_cosh_id="pg:fungal_diseases",client_id=client.id,
area_or_plant="PLANT_WISE",status="ACTIVE",
)
    db.add_all([pg_aw, pg_pw])
    await db.flush()
    aw_tl, _ = await _add_pg_timeline(
db,pg_rec_id=pg_aw.id,name="AW",l2_type="AW_FUNGICIDE",
)
    pw_tl, _ = await _add_pg_timeline(
db,pg_rec_id=pg_pw.id,name="PW",l2_type="PW_FUNGICIDE",
)

    pundit, _, query = await _seed_pundit_with_query(
db,client=client,farmer=farmer,sub=sub,
)
    await db.commit()

    await respond_to_query(
query_id=query.id,
data={"problem_cosh_id": "pg:fungal_diseases"},
db=db,current_user=pundit,
)

    out = await get_today_advisory(db=db, current_user=farmer)
    timelines_by_id = {t["id"]: t for t in out[0]["timelines"]}
    assert f"cha-pg-{aw_tl.id}" in timelines_by_id, (
"Area-wise bundle must reach the area-wise crop's farmer"
)
    assert f"cha-pg-{pw_tl.id}" not in timelines_by_id, (
"Plant-wise bundle must NOT reach an area-wise crop's farmer"
)


# ── Test 6: bundle correctness — plant-wise crop picks plant-wise bundle ────

@requires_docker
@pytest.mark.asyncio
async def test_today_picks_plant_wise_pg_bundle_for_plant_wise_crop_e2e(db):
    """Mirror of test 5 — plant-wise crop, plant-wise bundle wins."""
    client = await make_client(db)
    farmer = await make_user(db, name="Farmer")
    await make_crop_reference(
db,"crop:apple",name="Apple",measure="PLANT_WISE",
)
    pkg = await make_package(db, client, crop_cosh_id="crop:apple")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    pg_aw = PGRecommendation(
problem_group_cosh_id="pg:fungal_diseases",client_id=client.id,
area_or_plant="AREA_WISE",status="ACTIVE",
)
    pg_pw = PGRecommendation(
problem_group_cosh_id="pg:fungal_diseases",client_id=client.id,
area_or_plant="PLANT_WISE",status="ACTIVE",
)
    db.add_all([pg_aw, pg_pw])
    await db.flush()
    aw_tl, _ = await _add_pg_timeline(
db,pg_rec_id=pg_aw.id,name="AW",l2_type="AW_FUNGICIDE",
)
    pw_tl, _ = await _add_pg_timeline(
db,pg_rec_id=pg_pw.id,name="PW",l2_type="PW_FUNGICIDE",
)

    pundit, _, query = await _seed_pundit_with_query(
db,client=client,farmer=farmer,sub=sub,
)
    await db.commit()

    await respond_to_query(
query_id=query.id,
data={"problem_cosh_id": "pg:fungal_diseases"},
db=db,current_user=pundit,
)

    out = await get_today_advisory(db=db, current_user=farmer)
    timelines_by_id = {t["id"]: t for t in out[0]["timelines"]}
    assert f"cha-pg-{pw_tl.id}" in timelines_by_id
    assert f"cha-pg-{aw_tl.id}" not in timelines_by_id


# ── Test 7: cross-pipe dedup — CCA suppresses overlapping QA input ──────────

@requires_docker
@pytest.mark.asyncio
async def test_cca_input_suppresses_overlapping_qa_input(db):
    """BL-03 cross-pipe: a CCA timeline (earlier from_date,anchored at
crop_start) and a QA timeline (anchored at triggered_at) both
    reference Urea via cosh:input:urea. CCA governs by from_date —
    QA's urea must be suppressed. Companion to existing
    `test_cca_governs_overlapping_cha_sp_input` but for the QA pipe."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    farmer = await make_user(db, name="Farmer")
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=20)
    await db.commit()

    # CCA TL: DAS 0..30, from_date = today-20.
    cca_tl = await make_timeline(
db,pkg,name="CCA_TL",
from_type=TimelineFromType.DAS,from_value=0,to_value=30,
)
    cca_p = await make_practice(
db,cca_tl,l0=PracticeL0.INPUT,l1="FERTILIZER",l2="UREA",
)
    await make_element(
db,cca_p,element_type="common_name",value=None,
unit_cosh_id=None,cosh_ref=COMMON_NAME_UREA,
)

    # QA TL: PG-table row with standard_response_id; triggered 2 days ago,
    # window 0..14 → from_date = today-2. Active and overlapping.
    sr = await create_standard_response(
client_id=client.id,
data={"question_text": "Should I apply nitrogen?"},
db=db,current_user=se,
)
    qa_tl, qa_p = await _add_qa_timeline_to_sr(
db,sr_id=sr["id"],name="QA-N",
l1_type="FERTILIZER",l2_type="UREA",
common_name_cosh=COMMON_NAME_UREA,
)
    triggered_at_dt = datetime.now(timezone.utc) - timedelta(days=2)
    db.add(TriggeredCHAEntry(
subscription_id=sub.id,
farmer_user_id=farmer.id,
client_id=client.id,
problem_cosh_id=None,
recommendation_type="QA",
recommendation_id=sr["id"],
triggered_by="QUERY",
triggered_at=triggered_at_dt,
status="ACTIVE",
problem_name="Should I apply nitrogen?",
))
    await db.commit()

    out = await get_today_advisory(db=db, current_user=farmer)
    timelines_by_id = {t["id"]: t for t in out[0]["timelines"]}
    qa_id = f"cha-qa-{qa_tl.id}"
    assert cca_tl.id in timelines_by_id
    assert qa_id in timelines_by_id

    rt_cca = timelines_by_id[cca_tl.id]
    rt_qa = timelines_by_id[qa_id]

    assert any(p["l2_type"] == "UREA" for p in rt_cca["practices"]), (
"CCA owns Urea (earlier from_date)"
    )
    assert not any(p["l2_type"] == "UREA" for p in rt_qa["practices"]), (
"QA's Urea must be suppressed by the earlier-from_date CCA — "
"BL-03 dedup is pipe-agnostic"
)
    assert rt_qa["suppressed_count"] == 1
