"""Batch 39R — Global PG status toggle + Global PG Timeline status
toggle. Two endpoints:

  PUT /advisory/global/pg-recommendations/{pg_id}
  PUT /advisory/global/pg-recommendations/{pg_id}/timelines/{tl_id}

PG status: ACTIVE ↔ INACTIVE flips freely; DRAFT → INACTIVE allowed
(discard a draft); DRAFT → ACTIVE refused (must go through publish).

Timeline status: ACTIVE ↔ INACTIVE flips freely; from_type stays
immutable; name + from/to values editable.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.advisory.models import PGRecommendation, Timeline
from app.modules.advisory.router import (
    update_global_pg,
    update_global_pg_timeline,
)
from app.modules.advisory.schemas import (
    PGRecommendationUpdate,
    PGTimelineUpdate,
)
from fastapi import HTTPException
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


async def _make_global_pg(db, *, status: str = "DRAFT") -> PGRecommendation:
    pg = PGRecommendation(
        problem_group_cosh_id="cosh-pg-fungal",
        client_id=None, area_or_plant="AREA_WISE", status=status,
    )
    db.add(pg)
    await db.commit()
    await db.refresh(pg)
    return pg


# ── PG status toggle ──────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_pg_active_to_inactive_flips(db):
    user = await make_user(db, name="CM")
    pg = await _make_global_pg(db, status="ACTIVE")
    out = await update_global_pg(
        pg_id=pg.id, request=PGRecommendationUpdate(status="INACTIVE"),
        db=db, current_user=user,
    )
    assert out.status == "INACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_pg_inactive_to_active_flips(db):
    user = await make_user(db, name="CM")
    pg = await _make_global_pg(db, status="INACTIVE")
    out = await update_global_pg(
        pg_id=pg.id, request=PGRecommendationUpdate(status="ACTIVE"),
        db=db, current_user=user,
    )
    assert out.status == "ACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_pg_draft_to_inactive_allowed(db):
    user = await make_user(db, name="CM")
    pg = await _make_global_pg(db, status="DRAFT")
    out = await update_global_pg(
        pg_id=pg.id, request=PGRecommendationUpdate(status="INACTIVE"),
        db=db, current_user=user,
    )
    assert out.status == "INACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_pg_draft_to_active_refused_with_publish_required(db):
    user = await make_user(db, name="CM")
    pg = await _make_global_pg(db, status="DRAFT")
    with pytest.raises(HTTPException) as ei:
        await update_global_pg(
            pg_id=pg.id, request=PGRecommendationUpdate(status="ACTIVE"),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "publish_required"


@requires_docker
@pytest.mark.asyncio
async def test_pg_invalid_status_value_rejected(db):
    user = await make_user(db, name="CM")
    pg = await _make_global_pg(db, status="ACTIVE")
    with pytest.raises(HTTPException) as ei:
        await update_global_pg(
            pg_id=pg.id, request=PGRecommendationUpdate(status="BOGUS"),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "invalid_status"


@requires_docker
@pytest.mark.asyncio
async def test_pg_404_when_pg_missing(db):
    user = await make_user(db, name="CM")
    with pytest.raises(HTTPException) as ei:
        await update_global_pg(
            pg_id="00000000-0000-0000-0000-000000000000",
            request=PGRecommendationUpdate(status="INACTIVE"),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_pg_update_refuses_client_scoped_row(db):
    """The Global PUT must not target a client-scoped PG row."""
    user = await make_user(db, name="CM")
    client = await make_client(db)
    pg = PGRecommendation(
        problem_group_cosh_id="cosh-pg-fungal",
        client_id=client.id, area_or_plant="AREA_WISE",
        status="ACTIVE",
    )
    db.add(pg)
    await db.commit()
    await db.refresh(pg)
    with pytest.raises(HTTPException) as ei:
        await update_global_pg(
            pg_id=pg.id, request=PGRecommendationUpdate(status="INACTIVE"),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 404


# ── PG Timeline status toggle ────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_pg_timeline_active_to_inactive_flips(db):
    user = await make_user(db, name="CM")
    pg = await _make_global_pg(db, status="ACTIVE")
    tl = Timeline(
        pg_recommendation_id=pg.id, name="W1",
        from_value=0, to_value=7,
    )
    db.add(tl)
    await db.commit()
    await db.refresh(tl)
    out = await update_global_pg_timeline(
        pg_id=pg.id, tl_id=tl.id,
        request=PGTimelineUpdate(status="INACTIVE"),
        db=db, current_user=user,
    )
    assert out["status"] == "INACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_pg_timeline_inactive_to_active_flips(db):
    user = await make_user(db, name="CM")
    pg = await _make_global_pg(db, status="ACTIVE")
    tl = Timeline(
        pg_recommendation_id=pg.id, name="W1",
        from_value=0, to_value=7, status="INACTIVE",
    )
    db.add(tl)
    await db.commit()
    await db.refresh(tl)
    out = await update_global_pg_timeline(
        pg_id=pg.id, tl_id=tl.id,
        request=PGTimelineUpdate(status="ACTIVE"),
        db=db, current_user=user,
    )
    assert out["status"] == "ACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_pg_timeline_invalid_status_rejected(db):
    user = await make_user(db, name="CM")
    pg = await _make_global_pg(db, status="ACTIVE")
    tl = Timeline(
        pg_recommendation_id=pg.id, name="W1",
        from_value=0, to_value=7,
    )
    db.add(tl)
    await db.commit()
    await db.refresh(tl)
    with pytest.raises(HTTPException) as ei:
        await update_global_pg_timeline(
            pg_id=pg.id, tl_id=tl.id,
            request=PGTimelineUpdate(status="DRAFT"),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "invalid_status"


@requires_docker
@pytest.mark.asyncio
async def test_pg_timeline_can_edit_name_and_range(db):
    user = await make_user(db, name="CM")
    pg = await _make_global_pg(db, status="ACTIVE")
    tl = Timeline(
        pg_recommendation_id=pg.id, name="W1",
        from_value=0, to_value=7,
    )
    db.add(tl)
    await db.commit()
    await db.refresh(tl)
    out = await update_global_pg_timeline(
        pg_id=pg.id, tl_id=tl.id,
        request=PGTimelineUpdate(name="Week 1", from_value=1, to_value=10),
        db=db, current_user=user,
    )
    assert out["name"] == "Week 1"
    assert out["from_value"] == 1
    assert out["to_value"] == 10


@requires_docker
@pytest.mark.asyncio
async def test_pg_timeline_404_when_wrong_pg(db):
    """Timeline IDs are global, so the (pg_id, tl_id) pair must match."""
    user = await make_user(db, name="CM")
    pg_a = await _make_global_pg(db, status="ACTIVE")
    pg_b = await _make_global_pg(db, status="ACTIVE")
    tl = Timeline(
        pg_recommendation_id=pg_a.id, name="W1",
        from_value=0, to_value=7,
    )
    db.add(tl)
    await db.commit()
    await db.refresh(tl)
    with pytest.raises(HTTPException) as ei:
        await update_global_pg_timeline(
            pg_id=pg_b.id, tl_id=tl.id,
            request=PGTimelineUpdate(status="INACTIVE"),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 404
