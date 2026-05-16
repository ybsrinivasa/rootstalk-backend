"""Global Publish content gates (Batch 39L-a, 2026-05-16).

Per CM-stated rules: SA-portal publish requires
  - at least one active Timeline on the Package
  - every active Timeline carries at least one Practice
The endpoint returns 422 with the COMPLETE list of failures so the
SA portal can render one consolidated checklist.

The CA-side publish gate (`assert_package_publish_ready`) is
deliberately LESS strict here — districts / PV / siblings /
authors are CA concerns the CM doesn't deal with at the Global
level.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.advisory.models import (
    Package, PackageStatus, PackageType, Practice, PracticeL0,
    Timeline, TimelineFromType, TimelineStatus,
)
from app.modules.advisory.router import publish_global_package
from tests.conftest import requires_docker
from tests.factories import make_user


async def _seed_global_pkg(db, *, name: str = "GP", duration: int = 30) -> Package:
    pkg = Package(
        client_id=None, name=name,
        crop_cosh_id="crop:tomato",
        package_type=PackageType.ANNUAL, duration_days=duration,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.DRAFT,
    )
    db.add(pkg)
    await db.flush()
    return pkg


async def _add_timeline(
    db, pkg: Package, *, name: str = "T1",
    from_value: int = 0, to_value: int = 10,
    status: TimelineStatus = TimelineStatus.ACTIVE,
) -> Timeline:
    tl = Timeline(
        package_id=pkg.id, name=name,
        from_type=TimelineFromType.DAS,
        from_value=from_value, to_value=to_value,
        status=status,
    )
    db.add(tl)
    await db.flush()
    return tl


async def _add_practice(db, tl: Timeline) -> Practice:
    p = Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
    )
    db.add(p)
    await db.flush()
    return p


@requires_docker
@pytest.mark.asyncio
async def test_publish_blocks_when_no_timelines(db):
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await publish_global_package(
            pkg_id=pkg.id, db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    body = exc.value.detail
    assert body["code"] == "publish_blocked"
    codes = [m["code"] for m in body["missing"]]
    assert "publish_no_timelines" in codes


@requires_docker
@pytest.mark.asyncio
async def test_publish_blocks_when_a_timeline_has_no_practices(db):
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db)
    tl1 = await _add_timeline(db, pkg, name="T1")
    tl2 = await _add_timeline(db, pkg, name="T2", from_value=10, to_value=20)
    await _add_practice(db, tl1)  # T2 stays empty
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await publish_global_package(
            pkg_id=pkg.id, db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    body = exc.value.detail
    assert body["code"] == "publish_blocked"
    empty = [m for m in body["missing"] if m["code"] == "publish_timeline_empty"]
    assert len(empty) == 1
    assert empty[0]["extra"]["timeline_name"] == "T2"


@requires_docker
@pytest.mark.asyncio
async def test_publish_skips_inactive_timelines(db):
    """An INACTIVE Timeline doesn't count toward the gates — neither
    its presence (the "at least one timeline" check), nor its
    practice-count requirement."""
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db)
    tl_active = await _add_timeline(db, pkg, name="Active")
    await _add_timeline(
        db, pkg, name="Stale", from_value=10, to_value=20,
        status=TimelineStatus.INACTIVE,
    )
    await _add_practice(db, tl_active)
    await db.commit()
    out = await publish_global_package(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    assert out.status == PackageStatus.ACTIVE


@requires_docker
@pytest.mark.asyncio
async def test_publish_succeeds_when_all_gates_pass(db):
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db)
    tl = await _add_timeline(db, pkg)
    await _add_practice(db, tl)
    await db.commit()
    out = await publish_global_package(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    assert out.status == PackageStatus.ACTIVE
    assert out.version == 1
    assert out.published_at is not None


@requires_docker
@pytest.mark.asyncio
async def test_publish_reports_all_failures_in_one_response(db):
    """The CM sees the complete checklist on one round-trip — every
    empty timeline surfaces, not just the first."""
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db)
    await _add_timeline(db, pkg, name="EmptyA")
    await _add_timeline(db, pkg, name="EmptyB", from_value=10, to_value=20)
    await _add_timeline(db, pkg, name="EmptyC", from_value=20, to_value=30)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await publish_global_package(
            pkg_id=pkg.id, db=db, current_user=user,
        )
    body = exc.value.detail
    empty = [m for m in body["missing"] if m["code"] == "publish_timeline_empty"]
    names = sorted(m["extra"]["timeline_name"] for m in empty)
    assert names == ["EmptyA", "EmptyB", "EmptyC"]
