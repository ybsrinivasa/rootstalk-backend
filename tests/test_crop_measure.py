"""Phase D.1 — crop_measures service + admin endpoints."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.sync.models import CropMeasure
from app.modules.sync.router import (
    CropMeasureSetRequest, list_crop_measures, set_crop_measure,
)
from app.services.crop_measure import (
    AREA_WISE, PLANT_WISE, get_measure, list_measures, set_measure,
)
from sqlalchemy import select
from tests.conftest import requires_docker
from tests.factories import make_user


# ── Service ─────────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_get_measure_returns_none_when_absent(db):
    out = await get_measure(db, "crop:tomato")
    assert out is None


@requires_docker
@pytest.mark.asyncio
async def test_set_measure_creates_row(db):
    user = await make_user(db)
    await db.commit()
    row = await set_measure(
        db, crop_cosh_id="crop:tomato", measure=AREA_WISE, user_id=user.id,
    )
    await db.commit()
    assert row.measure == AREA_WISE
    assert row.crop_cosh_id == "crop:tomato"
    assert row.updated_by_user_id == user.id

    # Read-back via accessor.
    assert await get_measure(db, "crop:tomato") == AREA_WISE


@requires_docker
@pytest.mark.asyncio
async def test_set_measure_upserts_on_existing_crop(db):
    user = await make_user(db)
    await db.commit()
    await set_measure(db, crop_cosh_id="crop:coconut", measure=AREA_WISE, user_id=user.id)
    await set_measure(db, crop_cosh_id="crop:coconut", measure=PLANT_WISE, user_id=user.id)
    await db.commit()

    rows = (await db.execute(
        select(CropMeasure).where(CropMeasure.crop_cosh_id == "crop:coconut")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].measure == PLANT_WISE


@requires_docker
@pytest.mark.asyncio
async def test_set_measure_rejects_invalid_value(db):
    with pytest.raises(ValueError, match="must be one of"):
        await set_measure(db, crop_cosh_id="crop:x", measure="WEIGHT_WISE")
    with pytest.raises(ValueError, match="must be one of"):
        await set_measure(db, crop_cosh_id="crop:x", measure="")


@requires_docker
@pytest.mark.asyncio
async def test_list_measures_returns_sorted_by_crop_id(db):
    await set_measure(db, crop_cosh_id="crop:zebra", measure=AREA_WISE)
    await set_measure(db, crop_cosh_id="crop:apple", measure=PLANT_WISE)
    await set_measure(db, crop_cosh_id="crop:mango", measure=AREA_WISE)
    await db.commit()
    rows = await list_measures(db)
    assert [r.crop_cosh_id for r in rows] == [
        "crop:apple", "crop:mango", "crop:zebra",
    ]


# ── Admin endpoints ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_set_endpoint_creates_then_lists(db):
    user = await make_user(db)
    await db.commit()

    out = await set_crop_measure(
        crop_cosh_id="crop:paddy",
        request=CropMeasureSetRequest(measure="AREA_WISE"),
        db=db, current_user=user,
    )
    assert out["crop_cosh_id"] == "crop:paddy"
    assert out["measure"] == "AREA_WISE"
    assert out["updated_by_user_id"] == user.id

    listing = await list_crop_measures(db=db, current_user=user)
    assert any(r["crop_cosh_id"] == "crop:paddy" for r in listing)


@requires_docker
@pytest.mark.asyncio
async def test_set_endpoint_returns_422_on_invalid_measure(db):
    user = await make_user(db)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await set_crop_measure(
            crop_cosh_id="crop:bad",
            request=CropMeasureSetRequest(measure="INVALID"),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_list_endpoint_empty_initially(db):
    user = await make_user(db)
    await db.commit()
    out = await list_crop_measures(db=db, current_user=user)
    assert out == []
