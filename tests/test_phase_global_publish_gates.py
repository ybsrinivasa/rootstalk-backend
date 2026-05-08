"""Global → Local publish gates + package-fork overwrite tests.

Two related guarantees these tests pin:

  1. Lists of globals visible to CA-portal SEs default to ACTIVE only
     — DRAFT and INACTIVE rows are hidden. CMs can opt-in via
     `include_drafts=true` for their own admin views.

  2. Imports of non-ACTIVE globals are rejected with a stable error
     code so the CA portal can surface "ask the CM to publish first".

  3. fork_global_package now mirrors the PG/SP overwrite contract —
     `?force=true` wipes existing local content + re-forks; default
     refuses with structured 409.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageStatus, PackageType, PGRecommendation,
    PGTimeline, Practice, PracticeL0, Timeline, TimelineFromType,
)
from app.modules.advisory.router import (
    fork_global_package, import_global_pg, list_global_packages,
    list_global_pg,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


# ── Seed helpers ────────────────────────────────────────────────────────────

async def _seed_global_pkg(
    db, *, name, status: PackageStatus = PackageStatus.ACTIVE,
) -> Package:
    pkg = Package(
        client_id=None,
        crop_cosh_id="crop:paddy",
        name=name,
        package_type=PackageType.ANNUAL,
        duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=status,
    )
    db.add(pkg)
    await db.flush()
    return pkg


async def _seed_global_pg(
    db, *, problem_group_cosh_id="pg:fungal", status: str = "ACTIVE",
) -> PGRecommendation:
    pg = PGRecommendation(
        problem_group_cosh_id=problem_group_cosh_id,
        client_id=None,
        application_type="SPRAY",
        status=status,
    )
    db.add(pg)
    await db.flush()
    return pg


# ── List-endpoint publish gates ─────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_global_packages_default_excludes_drafts(db):
    user = await make_user(db, name="SE")
    await _seed_global_pkg(db, name="ActivePkg", status=PackageStatus.ACTIVE)
    await _seed_global_pkg(db, name="DraftPkg", status=PackageStatus.DRAFT)
    await _seed_global_pkg(db, name="InactivePkg", status=PackageStatus.INACTIVE)
    await db.commit()

    out = await list_global_packages(db=db, current_user=user)
    names = {p.name for p in out}
    assert names == {"ActivePkg"}


@requires_docker
@pytest.mark.asyncio
async def test_list_global_packages_include_drafts_returns_all(db):
    """CMs override the default to see DRAFT/INACTIVE rows in admin views."""
    user = await make_user(db, name="CM")
    await _seed_global_pkg(db, name="ActivePkg", status=PackageStatus.ACTIVE)
    await _seed_global_pkg(db, name="DraftPkg", status=PackageStatus.DRAFT)
    await db.commit()

    out = await list_global_packages(
        include_drafts=True, db=db, current_user=user,
    )
    names = {p.name for p in out}
    assert names == {"ActivePkg", "DraftPkg"}


@requires_docker
@pytest.mark.asyncio
async def test_list_global_pg_default_excludes_drafts(db):
    user = await make_user(db, name="SE")
    await _seed_global_pg(db, problem_group_cosh_id="pg:1", status="ACTIVE")
    await _seed_global_pg(db, problem_group_cosh_id="pg:2", status="DRAFT")
    await _seed_global_pg(db, problem_group_cosh_id="pg:3", status="INACTIVE")
    await db.commit()

    out = await list_global_pg(db=db, current_user=user)
    pgs = {p.problem_group_cosh_id for p in out}
    assert pgs == {"pg:1"}


@requires_docker
@pytest.mark.asyncio
async def test_list_global_pg_include_drafts_returns_all(db):
    user = await make_user(db, name="CM")
    await _seed_global_pg(db, problem_group_cosh_id="pg:1", status="ACTIVE")
    await _seed_global_pg(db, problem_group_cosh_id="pg:2", status="DRAFT")
    await db.commit()

    out = await list_global_pg(include_drafts=True, db=db, current_user=user)
    pgs = {p.problem_group_cosh_id for p in out}
    assert pgs == {"pg:1", "pg:2"}


# ── Import-endpoint publish gates ───────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_import_draft_pg_rejected_422(db):
    user = await make_user(db, name="SE")
    pg = await _seed_global_pg(db, status="DRAFT")
    client = await make_client(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await import_global_pg(
            client_id=client.id, global_pg_id=pg.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_pg_not_published"
    assert exc.value.detail["current_status"] == "DRAFT"


@requires_docker
@pytest.mark.asyncio
async def test_import_inactive_pg_also_rejected(db):
    user = await make_user(db, name="SE")
    pg = await _seed_global_pg(db, status="INACTIVE")
    client = await make_client(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await import_global_pg(
            client_id=client.id, global_pg_id=pg.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_pg_not_published"


@requires_docker
@pytest.mark.asyncio
async def test_fork_draft_package_rejected_422(db):
    user = await make_user(db, name="SE")
    pkg = await _seed_global_pkg(db, name="DraftPkg", status=PackageStatus.DRAFT)
    client = await make_client(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await fork_global_package(
            client_id=client.id, pkg_id=pkg.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_package_not_published"


# ── Package-fork overwrite (?force=true) ────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_fork_package_initial_succeeds(db):
    user = await make_user(db, name="SE")
    pkg = await _seed_global_pkg(db, name="GPaddy", status=PackageStatus.ACTIVE)
    # One Timeline + one Practice on the global so we can verify deep-copy.
    tl = Timeline(
        package_id=pkg.id, name="GTL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=15,
    )
    db.add(tl)
    await db.flush()
    db.add(Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
        display_order=1, is_special_input=False,
    ))
    await db.flush()

    client = await make_client(db, full_name="C1")
    await db.commit()

    out = await fork_global_package(
        client_id=client.id, pkg_id=pkg.id,
        db=db, current_user=user,
    )
    assert out.parent_global_id == pkg.id
    tls = (await db.execute(
        select(Timeline).where(Timeline.package_id == out.id)
    )).scalars().all()
    assert len(tls) == 1


@requires_docker
@pytest.mark.asyncio
async def test_refork_package_without_force_returns_409(db):
    user = await make_user(db, name="SE")
    pkg = await _seed_global_pkg(db, name="GPaddy", status=PackageStatus.ACTIVE)
    tl = Timeline(
        package_id=pkg.id, name="GTL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=15,
    )
    db.add(tl)
    await db.flush()
    client = await make_client(db, full_name="C1")
    await db.commit()

    # Initial fork.
    await fork_global_package(
        client_id=client.id, pkg_id=pkg.id,
        db=db, current_user=user,
    )

    # Re-fork without force fails with the structured 409.
    with pytest.raises(HTTPException) as exc:
        await fork_global_package(
            client_id=client.id, pkg_id=pkg.id,
            force=False, db=db, current_user=user,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "import_would_overwrite"
    assert exc.value.detail["existing"]["timeline_count"] == 1


@requires_docker
@pytest.mark.asyncio
async def test_refork_package_with_force_overwrites_recipe_content(db):
    """Force re-fork wipes Timelines/Practices/Elements and re-copies
    from the global. Package row is preserved (same id)."""
    user = await make_user(db, name="SE")
    pkg = await _seed_global_pkg(db, name="GPaddy", status=PackageStatus.ACTIVE)
    tl = Timeline(
        package_id=pkg.id, name="GTL-original",
        from_type=TimelineFromType.DAS, from_value=0, to_value=15,
    )
    db.add(tl)
    await db.flush()
    client = await make_client(db, full_name="C1")
    await db.commit()

    forked = await fork_global_package(
        client_id=client.id, pkg_id=pkg.id,
        db=db, current_user=user,
    )
    fork_id_before = forked.id

    # SE adds a custom timeline locally.
    db.add(Timeline(
        package_id=forked.id, name="SE custom",
        from_type=TimelineFromType.DAS, from_value=20, to_value=30,
    ))
    await db.commit()
    assert len((await db.execute(
        select(Timeline).where(Timeline.package_id == forked.id)
    )).scalars().all()) == 2

    # Force re-fork — custom wiped, only global's timeline remains.
    out = await fork_global_package(
        client_id=client.id, pkg_id=pkg.id,
        force=True, db=db, current_user=user,
    )
    assert out.id == fork_id_before  # row preserved

    after_tls = (await db.execute(
        select(Timeline).where(Timeline.package_id == out.id)
    )).scalars().all()
    assert len(after_tls) == 1
    assert after_tls[0].name == "GTL-original"
