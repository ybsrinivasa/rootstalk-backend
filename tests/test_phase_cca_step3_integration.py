"""CCA Step 3 — DB-backed integration tests for the timeline
validation hardening (Batch 3-Hardening: type ↔ package consistency,
sign validation, name-uniqueness 422).

Pure-function coverage of the validators lives in
`tests/test_timeline_validation.py`. This file drives
`create_timeline` / `update_timeline` / `import_timeline` against
the testcontainer DB to verify each validator's stable error code
surfaces correctly through the API.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageStatus, PackageType, Timeline, TimelineFromType,
)
from app.modules.advisory.router import (
    create_package, create_timeline, import_timeline, update_timeline,
)
from app.modules.advisory.schemas import (
    PackageCreate, TimelineCreate, TimelineUpdate,
)
from app.modules.clients.router import add_crop
from app.modules.clients.schemas import CropCreate
from tests.conftest import requires_docker
from tests.factories import make_client, make_crop_reference, make_user


async def _annual_paddy_package(db, *, client, user, name="Annual Paddy") -> Package:
    """Spin up an Annual paddy Package via the API path (so the
    membership gate + ClientCrop snapshot are in place)."""
    await make_crop_reference(db, "crop:paddy", measure="AREA_WISE")
    await db.commit()
    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    return await create_package(
        client_id=client.id,
        request=PackageCreate(
            crop_cosh_id="crop:paddy", name=name,
            package_type=PackageType.ANNUAL, duration_days=120,
            start_date_label_cosh_id="label:sowing_date",
        ),
        db=db, current_user=user,
    )


async def _perennial_paddy_package(db, *, client, user, name="Perennial Paddy") -> Package:
    await make_crop_reference(db, "crop:paddy", measure="AREA_WISE")
    await db.commit()
    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    return await create_package(
        client_id=client.id,
        request=PackageCreate(
            crop_cosh_id="crop:paddy", name=name,
            package_type=PackageType.PERENNIAL, duration_days=365,
            start_date_label_cosh_id="label:planting_date",
        ),
        db=db, current_user=user,
    )


# ── create_timeline: happy paths ─────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_annual_das_timeline(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)

    out = await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="TL-1", from_type=TimelineFromType.DAS,
            from_value=0, to_value=15,
        ),
        db=db, current_user=user,
    )
    assert out.from_type == TimelineFromType.DAS
    assert out.from_value == 0 and out.to_value == 15


@requires_docker
@pytest.mark.asyncio
async def test_create_annual_dbs_timeline(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)

    out = await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="TL-DBS", from_type=TimelineFromType.DBS,
            from_value=15, to_value=8,
        ),
        db=db, current_user=user,
    )
    assert out.from_type == TimelineFromType.DBS


@requires_docker
@pytest.mark.asyncio
async def test_create_perennial_calendar_timeline(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _perennial_paddy_package(db, client=client, user=user)

    out = await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="TL-Cal", from_type=TimelineFromType.CALENDAR,
            from_value=60, to_value=120,
        ),
        db=db, current_user=user,
    )
    assert out.from_type == TimelineFromType.CALENDAR


# ── create_timeline: type ↔ package consistency ──────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_calendar_on_annual_422(db):
    """Spec rule: Annual packages support DBS/DAS only."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)

    with pytest.raises(HTTPException) as ei:
        await create_timeline(
            client_id=client.id, package_id=pkg.id,
            request=TimelineCreate(
                name="Bad", from_type=TimelineFromType.CALENDAR,
                from_value=10, to_value=50,
            ),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "timeline_type_mismatch"


@requires_docker
@pytest.mark.asyncio
async def test_create_das_on_perennial_422(db):
    """Spec rule: Perennial packages support CALENDAR only."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _perennial_paddy_package(db, client=client, user=user)

    with pytest.raises(HTTPException) as ei:
        await create_timeline(
            client_id=client.id, package_id=pkg.id,
            request=TimelineCreate(
                name="Bad", from_type=TimelineFromType.DAS,
                from_value=0, to_value=15,
            ),
            db=db, current_user=user,
        )
    assert ei.value.detail["code"] == "timeline_type_mismatch"


@requires_docker
@pytest.mark.asyncio
async def test_create_dbs_on_perennial_422(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _perennial_paddy_package(db, client=client, user=user)

    with pytest.raises(HTTPException) as ei:
        await create_timeline(
            client_id=client.id, package_id=pkg.id,
            request=TimelineCreate(
                name="Bad", from_type=TimelineFromType.DBS,
                from_value=15, to_value=8,
            ),
            db=db, current_user=user,
        )
    assert ei.value.detail["code"] == "timeline_type_mismatch"


# ── create_timeline: sign validation ─────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_dbs_with_zero_value_422(db):
    """Spec: DBS values must be strictly positive (days BEFORE
    crop start). from=0 means "0 days before" = the start day,
    which is DAS territory."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)

    with pytest.raises(HTTPException) as ei:
        await create_timeline(
            client_id=client.id, package_id=pkg.id,
            request=TimelineCreate(
                name="Zero", from_type=TimelineFromType.DBS,
                from_value=10, to_value=0,
            ),
            db=db, current_user=user,
        )
    assert ei.value.detail["code"] == "timeline_invalid_sign"


@requires_docker
@pytest.mark.asyncio
async def test_create_das_with_negative_value_422(db):
    """DAS values are start day onwards; negative = days before =
    DBS territory."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)

    with pytest.raises(HTTPException) as ei:
        await create_timeline(
            client_id=client.id, package_id=pkg.id,
            request=TimelineCreate(
                name="Neg", from_type=TimelineFromType.DAS,
                from_value=-3, to_value=10,
            ),
            db=db, current_user=user,
        )
    assert ei.value.detail["code"] == "timeline_invalid_sign"


# ── create_timeline: direction (kept for cohesion) ───────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_das_direction_inverted_422(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)

    with pytest.raises(HTTPException) as ei:
        await create_timeline(
            client_id=client.id, package_id=pkg.id,
            request=TimelineCreate(
                name="BadDir", from_type=TimelineFromType.DAS,
                from_value=10, to_value=5,
            ),
            db=db, current_user=user,
        )
    assert ei.value.detail["code"] == "timeline_invalid_direction"


# ── create_timeline: name uniqueness ─────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_duplicate_name_422(db):
    """Pre-fix the unique-constraint at the DB level fired as a 500
    IntegrityError. Now caught explicitly with a stable code so the
    CA portal can render a friendly error."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)

    await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="Same", from_type=TimelineFromType.DAS,
            from_value=0, to_value=15,
        ),
        db=db, current_user=user,
    )
    with pytest.raises(HTTPException) as ei:
        await create_timeline(
            client_id=client.id, package_id=pkg.id,
            request=TimelineCreate(
                name="Same", from_type=TimelineFromType.DAS,
                from_value=20, to_value=40,
            ),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "timeline_name_duplicate"


@requires_docker
@pytest.mark.asyncio
async def test_create_overlapping_timelines_in_same_package_allowed(db):
    """Per user clarification 2026-05-07: overlaps within DAS or
    within DBS are allowed by spec — gap/overlap detection (BL-17)
    is informational, not blocking."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)

    await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="A", from_type=TimelineFromType.DAS,
            from_value=5, to_value=9,
        ),
        db=db, current_user=user,
    )
    out = await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="B", from_type=TimelineFromType.DAS,
            from_value=8, to_value=12,  # overlaps A on day-offsets 8-9
        ),
        db=db, current_user=user,
    )
    assert out.name == "B"


# ── update_timeline: name uniqueness on rename ───────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_update_rename_to_existing_name_422(db):
    """Renaming TL-A to TL-B's existing name must surface 422
    timeline_name_duplicate, not 500."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)

    tl_a = await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="A", from_type=TimelineFromType.DAS,
            from_value=0, to_value=10,
        ),
        db=db, current_user=user,
    )
    await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="B", from_type=TimelineFromType.DAS,
            from_value=11, to_value=20,
        ),
        db=db, current_user=user,
    )

    with pytest.raises(HTTPException) as ei:
        await update_timeline(
            client_id=client.id, package_id=pkg.id, timeline_id=tl_a.id,
            request=TimelineUpdate(name="B"),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "timeline_name_duplicate"


@requires_docker
@pytest.mark.asyncio
async def test_update_keep_same_name_doesnt_false_positive(db):
    """Updating without changing the name shouldn't trip the unique
    check on the timeline's own row."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)

    tl = await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="A", from_type=TimelineFromType.DAS,
            from_value=0, to_value=10,
        ),
        db=db, current_user=user,
    )
    out = await update_timeline(
        client_id=client.id, package_id=pkg.id, timeline_id=tl.id,
        request=TimelineUpdate(to_value=20),
        db=db, current_user=user,
    )
    assert out.to_value == 20
    assert out.name == "A"


# ── update_timeline: post-update validation ──────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_update_to_negative_das_422(db):
    """Update can't take a DAS timeline to a negative value either."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)
    tl = await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="A", from_type=TimelineFromType.DAS,
            from_value=0, to_value=10,
        ),
        db=db, current_user=user,
    )

    with pytest.raises(HTTPException) as ei:
        await update_timeline(
            client_id=client.id, package_id=pkg.id, timeline_id=tl.id,
            request=TimelineUpdate(from_value=-5),
            db=db, current_user=user,
        )
    assert ei.value.detail["code"] == "timeline_invalid_sign"


# ── import_timeline ──────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_import_into_mismatched_package_type_422(db):
    """Import a DAS timeline (from an Annual source package) into a
    Perennial target — must reject with timeline_type_mismatch."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    annual_pkg = await _annual_paddy_package(db, client=client, user=user, name="Source")
    perennial_pkg = await create_package(
        client_id=client.id,
        request=PackageCreate(
            crop_cosh_id="crop:paddy", name="Target",
            package_type=PackageType.PERENNIAL, duration_days=365,
            start_date_label_cosh_id="label:planting_date",
        ),
        db=db, current_user=user,
    )
    src_tl = await create_timeline(
        client_id=client.id, package_id=annual_pkg.id,
        request=TimelineCreate(
            name="DAS-TL", from_type=TimelineFromType.DAS,
            from_value=0, to_value=10,
        ),
        db=db, current_user=user,
    )

    with pytest.raises(HTTPException) as ei:
        await import_timeline(
            client_id=client.id, package_id=perennial_pkg.id,
            data={"source_timeline_id": src_tl.id, "new_name": "Imported"},
            db=db, current_user=user,
        )
    assert ei.value.detail["code"] == "timeline_type_mismatch"


@requires_docker
@pytest.mark.asyncio
async def test_import_with_duplicate_name_422(db):
    """Import with new_name matching an existing timeline in the
    target package — friendly 422 instead of DB-constraint 500."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)

    src_tl = await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="Original", from_type=TimelineFromType.DAS,
            from_value=0, to_value=10,
        ),
        db=db, current_user=user,
    )
    await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="Existing", from_type=TimelineFromType.DAS,
            from_value=11, to_value=20,
        ),
        db=db, current_user=user,
    )

    with pytest.raises(HTTPException) as ei:
        await import_timeline(
            client_id=client.id, package_id=pkg.id,
            data={
                "source_timeline_id": src_tl.id,
                "new_name": "Existing",  # collides
            },
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "timeline_name_duplicate"


@requires_docker
@pytest.mark.asyncio
async def test_import_happy_path_same_package_with_rename(db):
    """Spec: imports can happen from the same Package; the new
    timeline must have a different name."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await _annual_paddy_package(db, client=client, user=user)
    src_tl = await create_timeline(
        client_id=client.id, package_id=pkg.id,
        request=TimelineCreate(
            name="Spray-Round-1", from_type=TimelineFromType.DAS,
            from_value=10, to_value=15,
        ),
        db=db, current_user=user,
    )

    out = await import_timeline(
        client_id=client.id, package_id=pkg.id,
        data={
            "source_timeline_id": src_tl.id,
            "new_name": "Spray-Round-2",
        },
        db=db, current_user=user,
    )
    assert out.name == "Spray-Round-2"
    assert out.from_type == TimelineFromType.DAS
    assert out.from_value == 10 and out.to_value == 15
