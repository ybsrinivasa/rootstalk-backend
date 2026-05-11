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
from tests.factories import make_client, make_cm_assignment, make_user


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
        area_or_plant="AREA_WISE",
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
    await make_cm_assignment(db, user=user, client=client)
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
    await make_cm_assignment(db, user=user, client=client)
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
    await make_cm_assignment(db, user=user, client=client)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await fork_global_package(
            client_id=client.id, pkg_id=pkg.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_package_not_published"


# ── Package fork: once-per-client-lifetime ─────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_fork_package_initial_succeeds(db):
    user = await make_user(db, name="CM")
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
    await make_cm_assignment(db, user=user, client=client)
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
async def test_refork_same_package_into_same_client_permanently_blocked(db):
    """A given global package can be forked into a given client at
    most ONCE in the client's lifetime. The second attempt is hard-
    blocked with a stable 409 — there is no force=true overwrite.
    SE/CM either edits the existing local copy or deletes it (a
    separate operation) before any future re-import can be attempted."""
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db, name="GPaddy", status=PackageStatus.ACTIVE)
    tl = Timeline(
        package_id=pkg.id, name="GTL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=15,
    )
    db.add(tl)
    await db.flush()
    client = await make_client(db, full_name="C1")
    await make_cm_assignment(db, user=user, client=client)
    await db.commit()

    # Initial fork.
    await fork_global_package(
        client_id=client.id, pkg_id=pkg.id,
        db=db, current_user=user,
    )

    # Re-fork is permanently blocked.
    with pytest.raises(HTTPException) as exc:
        await fork_global_package(
            client_id=client.id, pkg_id=pkg.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "package_already_pushed"
    assert exc.value.detail["existing"]["timeline_count"] == 1


# ── CM authorisation ────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_fork_rejected_when_user_has_no_cm_assignment(db):
    """Caller without an active CMClientAssignment for the target
    client gets 403 even if the global package is ACTIVE."""
    user = await make_user(db, name="RandomUser")
    pkg = await _seed_global_pkg(db, name="GPaddy", status=PackageStatus.ACTIVE)
    client = await make_client(db, full_name="ClientA")
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await fork_global_package(
            client_id=client.id, pkg_id=pkg.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "cm_assignment_required"


@requires_docker
@pytest.mark.asyncio
async def test_fork_rejected_when_cm_assigned_to_different_client(db):
    """A CM assigned to client B cannot fork into client A."""
    user = await make_user(db, name="CM-of-B")
    pkg = await _seed_global_pkg(db, name="GPaddy", status=PackageStatus.ACTIVE)
    client_a = await make_client(db, full_name="ClientA")
    client_b = await make_client(db, full_name="ClientB")
    await make_cm_assignment(db, user=user, client=client_b)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await fork_global_package(
            client_id=client_a.id, pkg_id=pkg.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_import_global_pg_requires_cm_assignment(db):
    user = await make_user(db, name="RandomUser")
    pg = await _seed_global_pg(db, status="ACTIVE")
    client = await make_client(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await import_global_pg(
            client_id=client.id, global_pg_id=pg.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "cm_assignment_required"


