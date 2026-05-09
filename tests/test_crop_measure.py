"""Crop → Measure tests — Cosh-sourced (Round 3, 2026-05-09).

Pre-Round-3 these tests covered `set_measure` (a manual SA write path
into the local `crop_measures` table). Post Round 3, Cosh's
`crop_area_plant_wise` Connect is the source; RootsTalk reads through
and never writes. The admin write endpoint and the local seed path
are gone.

What's tested here now:
  • `get_measure` walks the Cosh Connect and maps the role UUID to
    the AREA_WISE / PLANT_WISE token.
  • Inactive Connect rows are filtered (a curator unlinking the
    classification revokes the measure).
  • The read-through `GET /admin/crop-measures` returns one row per
    Crop with `measure: None` for un-classified ones (still pending
    Cosh-side typing).
"""
from __future__ import annotations

import pytest

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.modules.sync.router import list_crop_measures
from app.services.cosh_constants import (
    COSH_AREA_PLANT_WISE_CORE, COSH_AREA_WISE_UUID,
    COSH_BIOLOGICAL_NAMES_CORE, COSH_CROP_AREA_PLANT_CONNECT,
    COSH_NAME_ROLE_CONNECT, COSH_PLANT_WISE_UUID, COSH_ROLES_CORE,
    COSH_ROLE_CROP_UUID,
)
from app.services.crop_measure import AREA_WISE, PLANT_WISE, get_measure
from tests.conftest import requires_docker
from tests.factories import make_crop_reference, make_user


# ── get_measure ────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_get_measure_returns_none_when_no_connect_row(db):
    """Crop classified but Cosh hasn't typed it Area/Plant yet — None."""
    await make_crop_reference(db, "bn:tomato", name="Tomato", measure=None)
    await db.commit()
    assert await get_measure(db, "bn:tomato") is None


@requires_docker
@pytest.mark.asyncio
async def test_get_measure_returns_area_wise(db):
    await make_crop_reference(db, "bn:onion", name="Onion", measure="AREA_WISE")
    await db.commit()
    assert await get_measure(db, "bn:onion") == AREA_WISE


@requires_docker
@pytest.mark.asyncio
async def test_get_measure_returns_plant_wise(db):
    await make_crop_reference(db, "bn:apple", name="Apple", measure="PLANT_WISE")
    await db.commit()
    assert await get_measure(db, "bn:apple") == PLANT_WISE


@requires_docker
@pytest.mark.asyncio
async def test_inactive_area_plant_connect_excluded(db):
    """A curator deactivating the area_plant_wise Connect row revokes
    the typing — same pattern as the Crop classification."""
    from sqlalchemy import select
    await make_crop_reference(db, "bn:mango", name="Mango", measure="AREA_WISE")
    row = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_id == "connect:bn:mango:measure",
        )
    )).scalar_one()
    row.status = "inactive"
    await db.commit()
    assert await get_measure(db, "bn:mango") is None


@requires_docker
@pytest.mark.asyncio
async def test_get_measure_returns_none_for_unknown_crop(db):
    """Cosh has no Connect rows for an unknown cosh_id → None.
    Caller (BL-06, plant-wise) treats None as a configuration error."""
    assert await get_measure(db, "bn:never-synced") is None


# ── Admin read-through endpoint ────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_crop_measures_surfaces_unclassified_crops(db):
    """The read-through endpoint must include Crops Cosh hasn't typed
    yet, with `measure: None` — the SA team uses this view to spot
    which crops still need the Area/Plant tag on the Cosh side."""
    user = await make_user(db)
    await make_crop_reference(db, "bn:typed", name="Typed", measure="AREA_WISE")
    await make_crop_reference(db, "bn:untyped", name="Untyped", measure=None)
    await db.commit()

    out = await list_crop_measures(db=db, current_user=user)
    by_id = {r["crop_cosh_id"]: r for r in out}
    assert by_id["bn:typed"]["measure"] == "AREA_WISE"
    assert by_id["bn:typed"]["name_en"] == "Typed"
    assert by_id["bn:untyped"]["measure"] is None
    assert by_id["bn:untyped"]["name_en"] == "Untyped"


@requires_docker
@pytest.mark.asyncio
async def test_list_crop_measures_excludes_pests(db):
    """Pest biological_names — even with Connect rows tagging them as
    Pest — should not appear in the crop-measures listing. The
    listing is keyed off Crop classification."""
    user = await make_user(db)
    db.add_all([
        CoshCoreItem(cosh_id=COSH_ROLE_CROP_UUID, core_type=COSH_ROLES_CORE,
                     translations={"en": "Crop"}, status="active"),
        CoshCoreItem(cosh_id="role-pest", core_type=COSH_ROLES_CORE,
                     translations={"en": "Pest"}, status="active"),
        CoshCoreItem(cosh_id="bn:aphid", core_type=COSH_BIOLOGICAL_NAMES_CORE,
                     translations={"en": "Aphid"}, status="active"),
    ])
    db.add(CoshConnectRow(
        connect_id="c:pest", connect_type=COSH_NAME_ROLE_CONNECT,
        status="active",
        endpoints=[
            {"role": COSH_BIOLOGICAL_NAMES_CORE, "cosh_id": "bn:aphid", "position": 1},
            {"role": COSH_ROLES_CORE, "cosh_id": "role-pest", "position": 2},
        ],
    ))
    await db.commit()

    out = await list_crop_measures(db=db, current_user=user)
    assert all(r["crop_cosh_id"] != "bn:aphid" for r in out)
