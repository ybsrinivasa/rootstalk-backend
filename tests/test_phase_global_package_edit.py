"""PUT /advisory/global/packages/{pkg_id} — edit Global Package
details after creation (2026-05-11). Mirrors the client-scoped
update_package validator: ANNUAL duration range-checked,
PERENNIAL locked at 365, name / description / start_date_label
freely editable."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.modules.advisory.models import (
    Package, PackageStatus, PackageType,
)
from app.modules.advisory.router import update_global_package
from app.modules.advisory.schemas import PackageUpdate
from tests.conftest import requires_docker
from tests.factories import make_user


async def _seed_global(db, *, package_type=PackageType.ANNUAL, duration=120):
    pkg = Package(
        client_id=None,
        name=f"GP-{uuid.uuid4().hex[:6]}",
        crop_cosh_id="crop:test",
        package_type=package_type, duration_days=duration,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.DRAFT,
    )
    db.add(pkg)
    await db.flush()
    return pkg


@requires_docker
@pytest.mark.asyncio
async def test_update_name_and_description(db):
    user = await make_user(db, name="CM")
    pkg = await _seed_global(db)
    await db.commit()
    out = await update_global_package(
        pkg_id=pkg.id,
        request=PackageUpdate(name="Tomato Drip PoP", description="Best for hill regions"),
        db=db, current_user=user,
    )
    assert out.name == "Tomato Drip PoP"
    assert out.description == "Best for hill regions"


@requires_docker
@pytest.mark.asyncio
async def test_update_duration_days_for_annual(db):
    user = await make_user(db, name="CM")
    pkg = await _seed_global(db, package_type=PackageType.ANNUAL, duration=120)
    await db.commit()
    out = await update_global_package(
        pkg_id=pkg.id, request=PackageUpdate(duration_days=180),
        db=db, current_user=user,
    )
    assert out.duration_days == 180


@requires_docker
@pytest.mark.asyncio
async def test_update_duration_days_locked_for_perennial(db):
    """Perennial duration is locked at 365 — update_package validator
    refuses any other value."""
    user = await make_user(db, name="CM")
    pkg = await _seed_global(db, package_type=PackageType.PERENNIAL, duration=365)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await update_global_package(
            pkg_id=pkg.id, request=PackageUpdate(duration_days=100),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_update_start_date_label(db):
    user = await make_user(db, name="CM")
    pkg = await _seed_global(db)
    await db.commit()
    out = await update_global_package(
        pkg_id=pkg.id,
        request=PackageUpdate(start_date_label_cosh_id="label:planting_date"),
        db=db, current_user=user,
    )
    assert out.start_date_label_cosh_id == "label:planting_date"


@requires_docker
@pytest.mark.asyncio
async def test_update_404_for_client_scoped_package(db):
    """The Global PUT must refuse to operate on client-scoped rows —
    keeps the separation clean. Caller would use the CA-side
    update_package endpoint for those."""
    from tests.factories import make_client, make_package
    user = await make_user(db, name="CM")
    client = await make_client(db)
    local_pkg = await make_package(db, client)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await update_global_package(
            pkg_id=local_pkg.id, request=PackageUpdate(name="x"),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 404
