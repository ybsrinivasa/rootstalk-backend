"""Lineage endpoint for the CA-portal version-history navigator
(Batch 7 follow-on for the multi-row work locked 2026-05-11).

GET /client/{cid}/packages/{pkg_id}/lineage returns all rows in
the lineage (same client + crop + name). Sorted DRAFTs-first,
then PUBLISHED → INACTIVE by version desc.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.modules.advisory.models import (
    Package, PackageCreatedVia, PackageStatus, PackageType,
)
from app.modules.advisory.router import get_package_lineage
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_user,
)


async def _local_pkg(
    db, *, client, name: str, status, version: int = 1,
    published_at=None, created_via=None,
):
    p = Package(
        client_id=client.id, name=name, crop_cosh_id="crop:test",
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=status, version=version,
        published_at=published_at, created_via=created_via,
    )
    db.add(p)
    await db.flush()
    return p


# ── Happy path ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_lineage_returns_all_rows_sorted_drafts_first(db):
    """Three INACTIVE history rows + one ACTIVE + one DRAFT → all 5
    returned. DRAFT first, then ACTIVE, then INACTIVE history by
    version desc."""
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_client_user(db, user=se, client=client)
    now = datetime.now(timezone.utc)
    v1 = await _local_pkg(db, client=client, name="Tomato PoP",
                          status=PackageStatus.INACTIVE, version=1,
                          published_at=now)
    v2 = await _local_pkg(db, client=client, name="Tomato PoP",
                          status=PackageStatus.INACTIVE, version=2,
                          published_at=now)
    v3 = await _local_pkg(db, client=client, name="Tomato PoP",
                          status=PackageStatus.INACTIVE, version=3,
                          published_at=now)
    v4_active = await _local_pkg(db, client=client, name="Tomato PoP",
                                 status=PackageStatus.ACTIVE, version=4,
                                 published_at=now)
    v5_draft = await _local_pkg(db, client=client, name="Tomato PoP",
                                status=PackageStatus.DRAFT, version=1,
                                created_via=PackageCreatedVia.SE_EDIT_DRAFT)
    await db.commit()

    out = await get_package_lineage(
        client_id=client.id, package_id=v4_active.id,
        db=db, current_user=se,
    )
    assert [r["id"] for r in out] == [v5_draft.id, v4_active.id, v3.id, v2.id, v1.id]
    current = next(r for r in out if r["is_current"])
    assert current["id"] == v4_active.id


@requires_docker
@pytest.mark.asyncio
async def test_lineage_carries_created_via_and_source_version_id(db):
    """A rollback-publish row carries source_version_id pointing
    at the historical row whose content it republished. The
    endpoint surfaces that for the audit trail."""
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_client_user(db, user=se, client=client)
    now = datetime.now(timezone.utc)
    v1 = await _local_pkg(db, client=client, name="P",
                          status=PackageStatus.INACTIVE, version=1,
                          published_at=now,
                          created_via=PackageCreatedVia.CM_PUSH)
    v2 = await _local_pkg(db, client=client, name="P",
                          status=PackageStatus.ACTIVE, version=2,
                          published_at=now,
                          created_via=PackageCreatedVia.SE_ROLLBACK_PUBLISH)
    v2.source_version_id = v1.id
    await db.commit()

    out = await get_package_lineage(
        client_id=client.id, package_id=v2.id, db=db, current_user=se,
    )
    by_id = {r["id"]: r for r in out}
    assert by_id[v2.id]["created_via"] == "SE_ROLLBACK_PUBLISH"
    assert by_id[v2.id]["source_version_id"] == v1.id
    assert by_id[v1.id]["created_via"] == "CM_PUSH"
    assert by_id[v1.id]["source_version_id"] is None


# ── Isolation ────────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_lineage_excludes_different_name_same_crop(db):
    """Another PoP at the same client + crop but with a different
    name is its own lineage — must not appear in this one's
    history."""
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_client_user(db, user=se, client=client)
    now = datetime.now(timezone.utc)
    target = await _local_pkg(db, client=client, name="A",
                              status=PackageStatus.ACTIVE, version=1,
                              published_at=now)
    other = await _local_pkg(db, client=client, name="B",
                             status=PackageStatus.ACTIVE, version=1,
                             published_at=now)
    await db.commit()

    out = await get_package_lineage(
        client_id=client.id, package_id=target.id, db=db, current_user=se,
    )
    assert [r["id"] for r in out] == [target.id]
    assert other.id not in {r["id"] for r in out}


@requires_docker
@pytest.mark.asyncio
async def test_lineage_excludes_other_clients_rows(db):
    """Two clients each with a 'Tomato PoP'. Caller's lineage must
    only show their own client's rows."""
    se_a = await make_user(db, name="SE-A")
    client_a = await make_client(db)
    client_b = await make_client(db)
    await make_client_user(db, user=se_a, client=client_a)
    now = datetime.now(timezone.utc)
    a_pkg = await _local_pkg(db, client=client_a, name="Tomato PoP",
                             status=PackageStatus.ACTIVE, version=1,
                             published_at=now)
    b_pkg = await _local_pkg(db, client=client_b, name="Tomato PoP",
                             status=PackageStatus.ACTIVE, version=1,
                             published_at=now)
    await db.commit()

    out = await get_package_lineage(
        client_id=client_a.id, package_id=a_pkg.id,
        db=db, current_user=se_a,
    )
    assert [r["id"] for r in out] == [a_pkg.id]
    assert b_pkg.id not in {r["id"] for r in out}


# ── Auth ─────────────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_lineage_403_when_caller_not_client_user(db):
    """Caller without an active ClientUser at this client → 403
    `client_user_required`."""
    # skip_auto_link so the user is a member of NO client (the test's
    # whole point is the missing ClientUser → 403).
    client = await make_client(db)
    rando = await make_user(db, name="Rando", skip_auto_link=True)
    now = datetime.now(timezone.utc)
    pkg = await _local_pkg(db, client=client, name="P",
                           status=PackageStatus.ACTIVE, version=1,
                           published_at=now)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await get_package_lineage(
            client_id=client.id, package_id=pkg.id,
            db=db, current_user=rando,
        )
    assert exc.value.status_code == 403
    # Batch K (2026-05-18): the view-guard fires first now, with
    # `advisory_view_forbidden`. Previously the test asserted the
    # downstream `client_user_required` code from
    # `_assert_client_user_required`. The new guard is a superset
    # (refuses non-members AND non-SE/non-CA members) — refusing
    # earlier is by design.
    assert exc.value.detail["code"] == "advisory_view_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_lineage_404_when_package_missing(db):
    """Missing package id → 404 from `_get_package`."""
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_client_user(db, user=se, client=client)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await get_package_lineage(
            client_id=client.id, package_id=f"ghost-{uuid.uuid4().hex[:8]}",
            db=db, current_user=se,
        )
    assert exc.value.status_code == 404
