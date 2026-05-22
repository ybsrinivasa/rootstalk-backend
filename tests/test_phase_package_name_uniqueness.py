"""Package name uniqueness — 2026-05-22.

Pre-fix bug (reported by the user): creating a new Package with the
same name as an existing one would silently land a second row with
a different UUID. From the SE's view it looked like a "replica" —
two packages with identical display, different IDs underneath.

Rule (locked 2026-05-22): within a (client_id, crop_cosh_id) bucket,
no two distinct Package lineages may share a name (case-insensitive).
Same name CAN repeat across crops; CAN repeat across versions of the
same lineage. Global namespace (client_id=None) is evaluated
separately from any client's namespace.

Lineage key is (client_id, crop_cosh_id, name) — different lineage
== different name. So same-name across different lineages == the bug
this file pins down.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.advisory.models import (
    Package, PackageStatus, PackageType,
)
from app.modules.advisory.router import (
    _assert_package_name_available, create_global_package, create_package,
    update_global_package, update_package,
)
from app.modules.advisory.schemas import PackageCreate, PackageUpdate
from app.modules.clients.models import ClientUserRole
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_crop_reference, make_package,
    make_user,
)


async def _seed_se(db, client):
    se = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    return se


async def _seed_crop_on_belt(db, client, crop_cosh_id="crop:tomato"):
    """The CA create_package path runs assert_crop_on_belt — seed
    the (client, crop) onto the conveyor belt via ClientCrop so
    tests don't trip on that gate."""
    from app.modules.clients.models import ClientCrop
    await make_crop_reference(db, crop_cosh_id, name=crop_cosh_id)
    db.add(ClientCrop(client_id=client.id, crop_cosh_id=crop_cosh_id))


# ── Helper (unit) ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_helper_rejects_same_name_same_crop_same_client(db):
    client = await make_client(db)
    existing = await make_package(
        db, client, name="Annual Cycle", crop_cosh_id="crop:tomato",
    )
    existing.status = PackageStatus.DRAFT
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await _assert_package_name_available(
            db, client_id=client.id,
            crop_cosh_id="crop:tomato", name="Annual Cycle",
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "package_name_taken"
    assert exc.value.detail["existing_package_id"] == existing.id


@requires_docker
@pytest.mark.asyncio
async def test_helper_case_insensitive(db):
    client = await make_client(db)
    await make_package(
        db, client, name="Annual Cycle", crop_cosh_id="crop:tomato",
    )
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await _assert_package_name_available(
            db, client_id=client.id,
            crop_cosh_id="crop:tomato", name="ANNUAL CYCLE",
        )
    assert exc.value.detail["code"] == "package_name_taken"


@requires_docker
@pytest.mark.asyncio
async def test_helper_allows_same_name_different_crop(db):
    """Crop disambiguates in the UI — same name across crops is
    explicitly allowed."""
    client = await make_client(db)
    await make_package(
        db, client, name="Annual Cycle", crop_cosh_id="crop:tomato",
    )
    await db.commit()
    # Different crop — no exception.
    await _assert_package_name_available(
        db, client_id=client.id,
        crop_cosh_id="crop:chilli", name="Annual Cycle",
    )


@requires_docker
@pytest.mark.asyncio
async def test_helper_allows_same_name_different_client(db):
    """Multi-tenant isolation — Client A and Client B both have an
    'Annual Cycle' Package for Tomato. Legal."""
    client_a = await make_client(db, short_name="alpha")
    client_b = await make_client(db, short_name="beta")
    await make_package(
        db, client_a, name="Annual Cycle", crop_cosh_id="crop:tomato",
    )
    await db.commit()
    # Client B is a separate bucket.
    await _assert_package_name_available(
        db, client_id=client_b.id,
        crop_cosh_id="crop:tomato", name="Annual Cycle",
    )


@requires_docker
@pytest.mark.asyncio
async def test_helper_ignores_inactive_rows(db):
    """An INACTIVE row with the same name no longer blocks reuse —
    the name slot frees up on deactivation. (Lineage versions stay
    visible via lineage endpoints regardless.)"""
    client = await make_client(db)
    pkg = await make_package(
        db, client, name="Annual Cycle", crop_cosh_id="crop:tomato",
    )
    pkg.status = PackageStatus.INACTIVE
    await db.commit()

    # Should NOT raise.
    await _assert_package_name_available(
        db, client_id=client.id,
        crop_cosh_id="crop:tomato", name="Annual Cycle",
    )


@requires_docker
@pytest.mark.asyncio
async def test_helper_global_namespace_separate_from_client(db):
    """A Global Package called 'Tomato Standard' must not block a
    client from naming their own Package 'Tomato Standard' — Global
    vs Client are separate namespaces."""
    # Seed Global pkg (client_id is None).
    global_pkg = Package(
        client_id=None, crop_cosh_id="crop:tomato", name="Tomato Standard",
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.ACTIVE,
    )
    db.add(global_pkg)
    await db.commit()

    client = await make_client(db)
    # Client-namespace check — Global doesn't interfere.
    await _assert_package_name_available(
        db, client_id=client.id,
        crop_cosh_id="crop:tomato", name="Tomato Standard",
    )


@requires_docker
@pytest.mark.asyncio
async def test_helper_exclude_self_on_rename_noop(db):
    """When the caller passes exclude_package_id, the row being
    edited doesn't flag itself — sanity check the param works."""
    client = await make_client(db)
    pkg = await make_package(
        db, client, name="Annual Cycle", crop_cosh_id="crop:tomato",
    )
    await db.commit()
    # Should NOT raise — we're excluding the only matching row.
    await _assert_package_name_available(
        db, client_id=client.id,
        crop_cosh_id="crop:tomato", name="Annual Cycle",
        exclude_package_id=pkg.id,
    )


# ── End-to-end via the CA create endpoint ─────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_package_rejects_duplicate_name_same_crop(db):
    client = await make_client(db)
    se = await _seed_se(db, client)
    await _seed_crop_on_belt(db, client, "crop:tomato")
    await make_package(
        db, client, name="Annual Cycle", crop_cosh_id="crop:tomato",
    )
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_package(
            client_id=client.id,
            request=PackageCreate(
                crop_cosh_id="crop:tomato", name="annual cycle",
                package_type=PackageType.ANNUAL, duration_days=120,
                start_date_label_cosh_id="label:sowing_date",
            ),
            db=db, current_user=se,
        )
    assert exc.value.detail["code"] == "package_name_taken"


@requires_docker
@pytest.mark.asyncio
async def test_create_package_allows_same_name_different_crop(db):
    """End-to-end version of the cross-crop allowance."""
    client = await make_client(db)
    se = await _seed_se(db, client)
    await _seed_crop_on_belt(db, client, "crop:tomato")
    await _seed_crop_on_belt(db, client, "crop:chilli")
    await make_package(
        db, client, name="Annual Cycle", crop_cosh_id="crop:tomato",
    )
    await db.commit()

    out = await create_package(
        client_id=client.id,
        request=PackageCreate(
            crop_cosh_id="crop:chilli", name="Annual Cycle",
            package_type=PackageType.ANNUAL, duration_days=120,
            start_date_label_cosh_id="label:sowing_date",
        ),
        db=db, current_user=se,
    )
    assert out.name == "Annual Cycle"
    assert out.crop_cosh_id == "crop:chilli"


# ── End-to-end via the CA rename (update) endpoint ────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_update_package_rename_to_taken_name_rejected(db):
    client = await make_client(db)
    se = await _seed_se(db, client)
    await _seed_crop_on_belt(db, client, "crop:tomato")
    # Two distinct lineages.
    taken = await make_package(
        db, client, name="Annual Cycle", crop_cosh_id="crop:tomato",
    )
    mine = await make_package(
        db, client, name="Quick Cycle", crop_cosh_id="crop:tomato",
    )
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await update_package(
            client_id=client.id, package_id=mine.id,
            request=PackageUpdate(name="Annual Cycle"),
            db=db, current_user=se,
        )
    assert exc.value.detail["code"] == "package_name_taken"
    assert exc.value.detail["existing_package_id"] == taken.id


@requires_docker
@pytest.mark.asyncio
async def test_update_package_same_name_noop_allowed(db):
    """Submitting the existing name (or just changing description)
    must not trip the rename gate on lineage siblings."""
    client = await make_client(db)
    se = await _seed_se(db, client)
    await _seed_crop_on_belt(db, client, "crop:tomato")
    pkg = await make_package(
        db, client, name="Annual Cycle", crop_cosh_id="crop:tomato",
    )
    await db.commit()

    out = await update_package(
        client_id=client.id, package_id=pkg.id,
        request=PackageUpdate(name="Annual Cycle", description="updated"),
        db=db, current_user=se,
    )
    assert out.description == "updated"


# ── End-to-end via SA Global create/update ────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_global_package_rejects_duplicate_name(db):
    # make_user auto-grants CONTENT_MANAGER, which satisfies the SA
    # Global write guard (`_assert_sa_or_cm`) without extra setup.
    sa = await make_user(db, name="SA")
    await make_crop_reference(db, "crop:tomato", name="Tomato")
    Package_existing = Package(
        client_id=None, crop_cosh_id="crop:tomato", name="Universal Tomato",
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.DRAFT,
    )
    db.add(Package_existing)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_global_package(
            request=PackageCreate(
                crop_cosh_id="crop:tomato", name="universal tomato",
                package_type=PackageType.ANNUAL, duration_days=120,
                start_date_label_cosh_id="label:sowing_date",
            ),
            db=db, current_user=sa,
        )
    assert exc.value.detail["code"] == "package_name_taken"
