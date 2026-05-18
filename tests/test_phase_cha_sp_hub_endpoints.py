"""CHA-SP hub list endpoints (2026-05-10).

Tests the SP-side mirror of the PG hub:

  • cha_sp_eligible_crops returns ClientCrop ∩ CropHealthCrop (the
crops where SE may author SP recommendations).
  • cha_sp_specific_problems returns the V1 hardcoded list-per-crop
    with `existing` flag for problems the SE has already authored.
  • cha_sp_list_recommendations chip-filters on crop, status.
  • cha_sp_list_timelines chip-filters on crop, recommendation.
  • cha_sp_list_practices: cross-timeline cross-cutting list.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.modules.advisory.models import (
Element,Practice,SPRecommendation,Timeline,
)
from app.modules.advisory.router import (
cha_sp_eligible_crops,cha_sp_list_practices,
cha_sp_list_recommendations,cha_sp_list_timelines,
cha_sp_specific_problems,
)
from app.modules.clients.models import ClientCrop
from app.modules.sync.models import CoshCoreItem, CropHealthCrop
from app.services.cosh_constants import COSH_BIOLOGICAL_NAMES_CORE
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


# ── Helpers ────────────────────────────────────────────────────────────────

async def _seed_eligible_crops(db, *, client):
    """Three crops in CA's set; two of them ALSO in the CM-CHA set.
    Returns (eligible cosh_ids, name → cosh_id map)."""
    db.add_all([
# Three biological_names with friendly Cosh-side names.
CoshCoreItem(cosh_id="crop:tomato",core_type=COSH_BIOLOGICAL_NAMES_CORE,
translations={"en": "Tomato"},status="active"),
        CoshCoreItem(cosh_id="crop:onion",core_type=COSH_BIOLOGICAL_NAMES_CORE,
translations={"en": "Onion"},status="active"),
        CoshCoreItem(cosh_id="crop:papaya",core_type=COSH_BIOLOGICAL_NAMES_CORE,
translations={"en": "Papaya"},status="active"),
        # CA shortlist for this client — all three.
        ClientCrop(client_id=client.id, crop_cosh_id="crop:tomato"),
        ClientCrop(client_id=client.id, crop_cosh_id="crop:onion"),
        ClientCrop(client_id=client.id, crop_cosh_id="crop:papaya"),
        # CM enabled CHA on tomato + onion only. Papaya not enabled.
        CropHealthCrop(crop_cosh_id="crop:tomato", status="ACTIVE"),
        CropHealthCrop(crop_cosh_id="crop:onion", status="ACTIVE"),
        # crop:cotton is CHA-enabled but NOT on this CA's shortlist.
        CropHealthCrop(crop_cosh_id="crop:cotton", status="ACTIVE"),
    ])
    await db.commit()


# ── eligible-crops ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_eligible_crops_returns_intersection(db):
    client = await make_client(db)
    user = await make_user(db, name="SE")
    await _seed_eligible_crops(db, client=client)

    out = await cha_sp_eligible_crops(client_id=client.id, db=db, current_user=user)
    cosh_ids = {c["crop_cosh_id"] for c in out}
    assert cosh_ids == {"crop:tomato", "crop:onion"}
    # Friendly names came through.
    by_id = {c["crop_cosh_id"]: c["name_en"] for c in out}
    assert by_id["crop:tomato"] == "Tomato"
    assert by_id["crop:onion"] == "Onion"


@requires_docker
@pytest.mark.asyncio
async def test_eligible_crops_excludes_soft_removed_client_crop(db):
    """CA soft-removed Tomato; even if CM still has it CHA-enabled,
    Tomato shouldn't appear for this client."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    await _seed_eligible_crops(db, client=client)
    from sqlalchemy import select as _sel
    tomato = (await db.execute(
_sel(ClientCrop).where(
ClientCrop.client_id == client.id,
ClientCrop.crop_cosh_id == "crop:tomato",
)
    )).scalar_one()
    tomato.removed_at = datetime.now(timezone.utc)
    await db.commit()

    out = await cha_sp_eligible_crops(client_id=client.id, db=db, current_user=user)
    cosh_ids = {c["crop_cosh_id"] for c in out}
    assert "crop:tomato" not in cosh_ids
    assert "crop:onion" in cosh_ids


@requires_docker
@pytest.mark.asyncio
async def test_eligible_crops_excludes_inactive_crop_health(db):
    """CM disabled CHA on Onion; Onion drops out for everyone."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    await _seed_eligible_crops(db, client=client)
    from sqlalchemy import select as _sel
    onion = (await db.execute(
_sel(CropHealthCrop).where(CropHealthCrop.crop_cosh_id == "crop:onion")
    )).scalar_one()
    onion.status = "INACTIVE"
    await db.commit()

    out = await cha_sp_eligible_crops(client_id=client.id, db=db, current_user=user)
    cosh_ids = {c["crop_cosh_id"] for c in out}
    assert cosh_ids == {"crop:tomato"}


# ── specific-problems ──────────────────────────────────────────────────────

async def _seed_sp_pg_crops_tomato(db):
    """Seed the minimum Cosh data for two SPs on a Tomato crop:
    biological_names Core entries + an sp_pg_crops Connect row each.
    Returns the crop_cosh_id used in the seed."""
    from app.modules.sync.models import CoshConnectRow
    from app.services.cosh_constants import (
        COSH_BIOLOGICAL_NAMES_CORE, COSH_PROBLEM_GROUPS_CORE,
        COSH_SP_PG_CROPS_CONNECT, SPPC_POS_CROP, SPPC_POS_PG, SPPC_POS_SP,
    )
    crop_id = "biological_name:solanum_lycopersicum"
    pg_id = "problem_group:fungal_diseases"
    sps = [
        ("biological_name:phytophthora_infestans", "Late Blight"),
        ("biological_name:helicoverpa_armigera",   "Fruit Borer"),
    ]
    db.add(CoshCoreItem(
        cosh_id=crop_id, core_type=COSH_BIOLOGICAL_NAMES_CORE,
        status="active", translations={"en": "Tomato"},
    ))
    db.add(CoshCoreItem(
        cosh_id=pg_id, core_type=COSH_PROBLEM_GROUPS_CORE,
        status="active", translations={"en": "Fungal Diseases"},
    ))
    for sp_id, en in sps:
        db.add(CoshCoreItem(
            cosh_id=sp_id, core_type=COSH_BIOLOGICAL_NAMES_CORE,
            status="active", translations={"en": en},
        ))
        db.add(CoshConnectRow(
            connect_id=f"sp_pg_crops:{sp_id}",
            connect_type=COSH_SP_PG_CROPS_CONNECT, status="active",
            endpoints=[
                {"role": COSH_BIOLOGICAL_NAMES_CORE, "cosh_id": sp_id,   "position": SPPC_POS_SP},
                {"role": COSH_PROBLEM_GROUPS_CORE,   "cosh_id": pg_id,   "position": SPPC_POS_PG},
                {"role": COSH_BIOLOGICAL_NAMES_CORE, "cosh_id": crop_id, "position": SPPC_POS_CROP},
            ],
        ))
    return crop_id, [s[0] for s in sps]


@requires_docker
@pytest.mark.asyncio
async def test_specific_problems_returns_cosh_list_for_crop(db):
    client = await make_client(db)
    user = await make_user(db, name="SE")
    crop_id, sp_ids = await _seed_sp_pg_crops_tomato(db)
    await db.commit()
    out = await cha_sp_specific_problems(
        client_id=client.id, crop_cosh_id=crop_id,
        db=db, current_user=user,
    )
    cosh_ids = {p["cosh_id"] for p in out}
    assert set(sp_ids) <= cosh_ids
    assert all(p["existing"] is None for p in out)


@requires_docker
@pytest.mark.asyncio
async def test_specific_problems_flags_existing_recommendation(db):
    """Once an SP recommendation exists for (client, crop, sp), the
    list flags it via the `existing` field so the SE can navigate
    straight into the existing bundle."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    crop_id, sp_ids = await _seed_sp_pg_crops_tomato(db)
    target_sp = sp_ids[0]
    other_sp = sp_ids[1]
    sp = SPRecommendation(
        specific_problem_cosh_id=target_sp,
        client_id=client.id, crop_cosh_id=crop_id,
        status="DRAFT",
    )
    db.add(sp); await db.commit()

    out = await cha_sp_specific_problems(
        client_id=client.id, crop_cosh_id=crop_id,
        db=db, current_user=user,
    )
    by_id = {p["cosh_id"]: p for p in out}
    assert by_id[target_sp]["existing"]["id"] == sp.id
    assert by_id[target_sp]["existing"]["status"] == "DRAFT"
    assert by_id[other_sp]["existing"] is None


@requires_docker
@pytest.mark.asyncio
async def test_specific_problems_unknown_crop_returns_empty(db):
    client = await make_client(db)
    user = await make_user(db, name="SE")
    await db.commit()
    out = await cha_sp_specific_problems(
client_id=client.id,crop_cosh_id="crop:never_in_v1_list",
db=db,current_user=user,
)
    assert out == []


# ── recommendations ────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_sp_recommendations_filter_by_crop(db):
    client = await make_client(db)
    user = await make_user(db, name="SE")
    db.add_all([
SPRecommendation(
specific_problem_cosh_id="sp:tomato_late_blight",
client_id=client.id,crop_cosh_id="crop:tomato",status="DRAFT",
),
        SPRecommendation(
specific_problem_cosh_id="sp:onion_thrips",
client_id=client.id,crop_cosh_id="crop:onion",status="ACTIVE",
),
    ])
    await db.commit()

    out = await cha_sp_list_recommendations(
client_id=client.id,crop_cosh_id="crop:tomato",
db=db,current_user=user,
)
    assert len(out) == 1
    assert out[0]["specific_problem_name_en"] == "Tomato Late Blight"


# ── timelines + practices ──────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_sp_timelines_carry_crop_and_sp_context(db):
    client = await make_client(db)
    user = await make_user(db, name="SE")
    db.add(CoshCoreItem(cosh_id="crop:tomato",core_type=COSH_BIOLOGICAL_NAMES_CORE,
translations={"en": "Tomato"},status="active"))
    sp = SPRecommendation(
specific_problem_cosh_id="sp:tomato_late_blight",
client_id=client.id,crop_cosh_id="crop:tomato",status="DRAFT",
)
    db.add(sp); await db.flush()
    tl = Timeline(
sp_recommendation_id=sp.id,name="Day 0–3",
from_value=0,to_value=3,
)
    db.add(tl); await db.flush()
    db.add(Practice(
timeline_id=tl.id,l0_type="INPUT",
l1_type="PESTICIDE",l2_type="CHEMICAL_PESTICIDES",
))
    await db.commit()

    out = await cha_sp_list_timelines(client_id=client.id, db=db, current_user=user)
    assert len(out) == 1
    row = out[0]
    assert row["crop_name_en"] == "Tomato"
    assert row["specific_problem_name_en"] == "Tomato Late Blight"
    assert row["practice_count"] == 1


@requires_docker
@pytest.mark.asyncio
async def test_sp_practices_breadcrumb(db):
    client = await make_client(db)
    user = await make_user(db, name="SE")
    db.add(CoshCoreItem(cosh_id="crop:tomato",core_type=COSH_BIOLOGICAL_NAMES_CORE,
translations={"en": "Tomato"},status="active"))
    sp = SPRecommendation(
specific_problem_cosh_id="sp:tomato_late_blight",
client_id=client.id,crop_cosh_id="crop:tomato",status="DRAFT",
)
    db.add(sp); await db.flush()
    tl = Timeline(sp_recommendation_id=sp.id, name="W1", from_value=0, to_value=7)
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

    out = await cha_sp_list_practices(client_id=client.id, db=db, current_user=user)
    assert out["total"] == 1
    p = out["items"][0]
    assert p["crop_name_en"] == "Tomato"
    assert p["specific_problem_name_en"] == "Tomato Late Blight"
    assert p["brand_cosh_id"] == "brand:dithane-m45"
    assert p["dosage_summary"] == "2 kg/ha"


# ── Round 3: detail page support + publish flow ────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_get_client_sp_returns_single_row(db):
    from app.modules.advisory.router import get_client_sp
    client = await make_client(db)
    user = await make_user(db, name="SE")
    sp = SPRecommendation(
specific_problem_cosh_id="sp:tomato_late_blight",
client_id=client.id,crop_cosh_id="crop:tomato",status="DRAFT",
)
    db.add(sp); await db.commit()
    out = await get_client_sp(client_id=client.id, sp_id=sp.id, db=db, current_user=user)
    assert out.id == sp.id
    assert out.crop_cosh_id == "crop:tomato"


@requires_docker
@pytest.mark.asyncio
async def test_delete_sp_practice_cascades_elements(db):
    from app.modules.advisory.router import delete_client_sp_practice
    from sqlalchemy import select as _sel
    client = await make_client(db)
    user = await make_user(db, name="SE")
    sp = SPRecommendation(
specific_problem_cosh_id="sp:x",client_id=client.id,
crop_cosh_id="crop:tomato",status="DRAFT",
)
    db.add(sp); await db.flush()
    tl = Timeline(sp_recommendation_id=sp.id, name="W1", from_value=0, to_value=7)
    db.add(tl); await db.flush()
    practice = Practice(
timeline_id=tl.id,l0_type="INPUT",
l1_type="PESTICIDE",l2_type="CHEMICAL_PESTICIDES",
)
    db.add(practice); await db.flush()
    db.add(Element(practice_id=practice.id, element_type="DOSAGE", value="2"))
    await db.commit()

    await delete_client_sp_practice(
client_id=client.id,sp_id=sp.id,tl_id=tl.id,practice_id=practice.id,
db=db,current_user=user,
)
    practices = (await db.execute(_sel(Practice))).scalars().all()
    elements = (await db.execute(_sel(Element))).scalars().all()
    assert practices == []
    assert elements == []


@requires_docker
@pytest.mark.asyncio
async def test_sp_readiness_ready_when_timeline_present(db):
    from app.modules.advisory.router import get_sp_publish_readiness
    client = await make_client(db)
    user = await make_user(db, name="SE")
    sp = SPRecommendation(
specific_problem_cosh_id="sp:x",client_id=client.id,
crop_cosh_id="crop:tomato",status="DRAFT",
)
    db.add(sp); await db.flush()
    db.add(Timeline(sp_recommendation_id=sp.id, name="W1", from_value=0, to_value=7))
    await db.commit()

    out = await get_sp_publish_readiness(
client_id=client.id,sp_id=sp.id,db=db,current_user=user,
)
    assert out["ready"] is True


@requires_docker
@pytest.mark.asyncio
async def test_sp_readiness_flags_no_timelines(db):
    from app.modules.advisory.router import get_sp_publish_readiness
    client = await make_client(db)
    user = await make_user(db, name="SE")
    sp = SPRecommendation(
specific_problem_cosh_id="sp:x",client_id=client.id,
crop_cosh_id="crop:tomato",status="DRAFT",
)
    db.add(sp); await db.commit()

    out = await get_sp_publish_readiness(
client_id=client.id,sp_id=sp.id,db=db,current_user=user,
)
    assert out["ready"] is False
    assert any(m["code"] == "no_timelines" for m in out["missing"])


@requires_docker
@pytest.mark.asyncio
async def test_sp_publish_422_on_empty(db):
    from fastapi import HTTPException
    from app.modules.advisory.router import publish_sp
    client = await make_client(db)
    user = await make_user(db, name="SE")
    sp = SPRecommendation(
specific_problem_cosh_id="sp:x",client_id=client.id,
crop_cosh_id="crop:tomato",status="DRAFT",
)
    db.add(sp); await db.commit()
    with pytest.raises(HTTPException) as exc:
        await publish_sp(client_id=client.id, sp_id=sp.id, db=db, current_user=user)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "publish_blocked_missing_fields"


@requires_docker
@pytest.mark.asyncio
async def test_sp_publish_only_deactivates_same_crop_problem_siblings(db):
    """Round 3 fix: sibling-deactivation now scoped on
    (client, crop, sp_cosh_id) — same crop, same problem only.
    Two SPs that happen to share specific_problem_cosh_id but for
    different crops must NOT cross-deactivate."""
    from app.modules.advisory.router import publish_sp
    from sqlalchemy import select as _sel
    client = await make_client(db)
    user = await make_user(db, name="SE")
    tomato_old = SPRecommendation(
specific_problem_cosh_id="sp:shared",client_id=client.id,
crop_cosh_id="crop:tomato",status="ACTIVE",
)
    tomato_new = SPRecommendation(
specific_problem_cosh_id="sp:shared",client_id=client.id,
crop_cosh_id="crop:tomato",status="DRAFT",
)
    onion_active = SPRecommendation(
specific_problem_cosh_id="sp:shared",client_id=client.id,
crop_cosh_id="crop:onion",status="ACTIVE",
)
    db.add_all([tomato_old, tomato_new, onion_active])
    await db.flush()
    db.add(Timeline(sp_recommendation_id=tomato_new.id, name="W1", from_value=0, to_value=7))
    await db.commit()

    await publish_sp(client_id=client.id, sp_id=tomato_new.id, db=db, current_user=user)
    refreshed = (await db.execute(_sel(SPRecommendation))).scalars().all()
    by_id = {r.id: r for r in refreshed}
    assert by_id[tomato_new.id].status == "ACTIVE"
    assert by_id[tomato_old.id].status == "INACTIVE"  # same (crop, sp), deactivated
    assert by_id[onion_active.id].status == "ACTIVE"  # different crop, untouched
