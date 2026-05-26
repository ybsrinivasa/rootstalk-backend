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
from app.modules.clients.models import ClientStatus, PaymentModel
from app.modules.subscriptions.router import (
    discover_crops, discover_crops_and_companies, discover_companies,
)
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
async def test_discover_hides_company_pays_clients_from_all_three_endpoints(db):
    """A COMPANY_PAYS client with a perfectly-eligible Package must
    not appear in any direct-subscription discovery surface.

    Spec: companies that chose COMPANY_PAYS during onboarding don't
    accept direct farmer subscriptions; their crops + tile must be
    invisible to a farmer browsing for an advisory. Flipping their
    payment_model back to FARMER_PAYS later (or vice versa) takes
    effect on the next discovery call — no cached state, just a
    plain WHERE clause."""
    user = await make_user(db, name="Farmer Pay")
    farmer_pays_client = await make_client(
        db, payment_model=PaymentModel.FARMER_PAYS,
    )
    farmer_pays_client.status = ClientStatus.ACTIVE
    company_pays_client = await make_client(
        db, payment_model=PaymentModel.COMPANY_PAYS,
    )
    company_pays_client.status = ClientStatus.ACTIVE

    fp_pkg = await make_package(db, farmer_pays_client, crop_cosh_id="crop:paddy")
    cp_pkg = await make_package(db, company_pays_client, crop_cosh_id="crop:chilli")

    await make_package_location(
        db, fp_pkg, state_cosh_id="state:ka", district_cosh_id="district:blr",
    )
    await make_package_location(
        db, cp_pkg, state_cosh_id="state:ka", district_cosh_id="district:blr",
    )
    await db.commit()

    # /farmer/discover/crops — only paddy, not chilli.
    crops_only = await discover_crops(
        district_cosh_id="district:blr", db=db, current_user=user,
    )
    assert {c["crop_cosh_id"] for c in crops_only} == {"crop:paddy"}

    # /farmer/discover/crops-and-companies — both crops + companies
    # collapsed to just the FARMER_PAYS one.
    cross = await discover_crops_and_companies(
        district_cosh_id="district:blr", db=db, current_user=user,
    )
    assert {c["crop_cosh_id"] for c in cross["crops"]} == {"crop:paddy"}
    assert {c["id"] for c in cross["companies"]} == {farmer_pays_client.id}

    # /farmer/discover/companies for paddy — only farmer-pays client.
    cos_paddy = await discover_companies(
        crop_cosh_id="crop:paddy", district_cosh_id="district:blr",
        db=db, current_user=user,
    )
    assert {c["id"] for c in cos_paddy} == {farmer_pays_client.id}

    # /farmer/discover/companies for chilli — empty, even though a
    # company-pays client publishes chilli in this district.
    cos_chilli = await discover_companies(
        crop_cosh_id="crop:chilli", district_cosh_id="district:blr",
        db=db, current_user=user,
    )
    assert cos_chilli == []

    # Flip the company_pays client to FARMER_PAYS — it must now
    # surface immediately on the next call. Confirms the filter is
    # a live read, not a snapshot.
    company_pays_client.payment_model = PaymentModel.FARMER_PAYS
    await db.commit()
    cos_chilli_after = await discover_companies(
        crop_cosh_id="crop:chilli", district_cosh_id="district:blr",
        db=db, current_user=user,
    )
    assert {c["id"] for c in cos_chilli_after} == {company_pays_client.id}


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
