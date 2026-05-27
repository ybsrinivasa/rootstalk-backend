"""Plant-wise vs area-wise subscription context (2026-05-27).

Crop Dashboard now branches on the crop's Cosh `crop_area_plant_wise`
typing:
  - AREA_WISE → farm_area_acres + crop_start_date
  - PLANT_WISE → number_of_plants + planting_year + crop_start_date

Tests pin:
  - /plant-count + /plant-count/confirm endpoints work for a
    plant-wise sub and refuse 422 for area-wise crops.
  - /farm-area refuses 422 for a plant-wise crop.
  - my-subscriptions response includes crop_measure + crop_age
    derived from the right source (days from start_date for
    area-wise; years from planting_year for plant-wise).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.subscriptions.models import Subscription
from app.modules.subscriptions.router import (
    confirm_plant_count, my_subscriptions, update_farm_area,
    update_plant_count,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_crop_reference, make_package, make_subscription, make_user,
)


CROP_TOMATO = "crop:tomato"
CROP_COCONUT = "crop:coconut"


async def _sub_for_crop(db, crop_cosh_id: str, measure: str):
    """Seed a farmer + sub at a client. No Primary pundit needed for
    these tests (they don't submit queries); update_plant_count + the
    measure-resolution path don't require routing."""
    client = await make_client(db)
    farmer = await make_user(db, name=f"Farmer-{crop_cosh_id}")
    await make_crop_reference(db, crop_cosh_id, name=crop_cosh_id, measure=measure)
    pkg = await make_package(db, client, crop_cosh_id=crop_cosh_id)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    return farmer, sub


# ── /plant-count endpoints ──────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_update_plant_count_writes_both_fields(db):
    farmer, sub = await _sub_for_crop(db, CROP_COCONUT, "PLANT_WISE")
    await db.commit()
    out = await update_plant_count(
        subscription_id=sub.id,
        data={"number_of_plants": 120, "planting_year": 2015},
        db=db, current_user=farmer,
    )
    assert out["number_of_plants"] == 120
    assert out["planting_year"] == 2015


@requires_docker
@pytest.mark.asyncio
async def test_update_plant_count_refuses_on_area_wise(db):
    farmer, sub = await _sub_for_crop(db, CROP_TOMATO, "AREA_WISE")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await update_plant_count(
            subscription_id=sub.id,
            data={"number_of_plants": 100, "planting_year": 2020},
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "wrong_measure_for_plant_endpoint"


@requires_docker
@pytest.mark.asyncio
async def test_update_farm_area_refuses_on_plant_wise(db):
    farmer, sub = await _sub_for_crop(db, CROP_COCONUT, "PLANT_WISE")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await update_farm_area(
            subscription_id=sub.id,
            data={"farm_area_acres": 2.0},
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "wrong_measure_for_area_endpoint"


@requires_docker
@pytest.mark.asyncio
async def test_confirm_plant_count_locks_both_fields(db):
    farmer, sub = await _sub_for_crop(db, CROP_COCONUT, "PLANT_WISE")
    await db.commit()
    await update_plant_count(
        subscription_id=sub.id,
        data={"number_of_plants": 50, "planting_year": 2018},
        db=db, current_user=farmer,
    )
    out = await confirm_plant_count(
        subscription_id=sub.id, data={},
        db=db, current_user=farmer,
    )
    assert out["confirmed_at"] is not None

    # Update after confirm must refuse.
    with pytest.raises(HTTPException) as exc:
        await update_plant_count(
            subscription_id=sub.id,
            data={"number_of_plants": 60},
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 400


@requires_docker
@pytest.mark.asyncio
async def test_confirm_plant_count_refuses_when_fields_missing(db):
    farmer, sub = await _sub_for_crop(db, CROP_COCONUT, "PLANT_WISE")
    await db.commit()
    # planting_year not set yet
    await update_plant_count(
        subscription_id=sub.id, data={"number_of_plants": 80},
        db=db, current_user=farmer,
    )
    with pytest.raises(HTTPException) as exc:
        await confirm_plant_count(
            subscription_id=sub.id, data={},
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 422


# ── my-subscriptions includes crop_measure + crop_age ───────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_my_subscriptions_carries_measure_and_age_area_wise(db):
    farmer, sub = await _sub_for_crop(db, CROP_TOMATO, "AREA_WISE")
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=45)
    await db.commit()
    out = await my_subscriptions(db=db, current_user=farmer)
    row = next(r for r in out if r["id"] == sub.id)
    assert row["crop_measure"] == "AREA_WISE"
    assert row["crop_age"]["unit"] == "days"
    assert row["crop_age"]["source"] == "START_DATE"
    # Allow ±1 day for clock-tick edges around the test boundary.
    assert 44 <= row["crop_age"]["value"] <= 45
    # Plant-wise fields stay null on an area-wise sub.
    assert row["number_of_plants"] is None
    assert row["planting_year"] is None


@requires_docker
@pytest.mark.asyncio
async def test_my_subscriptions_carries_measure_and_age_plant_wise(db):
    from datetime import date as _date
    farmer, sub = await _sub_for_crop(db, CROP_COCONUT, "PLANT_WISE")
    sub.number_of_plants = 200
    sub.planting_year = 2015
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()
    out = await my_subscriptions(db=db, current_user=farmer)
    row = next(r for r in out if r["id"] == sub.id)
    assert row["crop_measure"] == "PLANT_WISE"
    assert row["crop_age"]["unit"] == "years"
    assert row["crop_age"]["source"] == "PLANTING_YEAR"
    assert row["crop_age"]["value"] == _date.today().year - 2015
    assert row["number_of_plants"] == 200
    assert row["planting_year"] == 2015


@requires_docker
@pytest.mark.asyncio
async def test_crop_age_beyond_floor_returns_is_minimum(db):
    """planting_year < 1970 (the dropdown floor) — set when the
    farmer picks "Beyond 1970" — returns is_minimum=true with a
    value clipped at current_year - 1970. PWA renders that as
    "> N years"."""
    from datetime import date as _date
    farmer, sub = await _sub_for_crop(db, CROP_COCONUT, "PLANT_WISE")
    sub.number_of_plants = 200
    sub.planting_year = 1969   # sentinel for "Beyond 1970"
    await db.commit()
    out = await my_subscriptions(db=db, current_user=farmer)
    row = next(r for r in out if r["id"] == sub.id)
    assert row["crop_age"]["unit"] == "years"
    assert row["crop_age"]["is_minimum"] is True
    assert row["crop_age"]["value"] == _date.today().year - 1970


@requires_docker
@pytest.mark.asyncio
async def test_my_subscriptions_handles_untyped_crop_as_area_wise(db):
    """An untyped crop (no entry in `crop_area_plant_wise` Connect)
    defaults to AREA_WISE so legacy data doesn't trip the renderer."""
    client = await make_client(db)
    farmer = await make_user(db, name="Untyped Farmer")
    pkg = await make_package(db, client, crop_cosh_id="crop:untyped")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    await db.commit()
    out = await my_subscriptions(db=db, current_user=farmer)
    row = next(r for r in out if r["id"] == sub.id)
    assert row["crop_measure"] == "AREA_WISE"
