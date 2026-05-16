"""Read-through lookups over Cosh's `sp_pg_crops` Connect (shipped
2026-05-14, 3-endpoint: SP × PG × Crop).

Seeds a minimal Cosh world (two PGs, three crops, four SPs) and
exercises the three lookup directions:

  • list_crops_for_pg
  • list_pgs_for_crop
  • list_sps_for_pg_crop

Plus inactive-row dropping and unknown-id empty behaviour.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_BIOLOGICAL_NAMES_CORE,
    COSH_PROBLEM_GROUPS_CORE,
    COSH_SP_PG_CROPS_CONNECT,
)
from app.services.sp_pg_crops_view import (
    list_crops_for_pg,
    list_pgs_for_crop,
    list_sps_for_pg_crop,
)
from tests.conftest import requires_docker


def _core(
    cosh_id: str, core_type: str, name: str, status: str = "active",
) -> CoshCoreItem:
    return CoshCoreItem(
        cosh_id=cosh_id, core_type=core_type,
        translations={"en": name}, status=status,
    )


def _sppc_row(
    cid: str, *, sp: str, pg: str, crop: str, status: str = "active",
) -> CoshConnectRow:
    return CoshConnectRow(
        connect_id=cid, connect_type=COSH_SP_PG_CROPS_CONNECT,
        status=status,
        endpoints=[
            {"role": COSH_BIOLOGICAL_NAMES_CORE, "cosh_id": sp,   "position": 1},
            {"role": COSH_PROBLEM_GROUPS_CORE,   "cosh_id": pg,   "position": 2},
            {"role": COSH_BIOLOGICAL_NAMES_CORE, "cosh_id": crop, "position": 3},
        ],
    )


async def _seed_world(db) -> None:
    """Two PGs, three crops, four SPs. Connect rows:
      r1 — Powdery Mildew (fungal) on Tomato
      r2 — Late Blight (fungal) on Tomato
      r3 — Late Blight (fungal) on Potato
      r4 — Aphid (sucking) on Tomato
      r5 — Aphid (sucking) on Mango
    """
    db.add(_core("pg:fungal", COSH_PROBLEM_GROUPS_CORE, "Fungal Diseases"))
    db.add(_core("pg:sucking", COSH_PROBLEM_GROUPS_CORE, "Sucking Pests"))

    db.add(_core("crop:tomato", COSH_BIOLOGICAL_NAMES_CORE, "Tomato"))
    db.add(_core("crop:potato", COSH_BIOLOGICAL_NAMES_CORE, "Potato"))
    db.add(_core("crop:mango", COSH_BIOLOGICAL_NAMES_CORE, "Mango"))

    db.add(_core("sp:powdery", COSH_BIOLOGICAL_NAMES_CORE, "Powdery Mildew"))
    db.add(_core("sp:lateblight", COSH_BIOLOGICAL_NAMES_CORE, "Late Blight"))
    db.add(_core("sp:aphid", COSH_BIOLOGICAL_NAMES_CORE, "Aphid"))

    db.add(_sppc_row("r1", sp="sp:powdery",    pg="pg:fungal",  crop="crop:tomato"))
    db.add(_sppc_row("r2", sp="sp:lateblight", pg="pg:fungal",  crop="crop:tomato"))
    db.add(_sppc_row("r3", sp="sp:lateblight", pg="pg:fungal",  crop="crop:potato"))
    db.add(_sppc_row("r4", sp="sp:aphid",      pg="pg:sucking", crop="crop:tomato"))
    db.add(_sppc_row("r5", sp="sp:aphid",      pg="pg:sucking", crop="crop:mango"))
    await db.commit()


# ── Crops for a PG ────────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_crops_for_pg_returns_distinct_sorted(db):
    await _seed_world(db)
    items = await list_crops_for_pg(db, pg_cosh_id="pg:fungal")
    assert [i["name_en"] for i in items] == ["Potato", "Tomato"]
    items = await list_crops_for_pg(db, pg_cosh_id="pg:sucking")
    assert [i["name_en"] for i in items] == ["Mango", "Tomato"]


@requires_docker
@pytest.mark.asyncio
async def test_crops_for_unknown_pg_returns_empty(db):
    await _seed_world(db)
    items = await list_crops_for_pg(db, pg_cosh_id="pg:does_not_exist")
    assert items == []


# ── PGs for a crop ────────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_pgs_for_crop_returns_distinct_sorted(db):
    await _seed_world(db)
    items = await list_pgs_for_crop(db, crop_cosh_id="crop:tomato")
    assert [i["name_en"] for i in items] == ["Fungal Diseases", "Sucking Pests"]
    items = await list_pgs_for_crop(db, crop_cosh_id="crop:potato")
    assert [i["name_en"] for i in items] == ["Fungal Diseases"]
    items = await list_pgs_for_crop(db, crop_cosh_id="crop:mango")
    assert [i["name_en"] for i in items] == ["Sucking Pests"]


@requires_docker
@pytest.mark.asyncio
async def test_pgs_for_unknown_crop_returns_empty(db):
    await _seed_world(db)
    items = await list_pgs_for_crop(db, crop_cosh_id="crop:nope")
    assert items == []


# ── SPs at (PG, crop) intersection ────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_sps_for_pg_crop_returns_intersection(db):
    await _seed_world(db)
    items = await list_sps_for_pg_crop(
        db, pg_cosh_id="pg:fungal", crop_cosh_id="crop:tomato",
    )
    assert [i["name_en"] for i in items] == ["Late Blight", "Powdery Mildew"]
    # Potato carries only Late Blight.
    items = await list_sps_for_pg_crop(
        db, pg_cosh_id="pg:fungal", crop_cosh_id="crop:potato",
    )
    assert [i["name_en"] for i in items] == ["Late Blight"]
    # Mango sucking → only Aphid.
    items = await list_sps_for_pg_crop(
        db, pg_cosh_id="pg:sucking", crop_cosh_id="crop:mango",
    )
    assert [i["name_en"] for i in items] == ["Aphid"]


@requires_docker
@pytest.mark.asyncio
async def test_sps_for_empty_intersection_returns_empty(db):
    await _seed_world(db)
    # No rows for Mango × Fungal.
    items = await list_sps_for_pg_crop(
        db, pg_cosh_id="pg:fungal", crop_cosh_id="crop:mango",
    )
    assert items == []


# ── Inactive handling ─────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_inactive_connect_row_ignored(db):
    await _seed_world(db)
    # Flip r3 (Late Blight on Potato) — the only fungal/Potato row —
    # to inactive. Potato must drop out of `list_crops_for_pg(fungal)`.
    r3 = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.connect_id == "r3")
    )).scalar_one()
    r3.status = "inactive"
    await db.commit()
    items = await list_crops_for_pg(db, pg_cosh_id="pg:fungal")
    assert [i["name_en"] for i in items] == ["Tomato"]


@requires_docker
@pytest.mark.asyncio
async def test_inactive_core_item_dropped_from_results(db):
    await _seed_world(db)
    # Mark Mango Core inactive → it must disappear from the sucking PG
    # crop list even though its Connect row is still active.
    mango = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "crop:mango")
    )).scalar_one()
    mango.status = "inactive"
    await db.commit()
    items = await list_crops_for_pg(db, pg_cosh_id="pg:sucking")
    assert [i["name_en"] for i in items] == ["Tomato"]
