"""Batch FF (2026-05-19) — footprint→package cascade integration.

Exercises `diff_footprint_and_cascade` against the testcontainer DB
end-to-end:

  • Shrink: a removed district leaves the package with other
    locations standing. PackageLocation rows hard-deleted; status
    unchanged; cascade_inactivated_reason stays NULL;
    last_cascade_at stamped.
  • Inactivate: the removed district was the only one. Status flips
    to INACTIVE with cascade_inactivated_reason='locations_cleared
    _by_cascade'; last_cascade_at stamped; package becomes
    publish-blocked until the SE adds locations.
  • Confirmation: without force=True, raises
    FootprintCascadeConfirmationRequired carrying the structured
    impact payload — nothing is mutated.
  • Force: with force=True, the cascade actually executes.
  • No-op: identical new_pairs vs current ⇒ nothing happens.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageLocation, PackageStatus, PackageType,
)
from app.modules.clients.models import ClientLocation
from app.modules.platform.models import StatusEnum
from app.services.footprint_cascade import (
    diff_footprint_and_cascade,
    FootprintCascadeConfirmationRequired,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


async def _seed_client_with_footprint(db, pairs):
    """Seed a client + ACTIVE ClientLocation rows for the given
    (state, district) pairs. Returns the client."""
    client = await make_client(db)
    for state_id, district_id in pairs:
        db.add(ClientLocation(
            client_id=client.id,
            state_cosh_id=state_id,
            district_cosh_id=district_id,
            status=StatusEnum.ACTIVE,
        ))
    await db.commit()
    return client


async def _seed_package(db, *, client, locations):
    """Seed an ACTIVE package with the given location pairs."""
    pkg = Package(
        client_id=client.id,
        crop_cosh_id="crop:tomato",
        name=f"PoP-{client.id[:6]}",
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.ACTIVE,
    )
    db.add(pkg)
    await db.flush()
    for state_id, district_id in locations:
        db.add(PackageLocation(
            package_id=pkg.id,
            state_cosh_id=state_id, district_cosh_id=district_id,
        ))
    await db.commit()
    return pkg


@requires_docker
@pytest.mark.asyncio
async def test_cascade_noop_when_nothing_removed(db):
    """new_pairs equal to current → no impact, no exception."""
    pairs = {("S1", "D1"), ("S1", "D2")}
    client = await _seed_client_with_footprint(db, pairs)
    pkg = await _seed_package(db, client=client, locations=list(pairs))

    impact = await diff_footprint_and_cascade(
        db, client_id=client.id, new_pairs=pairs, force=False,
    )
    assert not impact.any_impact
    refreshed = await db.get(Package, pkg.id)
    assert refreshed.status == PackageStatus.ACTIVE
    assert refreshed.last_cascade_at is None


@requires_docker
@pytest.mark.asyncio
async def test_cascade_confirmation_required_without_force(db):
    """A diff that affects packages raises
    FootprintCascadeConfirmationRequired and does NOT mutate."""
    pairs = {("S1", "D1"), ("S1", "D2")}
    client = await _seed_client_with_footprint(db, pairs)
    pkg = await _seed_package(db, client=client, locations=list(pairs))

    with pytest.raises(FootprintCascadeConfirmationRequired) as exc:
        await diff_footprint_and_cascade(
            db, client_id=client.id, new_pairs={("S1", "D1")},
            force=False,
        )
    impact = exc.value.impact
    assert impact.removed_pairs == [("S1", "D2")]
    assert len(impact.shrunk) == 1
    assert impact.shrunk[0].id == pkg.id
    assert impact.shrunk[0].remaining_after == 1
    assert impact.inactivated == []

    # Nothing was actually mutated.
    refreshed = await db.get(Package, pkg.id)
    assert refreshed.status == PackageStatus.ACTIVE
    assert refreshed.last_cascade_at is None
    pl_count = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == pkg.id)
    )).scalars().all()
    assert len(pl_count) == 2


@requires_docker
@pytest.mark.asyncio
async def test_cascade_shrink_with_force(db):
    """Force=true: removes the matching PackageLocation; package
    keeps ACTIVE; last_cascade_at stamped; reason stays NULL because
    status didn't flip."""
    pairs = {("S1", "D1"), ("S1", "D2")}
    client = await _seed_client_with_footprint(db, pairs)
    pkg = await _seed_package(db, client=client, locations=list(pairs))

    impact = await diff_footprint_and_cascade(
        db, client_id=client.id, new_pairs={("S1", "D1")}, force=True,
    )
    assert len(impact.shrunk) == 1
    assert impact.inactivated == []

    await db.refresh(pkg)
    assert pkg.status == PackageStatus.ACTIVE
    assert pkg.last_cascade_at is not None
    assert pkg.cascade_inactivated_at is None
    assert pkg.cascade_inactivated_reason is None
    remaining = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == pkg.id)
    )).scalars().all()
    assert len(remaining) == 1
    assert (remaining[0].state_cosh_id, remaining[0].district_cosh_id) == ("S1", "D1")


@requires_docker
@pytest.mark.asyncio
async def test_cascade_inactivate_when_last_district_removed(db):
    """Removing the only district the package referenced flips
    status to INACTIVE + stamps the locations-cascade reason."""
    pairs = {("S1", "D1")}
    client = await _seed_client_with_footprint(db, pairs)
    pkg = await _seed_package(db, client=client, locations=list(pairs))

    impact = await diff_footprint_and_cascade(
        db, client_id=client.id, new_pairs=set(), force=True,
    )
    assert impact.shrunk == []
    assert len(impact.inactivated) == 1
    assert impact.inactivated[0].id == pkg.id
    assert impact.inactivated[0].remaining_after == 0

    await db.refresh(pkg)
    assert pkg.status == PackageStatus.INACTIVE
    assert pkg.cascade_inactivated_at is not None
    assert pkg.cascade_inactivated_reason == "locations_cleared_by_cascade"
    assert pkg.last_cascade_at is not None
    remaining = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == pkg.id)
    )).scalars().all()
    assert remaining == []


@requires_docker
@pytest.mark.asyncio
async def test_cascade_does_not_touch_other_clients(db):
    """A footprint change on client A must not cascade into client
    B's packages, even if they happen to reference the same
    (state, district) pair."""
    pairs = {("S1", "D1")}
    client_a = await _seed_client_with_footprint(db, pairs)
    client_b = await _seed_client_with_footprint(db, pairs)
    pkg_b = await _seed_package(db, client=client_b, locations=list(pairs))

    impact = await diff_footprint_and_cascade(
        db, client_id=client_a.id, new_pairs=set(), force=True,
    )
    assert impact.inactivated == []

    await db.refresh(pkg_b)
    assert pkg_b.status == PackageStatus.ACTIVE
    pl = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == pkg_b.id)
    )).scalars().all()
    assert len(pl) == 1


# ── Router-level confirmation flow ────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_set_client_locations_returns_422_then_succeeds_with_force(db):
    """End-to-end: the PUT endpoint returns 422 with the impact
    payload when force is not set, and succeeds when retried with
    force=True. Mirrors the CA portal's confirm-then-retry UX."""
    from fastapi import HTTPException
    from app.modules.clients.router import set_client_locations
    from app.modules.clients.schemas import LocationCreate

    pairs = {("S1", "D1"), ("S1", "D2")}
    client = await _seed_client_with_footprint(db, pairs)
    pkg = await _seed_package(db, client=client, locations=list(pairs))
    user = await make_user(db, name="CA")
    await db.commit()

    new_pairs_in = [LocationCreate(state_cosh_id="S1", district_cosh_id="D1")]

    with pytest.raises(HTTPException) as exc:
        await set_client_locations(
            client_id=client.id, pairs=new_pairs_in, force=False,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "footprint_cascade_confirmation_required"
    impact = exc.value.detail["impact"]
    assert impact["removed_pairs"] == [{"state_cosh_id": "S1", "district_cosh_id": "D2"}]
    assert len(impact["will_shrink"]) == 1
    assert impact["will_shrink"][0]["package_id"] == pkg.id
    assert impact["will_inactivate"] == []

    # Nothing mutated yet.
    await db.refresh(pkg)
    assert pkg.last_cascade_at is None

    out = await set_client_locations(
        client_id=client.id, pairs=new_pairs_in, force=True,
        db=db, current_user=user,
    )
    assert out["saved"] == 1

    await db.refresh(pkg)
    assert pkg.last_cascade_at is not None
    remaining = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == pkg.id)
    )).scalars().all()
    assert len(remaining) == 1


@requires_docker
@pytest.mark.asyncio
async def test_remove_location_returns_422_then_succeeds_with_force(db):
    """Single-row DELETE goes through the same cascade gate."""
    from fastapi import HTTPException
    from app.modules.clients.router import remove_location

    pairs = [("S1", "D1")]
    client = await _seed_client_with_footprint(db, pairs)
    pkg = await _seed_package(db, client=client, locations=pairs)
    user = await make_user(db, name="CA")
    await db.commit()
    loc = (await db.execute(
        select(ClientLocation).where(ClientLocation.client_id == client.id)
    )).scalar_one()

    with pytest.raises(HTTPException) as exc:
        await remove_location(
            client_id=client.id, location_id=loc.id, force=False,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "footprint_cascade_confirmation_required"
    assert len(exc.value.detail["impact"]["will_inactivate"]) == 1

    await remove_location(
        client_id=client.id, location_id=loc.id, force=True,
        db=db, current_user=user,
    )
    await db.refresh(pkg)
    assert pkg.status == PackageStatus.INACTIVE
    assert pkg.cascade_inactivated_reason == "locations_cleared_by_cascade"
