"""SA-portal push-status helper endpoint (Batch 4 of the multi-row
versioning work locked 2026-05-11).

Verifies `GET /advisory/global/packages/{pkg_id}/push-status`:
  • Lists every client the calling CM can edit, with per-client
    push/publish state.
  • already_pushed reflects any Local row in the lineage,
    regardless of status (CM_PUSH never repeats, so its presence
    is a stable "first contact happened" signal).
  • pushed_at = earliest Local row's created_at.
  • latest_local_published_at = max(published_at) across PUBLISHED
    history rows in the lineage; None when nothing has been
    published locally yet (CM-push DRAFT still sitting unpublished).
  • has_pending_draft = true when a DRAFT exists in the lineage.
  • CMs without any active assignments get an empty list.
  • Clients the CM isn't assigned to don't appear in the response
    — defends against cross-client leakage.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageStatus, PackageType, Practice, PracticeL0,
    Timeline, TimelineFromType,
)
from app.modules.advisory.router import (
    clone_to_draft, get_global_package_push_status, publish_package,
    pull_global_package, push_global_package,
)
from app.modules.clients.models import ClientCrop, ClientUserRole
from app.modules.platform.models import StatusEnum
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_cm_assignment, make_user,
)


async def _seed_global(db, *, name: str | None = None):
    pkg = Package(
        client_id=None,
        name=name or f"GP-{uuid.uuid4().hex[:6]}",
        crop_cosh_id="crop:test",
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.ACTIVE,
    )
    db.add(pkg)
    await db.flush()
    tl = Timeline(
        package_id=pkg.id, name="GTL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=15,
    )
    db.add(tl)
    await db.flush()
    db.add(Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="FERTILIZER", l2_type="UREA", display_order=0,
    ))
    await db.flush()
    return pkg


# ── Happy path: CM sees both assigned clients with correct state ────────────

@requires_docker
@pytest.mark.asyncio
async def test_push_status_lists_all_assigned_clients(db):
    """CM has 2 clients. Pushed to one, not the other.
    Endpoint lists both with correct already_pushed booleans."""
    cm = await make_user(db, name=f"CM-{uuid.uuid4().hex[:4]}")
    client_a = await make_client(db, full_name="Alpha Co")
    client_b = await make_client(db, full_name="Beta Co")
    await make_cm_assignment(db, user=cm, client=client_a)
    await make_cm_assignment(db, user=cm, client=client_b)
    gpkg = await _seed_global(db)
    db.add(ClientCrop(client_id=client_a.id, crop_cosh_id=gpkg.crop_cosh_id))
    db.add(ClientCrop(client_id=client_b.id, crop_cosh_id=gpkg.crop_cosh_id))
    await db.commit()

    # Push to A only.
    await push_global_package(
        client_id=client_a.id, pkg_id=gpkg.id, db=db, current_user=cm,
    )

    out = await get_global_package_push_status(
        pkg_id=gpkg.id, db=db, current_user=cm,
    )
    by_id = {e["client_id"]: e for e in out}
    assert client_a.id in by_id and client_b.id in by_id
    assert by_id[client_a.id]["already_pushed"] is True
    assert by_id[client_a.id]["pushed_at"] is not None
    assert by_id[client_a.id]["has_pending_draft"] is True  # DRAFT from push
    assert by_id[client_a.id]["latest_local_published_at"] is None  # SE hasn't published
    assert by_id[client_b.id]["already_pushed"] is False
    assert by_id[client_b.id]["pushed_at"] is None
    assert by_id[client_b.id]["has_pending_draft"] is False


@requires_docker
@pytest.mark.asyncio
async def test_push_status_after_se_publishes(db):
    """Once the SE publishes the pushed DRAFT, has_pending_draft
    flips to False and latest_local_published_at is set."""
    cm = await make_user(db, name=f"CM-{uuid.uuid4().hex[:4]}")
    se = await make_user(db, name=f"SE-{uuid.uuid4().hex[:4]}")
    client = await make_client(db, full_name="Pubco")
    await make_cm_assignment(db, user=cm, client=client)
    cu = await make_client_user(db, user=se, client=client)
    cu.role = ClientUserRole.SUBJECT_EXPERT
    cu.status = StatusEnum.ACTIVE
    gpkg = await _seed_global(db)
    db.add(ClientCrop(client_id=client.id, crop_cosh_id=gpkg.crop_cosh_id))
    await db.commit()

    pushed = await push_global_package(
        client_id=client.id, pkg_id=gpkg.id, db=db, current_user=cm,
    )
    # Author needs at least 1 PackageLocation + PackageAuthor for
    # publish-readiness. Add them inline.
    from app.modules.advisory.models import PackageLocation, PackageAuthor
    db.add(PackageLocation(
        package_id=pushed.id, state_cosh_id="state:test",
        district_cosh_id=f"district:test:{uuid.uuid4().hex[:4]}",
    ))
    db.add(PackageAuthor(package_id=pushed.id, user_id=se.id))
    await db.commit()

    await publish_package(
        client_id=client.id, package_id=pushed.id,
        db=db, current_user=se,
    )

    out = await get_global_package_push_status(
        pkg_id=gpkg.id, db=db, current_user=cm,
    )
    entry = next(e for e in out if e["client_id"] == client.id)
    assert entry["already_pushed"] is True
    assert entry["has_pending_draft"] is False
    assert entry["latest_local_published_at"] is not None


@requires_docker
@pytest.mark.asyncio
async def test_push_status_with_pending_pull_draft(db):
    """SE has pulled a refresh but hasn't published it yet:
    has_pending_draft=True; latest_local_published_at still
    reflects the prior published version."""
    cm = await make_user(db, name=f"CM-{uuid.uuid4().hex[:4]}")
    se = await make_user(db, name=f"SE-{uuid.uuid4().hex[:4]}")
    client = await make_client(db, full_name="Pullco")
    await make_cm_assignment(db, user=cm, client=client)
    cu = await make_client_user(db, user=se, client=client)
    cu.role = ClientUserRole.SUBJECT_EXPERT
    cu.status = StatusEnum.ACTIVE
    gpkg = await _seed_global(db)
    db.add(ClientCrop(client_id=client.id, crop_cosh_id=gpkg.crop_cosh_id))
    await db.commit()

    pushed = await push_global_package(
        client_id=client.id, pkg_id=gpkg.id, db=db, current_user=cm,
    )
    from app.modules.advisory.models import PackageLocation, PackageAuthor
    db.add(PackageLocation(
        package_id=pushed.id, state_cosh_id="state:test",
        district_cosh_id=f"district:test:{uuid.uuid4().hex[:4]}",
    ))
    db.add(PackageAuthor(package_id=pushed.id, user_id=se.id))
    await db.commit()
    await publish_package(
        client_id=client.id, package_id=pushed.id,
        db=db, current_user=se,
    )
    # SE pulls a refresh.
    await pull_global_package(
        client_id=client.id, pkg_id=gpkg.id, db=db, current_user=se,
    )

    out = await get_global_package_push_status(
        pkg_id=gpkg.id, db=db, current_user=cm,
    )
    entry = next(e for e in out if e["client_id"] == client.id)
    assert entry["has_pending_draft"] is True
    assert entry["latest_local_published_at"] is not None  # prior publish


# ── Auth + isolation ────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_push_status_excludes_clients_not_assigned_to_caller(db):
    """A CM seeing the push-status only sees their assigned clients.
    A different CM's clients must not leak."""
    cm_a = await make_user(db, name=f"CMA-{uuid.uuid4().hex[:4]}")
    cm_b = await make_user(db, name=f"CMB-{uuid.uuid4().hex[:4]}")
    client_a = await make_client(db, full_name="AClient")
    client_b = await make_client(db, full_name="BClient")
    await make_cm_assignment(db, user=cm_a, client=client_a)
    await make_cm_assignment(db, user=cm_b, client=client_b)
    gpkg = await _seed_global(db)
    await db.commit()

    out_a = await get_global_package_push_status(
        pkg_id=gpkg.id, db=db, current_user=cm_a,
    )
    out_b = await get_global_package_push_status(
        pkg_id=gpkg.id, db=db, current_user=cm_b,
    )
    assert [e["client_id"] for e in out_a] == [client_a.id]
    assert [e["client_id"] for e in out_b] == [client_b.id]


@requires_docker
@pytest.mark.asyncio
async def test_push_status_empty_for_caller_with_no_assignments(db):
    """A user with no active CMClientAssignment gets [], not 403.
    The SA-portal UI can render this as an empty state."""
    user = await make_user(db, name=f"NoAssign-{uuid.uuid4().hex[:4]}")
    gpkg = await _seed_global(db)
    await db.commit()

    out = await get_global_package_push_status(
        pkg_id=gpkg.id, db=db, current_user=user,
    )
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_push_status_404_for_missing_global(db):
    cm = await make_user(db, name=f"CM-{uuid.uuid4().hex[:4]}")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await get_global_package_push_status(
            pkg_id="ghost-id", db=db, current_user=cm,
        )
    assert exc.value.status_code == 404


# ── Ordering + multi-row lineage tracking ───────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_push_status_not_pushed_clients_sort_first(db):
    """Stable ordering: not-yet-pushed clients (the CM's actionable
    targets) come before already-pushed ones; within each bucket,
    alphabetical by name."""
    cm = await make_user(db, name=f"CM-{uuid.uuid4().hex[:4]}")
    client_x = await make_client(db, full_name="Xenon")  # not pushed
    client_a = await make_client(db, full_name="Argon")  # pushed
    client_m = await make_client(db, full_name="Mercury")  # not pushed
    for c in (client_x, client_a, client_m):
        await make_cm_assignment(db, user=cm, client=c)
    gpkg = await _seed_global(db)
    db.add(ClientCrop(client_id=client_a.id, crop_cosh_id=gpkg.crop_cosh_id))
    await db.commit()
    await push_global_package(
        client_id=client_a.id, pkg_id=gpkg.id, db=db, current_user=cm,
    )

    out = await get_global_package_push_status(
        pkg_id=gpkg.id, db=db, current_user=cm,
    )
    names = [e["client_name"] for e in out]
    assert names == ["Mercury", "Xenon", "Argon"], (
        "not-yet-pushed first (alphabetical), then pushed clients"
    )
