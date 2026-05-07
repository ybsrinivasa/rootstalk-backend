"""CCA Step 2 — DB-backed integration tests for Package field
validation (Batch 2A: duration range + Perennial lock + Start Date
Label required at create).

Pure-function coverage of `validate_package_duration_for_*` lives in
`tests/test_package_validation.py`. This file drives `create_package`
and `update_package` against the testcontainer DB to verify the
validators produce the right HTTP responses.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.modules.advisory.models import PackageStatus, PackageType
from app.modules.advisory.router import create_package, update_package
from app.modules.advisory.schemas import PackageCreate, PackageUpdate
from app.modules.clients.models import ClientCrop
from app.modules.clients.router import add_crop
from app.modules.clients.schemas import CropCreate
from tests.conftest import requires_docker
from tests.factories import make_client, make_crop_reference, make_user


async def _seed_paddy_on_belt(db, client, user) -> None:
    """Seed paddy in Cosh + add to the company conveyor belt."""
    await make_crop_reference(db, "crop:paddy", measure="AREA_WISE")
    await db.commit()
    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )


# ── create_package — duration validation ─────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_annual_with_valid_duration(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)

    out = await create_package(
        client_id=client.id,
        request=PackageCreate(
            crop_cosh_id="crop:paddy", name="Paddy Pop A",
            package_type=PackageType.ANNUAL, duration_days=120,
            start_date_label_cosh_id="label:sowing_date",
        ),
        db=db, current_user=user,
    )
    assert out.duration_days == 120
    assert out.package_type == PackageType.ANNUAL


@requires_docker
@pytest.mark.asyncio
async def test_create_annual_missing_duration_422(db):
    """Spec rule: Annual duration is mandatory. Pre-fix the route
    silently defaulted to 180 days — fail loud instead so the CA
    portal can prompt the expert."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)

    with pytest.raises(HTTPException) as ei:
        await create_package(
            client_id=client.id,
            request=PackageCreate(
                crop_cosh_id="crop:paddy", name="Paddy Missing Duration",
                package_type=PackageType.ANNUAL,
                # duration_days deliberately omitted
                start_date_label_cosh_id="label:sowing_date",
            ),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "duration_required"


@requires_docker
@pytest.mark.asyncio
async def test_create_annual_out_of_range_422(db):
    """A typo (e.g. 9999) must be rejected at create rather than
    shipping a Package with insane timeline arithmetic."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)

    with pytest.raises(HTTPException) as ei:
        await create_package(
            client_id=client.id,
            request=PackageCreate(
                crop_cosh_id="crop:paddy", name="Paddy Crazy Duration",
                package_type=PackageType.ANNUAL, duration_days=9999,
                start_date_label_cosh_id="label:sowing_date",
            ),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "duration_out_of_range"


@requires_docker
@pytest.mark.asyncio
async def test_create_perennial_forces_365(db):
    """Spec §4.1: Perennial duration is system-set. Whatever the
    caller sends (including None or a typo), persist 365."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)

    out = await create_package(
        client_id=client.id,
        request=PackageCreate(
            crop_cosh_id="crop:paddy", name="Paddy Perennial",
            package_type=PackageType.PERENNIAL, duration_days=100,
            start_date_label_cosh_id="label:planting_date",
        ),
        db=db, current_user=user,
    )
    assert out.duration_days == 365


@requires_docker
@pytest.mark.asyncio
async def test_create_missing_start_date_label_422(db):
    """Spec §4.1: Start Date Label is mandatory at create time.
    Validated at the Pydantic layer (not Optional anymore)."""
    client = await make_client(db)
    await db.commit()

    with pytest.raises(ValidationError):
        # Pydantic itself rejects — no router call needed
        PackageCreate(
            crop_cosh_id="crop:paddy", name="No Label PoP",
            package_type=PackageType.ANNUAL, duration_days=120,
            # start_date_label_cosh_id deliberately omitted
        )


# ── update_package — Perennial lock + Annual range ───────────────────────────

async def _create_test_package(
    db, *, client, user, package_type=PackageType.ANNUAL, duration_days=120,
):
    return await create_package(
        client_id=client.id,
        request=PackageCreate(
            crop_cosh_id="crop:paddy", name=f"PoP {package_type.value} {duration_days}",
            package_type=package_type, duration_days=duration_days,
            start_date_label_cosh_id="label:sowing_date",
        ),
        db=db, current_user=user,
    )


@requires_docker
@pytest.mark.asyncio
async def test_update_annual_duration_to_valid_range(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)

    out = await update_package(
        client_id=client.id, package_id=pkg.id,
        request=PackageUpdate(duration_days=180),
        db=db, current_user=user,
    )
    assert out.duration_days == 180


@requires_docker
@pytest.mark.asyncio
async def test_update_annual_out_of_range_422(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)

    with pytest.raises(HTTPException) as ei:
        await update_package(
            client_id=client.id, package_id=pkg.id,
            request=PackageUpdate(duration_days=500),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "duration_out_of_range"


@requires_docker
@pytest.mark.asyncio
async def test_update_perennial_duration_change_blocked_422(db):
    """The headline rule: Perennial duration is locked. Pre-fix
    `update_package` blindly setattr'd whatever was sent — flipping
    a Perennial to 100 days would have broken advisory alignment.
    Now blocked with a stable error code."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(
        db, client=client, user=user,
        package_type=PackageType.PERENNIAL, duration_days=365,
    )

    with pytest.raises(HTTPException) as ei:
        await update_package(
            client_id=client.id, package_id=pkg.id,
            request=PackageUpdate(duration_days=100),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "perennial_duration_locked"


@requires_docker
@pytest.mark.asyncio
async def test_update_perennial_resending_365_accepted(db):
    """Friendly-client rule: re-sending the unchanged 365 value
    shouldn't fail. Frontend bodies often include all fields."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(
        db, client=client, user=user,
        package_type=PackageType.PERENNIAL, duration_days=365,
    )

    out = await update_package(
        client_id=client.id, package_id=pkg.id,
        request=PackageUpdate(duration_days=365),
        db=db, current_user=user,
    )
    assert out.duration_days == 365


@requires_docker
@pytest.mark.asyncio
async def test_update_other_fields_unaffected(db):
    """A name/description update on a Perennial package should not
    trip the duration guard — duration_days isn't in the body."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(
        db, client=client, user=user,
        package_type=PackageType.PERENNIAL, duration_days=365,
    )

    out = await update_package(
        client_id=client.id, package_id=pkg.id,
        request=PackageUpdate(name="Renamed Perennial PoP", description="A note"),
        db=db, current_user=user,
    )
    assert out.name == "Renamed Perennial PoP"
    assert out.description == "A note"
    assert out.duration_days == 365  # unchanged
