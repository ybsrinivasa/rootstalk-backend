"""GET /farmer/discover/crops-and-companies — discovery view.

The PWA Crops & Companies page calls this once on mount and then
cross-filters locally. Three things to pin:
  - Only ACTIVE Packages whose PackageLocation covers the district
    surface
  - Cross-mapping (crop→client_ids, client→crop_cosh_ids) is correct
  - Inactive client or inactive package is excluded
"""
from __future__ import annotations

import pytest

from app.modules.advisory.models import PackageStatus
from app.modules.clients.models import ClientStatus
from app.modules.subscriptions.router import discover_crops_and_companies
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_package_location, make_user,
)


@requires_docker
@pytest.mark.asyncio
async def test_discover_returns_cross_mapping(db):
    user = await make_user(db, name="Farmer D")
    client_a = await make_client(db); client_a.status = ClientStatus.ACTIVE
    client_b = await make_client(db); client_b.status = ClientStatus.ACTIVE
    pkg_a_paddy = await make_package(db, client_a, crop_cosh_id="crop:paddy")
    pkg_b_paddy = await make_package(db, client_b, crop_cosh_id="crop:paddy")
    pkg_a_chilli = await make_package(db, client_a, crop_cosh_id="crop:chilli")

    await make_package_location(db, pkg_a_paddy,
        state_cosh_id="state:ka", district_cosh_id="district:blr")
    await make_package_location(db, pkg_b_paddy,
        state_cosh_id="state:ka", district_cosh_id="district:blr")
    await make_package_location(db, pkg_a_chilli,
        state_cosh_id="state:ka", district_cosh_id="district:blr")
    await db.commit()

    out = await discover_crops_and_companies(
        district_cosh_id="district:blr", db=db, current_user=user,
    )

    crops_by_id = {c["crop_cosh_id"]: c for c in out["crops"]}
    cos_by_id = {c["id"]: c for c in out["companies"]}

    # paddy is published by both clients; chilli only by client_a.
    assert set(crops_by_id["crop:paddy"]["client_ids"]) == {client_a.id, client_b.id}
    assert crops_by_id["crop:chilli"]["client_ids"] == [client_a.id]

    # client_a covers both crops; client_b covers paddy only.
    assert set(cos_by_id[client_a.id]["crop_cosh_ids"]) == {"crop:paddy", "crop:chilli"}
    assert cos_by_id[client_b.id]["crop_cosh_ids"] == ["crop:paddy"]


@requires_docker
@pytest.mark.asyncio
async def test_discover_excludes_inactive_packages_and_clients(db):
    user = await make_user(db, name="Farmer E")
    live_client = await make_client(db); live_client.status = ClientStatus.ACTIVE
    dead_client = await make_client(db); dead_client.status = ClientStatus.INACTIVE

    live_pkg = await make_package(db, live_client, crop_cosh_id="crop:paddy")
    draft_pkg = await make_package(db, live_client, crop_cosh_id="crop:chilli")
    draft_pkg.status = PackageStatus.DRAFT  # not active → must NOT surface
    dead_pkg = await make_package(db, dead_client, crop_cosh_id="crop:tomato")
    # client INACTIVE → dead_pkg must NOT surface (factory defaults ACTIVE)

    await make_package_location(db, live_pkg,
        state_cosh_id="state:ka", district_cosh_id="district:blr")
    await make_package_location(db, draft_pkg,
        state_cosh_id="state:ka", district_cosh_id="district:blr")
    await make_package_location(db, dead_pkg,
        state_cosh_id="state:ka", district_cosh_id="district:blr")
    await db.commit()

    out = await discover_crops_and_companies(
        district_cosh_id="district:blr", db=db, current_user=user,
    )

    crop_ids = {c["crop_cosh_id"] for c in out["crops"]}
    company_ids = {c["id"] for c in out["companies"]}
    assert crop_ids == {"crop:paddy"}
    assert company_ids == {live_client.id}


@requires_docker
@pytest.mark.asyncio
async def test_discover_excludes_packages_not_in_district(db):
    user = await make_user(db, name="Farmer F")
    client = await make_client(db); client.status = ClientStatus.ACTIVE
    pkg = await make_package(db, client, crop_cosh_id="crop:paddy")
    await make_package_location(db, pkg,
        state_cosh_id="state:ka", district_cosh_id="district:mysore")
    await db.commit()

    out = await discover_crops_and_companies(
        district_cosh_id="district:blr", db=db, current_user=user,
    )
    assert out["crops"] == []
    assert out["companies"] == []
