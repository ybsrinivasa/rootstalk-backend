"""SA-portal Global CCA hub backend endpoints (Batch 8a, 2026-05-11).

Mirrors the CA-portal /client/{cid}/cca/{crops,timelines,practices}
shape but at Global scope (Package.client_id IS NULL), feeding the
SA-portal four-screen hub at /advisory/global/{crops,packages,
timelines,practices}. Plus the existing /advisory/global/packages
gains a crop_cosh_id filter for the Crops → Packages drill-down.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageStatus, PackageType, Practice, PracticeL0,
    Timeline, TimelineFromType,
)
from app.modules.advisory.router import (
    global_cca_list_crops, global_cca_list_practices,
    global_cca_list_timelines, list_global_packages,
)
from app.modules.sync.models import CoshCoreItem
from app.services.cosh_constants import COSH_BIOLOGICAL_NAMES_CORE
from tests.conftest import requires_docker
from tests.factories import make_crop_reference, make_user


async def _global_pkg_with_content(
    db, *, name: str, crop_cosh_id: str,
    status=PackageStatus.ACTIVE, with_timeline: bool = True,
    with_practice: bool = True,
):
    pkg = Package(
        client_id=None, name=name, crop_cosh_id=crop_cosh_id,
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=status,
    )
    db.add(pkg)
    await db.flush()
    if with_timeline:
        tl = Timeline(
            package_id=pkg.id, name=f"TL-{uuid.uuid4().hex[:4]}",
            from_type=TimelineFromType.DAS, from_value=0, to_value=15,
        )
        db.add(tl)
        await db.flush()
        if with_practice:
            db.add(Practice(
                timeline_id=tl.id, l0_type=PracticeL0.INPUT,
                l1_type="FERTILIZER", l2_type="UREA", display_order=0,
            ))
            await db.flush()
    return pkg


# ── /advisory/global/cca/crops ─────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_global_crops_returns_full_cosh_universe(db):
    """Every Cosh-classified crop is returned, including ones with
    zero Global Packages. CMs need the full universe to author
    against any crop."""
    user = await make_user(db, name="CM")
    await make_crop_reference(db, "crop:tomato", name="Tomato")
    await make_crop_reference(db, "crop:paddy", name="Paddy")
    await make_crop_reference(db, "crop:onion", name="Onion")
    # Author packages only for tomato.
    await _global_pkg_with_content(
        db, name=f"P-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:tomato",
    )
    await db.commit()

    out = await global_cca_list_crops(db=db, current_user=user)
    by_id = {row["crop_cosh_id"]: row for row in out}
    assert "crop:tomato" in by_id
    assert "crop:paddy" in by_id
    assert "crop:onion" in by_id
    assert by_id["crop:tomato"]["package_counts"].get("ACTIVE") == 1
    # Crops without packages still appear, with empty counts.
    assert by_id["crop:paddy"]["package_counts"] == {}


@requires_docker
@pytest.mark.asyncio
async def test_global_crops_breakdown_by_status(db):
    """Per-crop package_counts is keyed by status string."""
    user = await make_user(db, name="CM")
    await make_crop_reference(db, "crop:tomato", name="Tomato")
    await _global_pkg_with_content(
        db, name=f"P1-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:tomato",
        status=PackageStatus.ACTIVE,
    )
    await _global_pkg_with_content(
        db, name=f"P2-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:tomato",
        status=PackageStatus.DRAFT,
    )
    await _global_pkg_with_content(
        db, name=f"P3-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:tomato",
        status=PackageStatus.INACTIVE,
    )
    await db.commit()

    out = await global_cca_list_crops(db=db, current_user=user)
    tomato = next(r for r in out if r["crop_cosh_id"] == "crop:tomato")
    assert tomato["package_counts"]["ACTIVE"] == 1
    assert tomato["package_counts"]["DRAFT"] == 1
    assert tomato["package_counts"]["INACTIVE"] == 1


@requires_docker
@pytest.mark.asyncio
async def test_global_crops_ignores_local_packages(db):
    """Client-scoped Packages do NOT count toward the global
    package_counts. The endpoint is strictly Global."""
    from tests.factories import make_client, make_package
    user = await make_user(db, name="CM")
    await make_crop_reference(db, "crop:tomato", name="Tomato")
    client = await make_client(db)
    await make_package(db, client, crop_cosh_id="crop:tomato")
    # No Global packages → tomato should show empty counts.
    await db.commit()

    out = await global_cca_list_crops(db=db, current_user=user)
    tomato = next(r for r in out if r["crop_cosh_id"] == "crop:tomato")
    assert tomato["package_counts"] == {}


# ── /advisory/global/packages?crop_cosh_id= ────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_global_packages_filtered_by_crop(db):
    """crop_cosh_id filter narrows the global packages list — used by
    the SA-portal Crops → Packages drill-down."""
    user = await make_user(db, name="CM")
    tomato_a = await _global_pkg_with_content(
        db, name=f"TomatoA-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:tomato",
    )
    tomato_b = await _global_pkg_with_content(
        db, name=f"TomatoB-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:tomato",
    )
    await _global_pkg_with_content(
        db, name=f"Paddy-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:paddy",
    )
    await db.commit()

    out = await list_global_packages(
        crop_cosh_id="crop:tomato", db=db, current_user=user,
    )
    assert {p.id for p in out} == {tomato_a.id, tomato_b.id}


# ── /advisory/global/cca/timelines ─────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_global_timelines_returns_only_global_scope(db):
    """Local Package timelines must NOT leak into the Global hub."""
    from tests.factories import make_client, make_package, make_timeline
    user = await make_user(db, name="CM")
    gpkg = await _global_pkg_with_content(
        db, name=f"G-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:tomato",
    )
    local_client = await make_client(db)
    local_pkg = await make_package(
        db, local_client, crop_cosh_id="crop:tomato",
    )
    await make_timeline(db, local_pkg, name="LocalTL")
    await db.commit()

    out = await global_cca_list_timelines(db=db, current_user=user)
    package_ids = {r["package_id"] for r in out}
    assert gpkg.id in package_ids
    assert local_pkg.id not in package_ids


@requires_docker
@pytest.mark.asyncio
async def test_global_timelines_filters_by_crop_and_package(db):
    user = await make_user(db, name="CM")
    tomato_pkg = await _global_pkg_with_content(
        db, name=f"T-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:tomato",
    )
    paddy_pkg = await _global_pkg_with_content(
        db, name=f"P-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:paddy",
    )
    await db.commit()

    by_crop = await global_cca_list_timelines(
        crop_cosh_id="crop:tomato", db=db, current_user=user,
    )
    assert {r["package_id"] for r in by_crop} == {tomato_pkg.id}

    by_pkg = await global_cca_list_timelines(
        package_id=paddy_pkg.id, db=db, current_user=user,
    )
    assert {r["package_id"] for r in by_pkg} == {paddy_pkg.id}


# ── /advisory/global/cca/practices ─────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_global_practices_returns_paginated_global_practices(db):
    user = await make_user(db, name="CM")
    pkg = await _global_pkg_with_content(
        db, name=f"P-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:tomato",
    )
    await db.commit()

    out = await global_cca_list_practices(db=db, current_user=user)
    assert out["total"] >= 1
    assert out["limit"] == 100
    assert out["offset"] == 0
    assert any(p["package_id"] == pkg.id for p in out["items"])


@requires_docker
@pytest.mark.asyncio
async def test_global_practices_excludes_local_scope(db):
    """Local Package practices must not appear in the Global feed."""
    from tests.factories import (
        make_client, make_element, make_package, make_practice,
        make_timeline,
    )
    user = await make_user(db, name="CM")
    gpkg = await _global_pkg_with_content(
        db, name=f"G-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:tomato",
    )
    local_client = await make_client(db)
    local_pkg = await make_package(
        db, local_client, crop_cosh_id="crop:tomato",
    )
    local_tl = await make_timeline(db, local_pkg, name="LocalTL")
    local_p = await make_practice(
        db, local_tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2="DAP",
    )
    await make_element(db, local_p, value="60")
    await db.commit()

    out = await global_cca_list_practices(db=db, current_user=user)
    package_ids = {p["package_id"] for p in out["items"]}
    assert gpkg.id in package_ids
    assert local_pkg.id not in package_ids


@requires_docker
@pytest.mark.asyncio
async def test_global_practices_filters_by_l1(db):
    """L1 type filter (e.g. FERTILIZER, PESTICIDE) drives the chip-
    based narrowing on the Practices screen."""
    user = await make_user(db, name="CM")
    pkg = await _global_pkg_with_content(
        db, name=f"P-{uuid.uuid4().hex[:4]}", crop_cosh_id="crop:tomato",
    )
    # Add a second practice with a different L1.
    tl = (await db.execute(
        select(Timeline).where(Timeline.package_id == pkg.id)
    )).scalar_one()
    db.add(Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="PESTICIDE", l2_type="MANCOZEB", display_order=1,
    ))
    await db.commit()

    fertilizer_only = await global_cca_list_practices(
        l1="FERTILIZER", db=db, current_user=user,
    )
    assert all(p["l1_type"] == "FERTILIZER" for p in fertilizer_only["items"])
    assert fertilizer_only["total"] >= 1
