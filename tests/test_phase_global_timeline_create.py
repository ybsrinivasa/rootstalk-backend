"""POST /advisory/global/packages/{pkg_id}/timelines — Global
Timeline creation (regression test for the 2026-05-11 500-on-
add bug where the endpoint called an undefined `_validate_timeline`).

Confirms the endpoint runs the same package_type/direction/sign
validation as the client-scoped `create_timeline`, plus the name
uniqueness pre-check."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.modules.advisory.models import (
    Package, PackageStatus, PackageType, TimelineFromType,
)
from app.modules.advisory.router import create_global_timeline
from app.modules.advisory.schemas import TimelineCreate
from tests.conftest import requires_docker
from tests.factories import make_user


async def _seed_global(db, *, package_type=PackageType.ANNUAL):
    pkg = Package(
        client_id=None,
        name=f"GP-{uuid.uuid4().hex[:6]}",
        crop_cosh_id="crop:test",
        package_type=package_type, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.DRAFT,
    )
    db.add(pkg)
    await db.flush()
    return pkg


@requires_docker
@pytest.mark.asyncio
async def test_happy_path_das_annual(db):
    user = await make_user(db, name="CM")
    pkg = await _seed_global(db)
    await db.commit()
    out = await create_global_timeline(
        pkg_id=pkg.id,
        request=TimelineCreate(
            name="Germination", from_type=TimelineFromType.DAS,
            from_value=1, to_value=8, display_order=0,
        ),
        db=db, current_user=user,
    )
    assert out.name == "Germination"
    assert out.from_type == TimelineFromType.DAS
    assert out.from_value == 1
    assert out.to_value == 8


@requires_docker
@pytest.mark.asyncio
async def test_dbs_requires_from_greater_than_to(db):
    """DBS counts down toward sowing — FROM should be larger than
    TO. Reversed input is a 422 from validate_timeline, not a 500."""
    user = await make_user(db, name="CM")
    pkg = await _seed_global(db)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await create_global_timeline(
            pkg_id=pkg.id,
            request=TimelineCreate(
                name="DBS bad", from_type=TimelineFromType.DBS,
                from_value=5, to_value=30, display_order=0,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_calendar_blocked_on_annual_package(db):
    """Annual Packages can't use CALENDAR — type/package gate fires."""
    user = await make_user(db, name="CM")
    pkg = await _seed_global(db, package_type=PackageType.ANNUAL)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await create_global_timeline(
            pkg_id=pkg.id,
            request=TimelineCreate(
                name="Cal on Annual", from_type=TimelineFromType.CALENDAR,
                from_value=60, to_value=240, display_order=0,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_duplicate_name_within_package_returns_422_not_500(db):
    """Name uniqueness per Package. Pre-check fires the friendly
    422 rather than letting the DB unique constraint surface a 500."""
    user = await make_user(db, name="CM")
    pkg = await _seed_global(db)
    await db.commit()
    await create_global_timeline(
        pkg_id=pkg.id,
        request=TimelineCreate(
            name="Dup Stage", from_type=TimelineFromType.DAS,
            from_value=0, to_value=10, display_order=0,
        ),
        db=db, current_user=user,
    )
    with pytest.raises(HTTPException) as exc:
        await create_global_timeline(
            pkg_id=pkg.id,
            request=TimelineCreate(
                name="Dup Stage", from_type=TimelineFromType.DAS,
                from_value=11, to_value=20, display_order=1,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_404_for_missing_package(db):
    user = await make_user(db, name="CM")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await create_global_timeline(
            pkg_id=f"ghost-{uuid.uuid4().hex[:8]}",
            request=TimelineCreate(
                name="x", from_type=TimelineFromType.DAS,
                from_value=0, to_value=10, display_order=0,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 404
