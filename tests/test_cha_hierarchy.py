"""CHA Recommendation Hierarchy Resolver — integration tests.

Spec: RootsTalk_AgriTeam_Document_v5-2.pdf §8.7. Rewired 2026-05-25 to
read the real Cosh wire shape:
- The diagnosed cosh_id is a `biological_names` pest (BL-08 output).
- The SP recommendation's `specific_problem_cosh_id` column stores the
  same biological_names id (the column name is legacy).
- The bridge from a pest to its parent `problem_groups` Core item lives
  in Cosh's `sp_pg_crops` Connect.
- PG recommendations are keyed on a `problem_groups` cosh_id.

Mock-based tests previously stubbed `cosh_core_items` lookups by core
type; that pattern stopped reflecting the production query path after
the rewire. These integration tests use real DB rows so the resolver
exercises actual SQL.
"""
from __future__ import annotations

import pytest

from app.modules.advisory.models import PGRecommendation, SPRecommendation
from app.modules.sync.models import CoshConnectRow
from app.services.cha_hierarchy import resolve_cha_recommendation
from tests.conftest import requires_docker
from tests.factories import make_client, make_crop_reference


CROP = "crop:tomato"
PEST = "bn:tomato_early_blight"   # biological_names pest cosh_id
PG_GROUP = "pg:fungal_diseases"   # problem_groups cosh_id (seeded by conftest)


def _sp_pg_crops_row(connect_id: str, *, sp: str, pg: str, crop: str) -> CoshConnectRow:
    """Build an sp_pg_crops Connect row in the real 3-position shape:
    pos 1 = biological_names (SP), pos 2 = problem_groups (PG),
    pos 3 = biological_names (crop)."""
    return CoshConnectRow(
        connect_id=connect_id,
        connect_type="sp_pg_crops",
        status="active",
        endpoints=[
            {"role": "biological_names", "cosh_id": sp,   "position": 1},
            {"role": "problem_groups",   "cosh_id": pg,   "position": 2},
            {"role": "biological_names", "cosh_id": crop, "position": 3},
        ],
        metadata_=None,
    )


@requires_docker
@pytest.mark.asyncio
async def test_sp_client_wins_when_authored_for_the_diagnosed_pest(db):
    """SP recommendation for the exact diagnosed problem (by this
    client, this crop) wins over any PG bundle. The SP row's
    specific_problem_cosh_id matches the biological_names id BL-08
    emits — no Cosh lookup needed for that branch."""
    client = await make_client(db)
    await make_crop_reference(db, CROP, name="Tomato", measure="AREA_WISE")
    sp = SPRecommendation(
        specific_problem_cosh_id=PEST, client_id=client.id,
        crop_cosh_id=CROP, status="ACTIVE",
    )
    # Also seed a client PG bundle that SHOULDN'T win.
    pg = PGRecommendation(
        problem_group_cosh_id=PG_GROUP, client_id=client.id,
        area_or_plant="AREA_WISE", status="ACTIVE",
    )
    db.add(_sp_pg_crops_row("sppc:tomato-eb", sp=PEST, pg=PG_GROUP, crop=CROP))
    db.add_all([sp, pg])
    await db.commit()

    out = await resolve_cha_recommendation(
        db, client.id, PEST, crop_cosh_id=CROP,
    )
    assert out is not None
    assert out.level == "SP_CLIENT"
    assert out.recommendation_type == "SP"
    assert out.recommendation_id == sp.id


@requires_docker
@pytest.mark.asyncio
async def test_pg_client_via_sp_pg_crops_bridge(db):
    """No SP authored → resolver walks sp_pg_crops to find the
    pest's parent problem_groups, then matches the client's PG
    bundle on that group."""
    client = await make_client(db)
    await make_crop_reference(db, CROP, name="Tomato", measure="AREA_WISE")
    db.add(_sp_pg_crops_row("sppc:tomato-eb", sp=PEST, pg=PG_GROUP, crop=CROP))
    pg = PGRecommendation(
        problem_group_cosh_id=PG_GROUP, client_id=client.id,
        area_or_plant="AREA_WISE", status="ACTIVE",
    )
    db.add(pg)
    await db.commit()

    out = await resolve_cha_recommendation(
        db, client.id, PEST, crop_cosh_id=CROP,
    )
    assert out is not None
    assert out.level == "PG_CLIENT"
    assert out.recommendation_type == "PG"
    assert out.recommendation_id == pg.id
    assert out.parent_pg_cosh_id == PG_GROUP


@requires_docker
@pytest.mark.asyncio
async def test_pg_global_fallback_via_bridge(db):
    """When the client has neither SP nor PG for the diagnosed pest,
    fall back to RootsTalk's global PG bundle (client_id IS NULL)."""
    client = await make_client(db)
    await make_crop_reference(db, CROP, name="Tomato", measure="AREA_WISE")
    db.add(_sp_pg_crops_row("sppc:tomato-eb", sp=PEST, pg=PG_GROUP, crop=CROP))
    pg_global = PGRecommendation(
        problem_group_cosh_id=PG_GROUP, client_id=None,
        area_or_plant="AREA_WISE", status="ACTIVE",
    )
    db.add(pg_global)
    await db.commit()

    out = await resolve_cha_recommendation(
        db, client.id, PEST, crop_cosh_id=CROP,
    )
    assert out is not None
    assert out.level == "PG_GLOBAL"
    assert out.recommendation_id == pg_global.id


@requires_docker
@pytest.mark.asyncio
async def test_none_when_no_bridge_and_no_pg(db):
    """A diagnosed pest with no sp_pg_crops row and no
    direct-`problem_groups` cosh_id match → no recommendation.
    The commit endpoint still succeeds upstream, but no
    TriggeredCHAEntry is created."""
    client = await make_client(db)
    await make_crop_reference(db, CROP, name="Tomato", measure="AREA_WISE")
    # No sp_pg_crops row; no PG/SP recs.
    out = await resolve_cha_recommendation(
        db, client.id, "bn:some-pest-cosh hasnt mapped yet",
        crop_cosh_id=CROP,
    )
    assert out is None


@requires_docker
@pytest.mark.asyncio
async def test_problem_groups_input_skips_bridge(db):
    """Edge: BL-08 narrows directly to a problem_groups item (rare
    but valid when Cosh hasn't split a group into sub-pests).
    The bridge lookup returns nothing, but the resolver recognises
    the cosh_id as a problem_groups Core and proceeds to PG lookup."""
    client = await make_client(db)
    await make_crop_reference(db, CROP, name="Tomato", measure="AREA_WISE")
    # No sp_pg_crops row needed — diagnosed cosh_id IS the
    # problem_groups id (seeded by conftest as
    # COSH_PROBLEM_GROUPS_CORE).
    pg = PGRecommendation(
        problem_group_cosh_id=PG_GROUP, client_id=client.id,
        area_or_plant="AREA_WISE", status="ACTIVE",
    )
    db.add(pg); await db.commit()

    out = await resolve_cha_recommendation(
        db, client.id, PG_GROUP, crop_cosh_id=CROP,
    )
    assert out is not None
    assert out.level == "PG_CLIENT"
    assert out.parent_pg_cosh_id == PG_GROUP


@requires_docker
@pytest.mark.asyncio
async def test_resolver_returns_friendly_name_from_cosh(db):
    """The diagnosed problem's display name resolves through Cosh
    `cosh_core_items.translations.en` so the advisory card shows
    "Tomato - Early Blight" rather than a raw cosh_id."""
    from app.modules.sync.models import CoshCoreItem
    client = await make_client(db)
    await make_crop_reference(db, CROP, name="Tomato", measure="AREA_WISE")
    db.add(CoshCoreItem(
        cosh_id=PEST, core_type="biological_names", status="active",
        translations={"en": "Tomato - Early Blight"},
    ))
    db.add(_sp_pg_crops_row("sppc:tomato-eb", sp=PEST, pg=PG_GROUP, crop=CROP))
    pg = PGRecommendation(
        problem_group_cosh_id=PG_GROUP, client_id=client.id,
        area_or_plant="AREA_WISE", status="ACTIVE",
    )
    db.add(pg); await db.commit()

    out = await resolve_cha_recommendation(
        db, client.id, PEST, crop_cosh_id=CROP,
    )
    assert out.problem_name == "Tomato - Early Blight"
