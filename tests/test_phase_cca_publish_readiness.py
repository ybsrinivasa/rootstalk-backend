"""Read-only publish-readiness endpoint (Round 4, 2026-05-10).

`GET /client/{cid}/packages/{pid}/publish-readiness` runs every gate
that `publish_package` runs but never mutates and never raises 422 —
returns `{ready: bool, missing?: [...]}` so the CA portal can render
a live "what's missing" checklist on the Package detail page.

Pure refactor of the existing publish gates into a query endpoint;
the gate logic itself is already tested per-batch under
`tests/test_publish_validation.py` and the CCA Step 4E gate.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.modules.advisory.models import (
    PackageAuthor, PackageLocation, PackageStatus,
)
from app.modules.advisory.router import get_publish_readiness
from app.modules.clients.models import ClientCrop
from sqlalchemy import select
from tests.conftest import requires_docker
from tests.factories import make_client, make_package, make_user


# ── Happy path ─────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_readiness_returns_ready_true_for_complete_package(db):
    """make_package factory seeds 1 location, 1 author, start_date_label.
    Ergo a fresh DRAFT package from the factory is publish-ready by
    default — the readiness endpoint reflects that."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    pkg = await make_package(db, client, name="Ready PoP", crop_cosh_id="crop:test")
    await db.commit()

    out = await get_publish_readiness(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=user,
    )
    assert out["ready"] is True
    assert out["status"] == "ACTIVE"  # factory creates ACTIVE; gate-check still applies
    assert out["version"] == pkg.version


# ── Missing locations ──────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_readiness_flags_missing_locations(db):
    """Strip the auto-seeded location; readiness endpoint surfaces
    `no_locations` in the missing array."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    pkg = await make_package(db, client, name="No Loc", crop_cosh_id="crop:test")
    # Remove the factory's auto-seeded location.
    locs = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == pkg.id)
    )).scalars().all()
    for l in locs:
        await db.delete(l)
    await db.commit()

    out = await get_publish_readiness(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=user,
    )
    assert out["ready"] is False
    assert out["blocker_code"] == "publish_blocked_missing_fields"
    missing_codes = {m["code"] for m in out["missing"]}
    assert "no_locations" in missing_codes


# ── Missing authors ────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_readiness_flags_missing_authors(db):
    client = await make_client(db)
    user = await make_user(db, name="SE")
    pkg = await make_package(db, client, name="No Authors", crop_cosh_id="crop:test")
    authors = (await db.execute(
        select(PackageAuthor).where(PackageAuthor.package_id == pkg.id)
    )).scalars().all()
    for a in authors:
        await db.delete(a)
    await db.commit()

    out = await get_publish_readiness(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=user,
    )
    assert out["ready"] is False
    missing_codes = {m["code"] for m in out["missing"]}
    assert "no_authors" in missing_codes


# ── Multiple missing — all surfaced ────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_readiness_carries_complete_checklist(db):
    """Pre-spec the SE saw one error at a time; the gate now returns
    the whole list. Readiness endpoint must do the same so the CA
    portal can render every red item before the SE clicks Publish."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    pkg = await make_package(db, client, name="Bare", crop_cosh_id="crop:test")
    # Strip BOTH locations and authors.
    for L in (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == pkg.id)
    )).scalars().all():
        await db.delete(L)
    for A in (await db.execute(
        select(PackageAuthor).where(PackageAuthor.package_id == pkg.id)
    )).scalars().all():
        await db.delete(A)
    await db.commit()

    out = await get_publish_readiness(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=user,
    )
    assert out["ready"] is False
    missing_codes = {m["code"] for m in out["missing"]}
    assert {"no_locations", "no_authors"} <= missing_codes


# ── Crop-belt gate ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_readiness_flags_crop_off_belt(db):
    """If the CA soft-removes the crop while a DRAFT package sits on
    it (DRAFT packages aren't cascade-inactivated), publish must
    refuse and the readiness endpoint must say so."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    pkg = await make_package(db, client, name="Crop Off Belt", crop_cosh_id="crop:test")
    # Soft-remove the auto-seeded ClientCrop row.
    cc = (await db.execute(
        select(ClientCrop).where(
            ClientCrop.client_id == client.id,
            ClientCrop.crop_cosh_id == "crop:test",
        )
    )).scalar_one()
    cc.removed_at = datetime.now(timezone.utc)
    await db.commit()

    out = await get_publish_readiness(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=user,
    )
    assert out["ready"] is False
    assert out["blocker_code"] == "crop_not_on_belt"


# ── 404 path ───────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_readiness_404_on_unknown_package(db):
    from fastapi import HTTPException
    client = await make_client(db)
    user = await make_user(db, name="SE")
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await get_publish_readiness(
            client_id=client.id, package_id="does-not-exist",
            db=db, current_user=user,
        )
    assert exc.value.status_code == 404
