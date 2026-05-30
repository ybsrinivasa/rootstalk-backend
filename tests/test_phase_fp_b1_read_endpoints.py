"""F-P Assign-Package-to-Farmer — B1 read-side endpoints (2026-05-29).

Covers the four PWA-facing GETs that gate and populate the
Facilitator-Promoter assignment flow:

- GET /promoter/me/kitty          (entry gate)
- GET /promoter/farmers/{phone}/locations
- GET /promoter/crops
- GET /promoter/packages/guided-step (server-derived client_id)

Backed by the design lock in
memory/project_rootstalk_fp_assign_package_design.md and the
`_resolve_promoter_locked_client` invariant (one ACTIVE
FACILITATOR + is_promoter=True ClientPromoter row per F-P).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageLocation, PackageStatus,
)
from app.modules.clients.models import ClientPromoter, ClientStatus
from app.modules.subscriptions.models import Subscription  # registers mapper
from app.modules.subscriptions.promoter_allocation_models import (
    PromoterAllocation,
)
from app.modules.subscriptions.router import (
    my_kitty,
    promoter_crops,
    promoter_farmer_locations,
    promoter_guided_step,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_facilitator, make_package, make_user,
)


# ── Seed helpers ────────────────────────────────────────────────────────────

async def _promote_to_fp(db, *, user, client) -> ClientPromoter:
    """Flip the user's facilitator binding to is_promoter=True AND bump
    the client to ACTIVE so the `_resolve_promoter_locked_client`
    helper finds them. The factory leaves both at their non-active
    defaults (is_promoter=False per V1.1 Item R12; client status =
    PENDING_REVIEW per the model default)."""
    row = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == user.id,
            ClientPromoter.client_id == client.id,
            ClientPromoter.promoter_type == "FACILITATOR",
        )
    )).scalar_one()
    row.is_promoter = True
    client.status = ClientStatus.ACTIVE
    await db.flush()
    return row


async def _allocate(db, *, client_id: str, promoter_user_id: str, units: int):
    db.add(PromoterAllocation(
        client_id=client_id,
        promoter_user_id=promoter_user_id,
        units_balance=units,
        allocated_total=units,
        reclaimed_total=0,
        consumed_total=0,
    ))
    await db.flush()


# ── /promoter/me/kitty ──────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_kitty_403_when_not_a_promoter(db):
    """Plain Facilitator (is_promoter=False) is NOT a Promoter and the
    flow must refuse entry."""
    client = await make_client(db)
    fac = await make_onboarded_facilitator(db, client=client)
    # is_promoter stays False (post-V1.1 default)

    with pytest.raises(HTTPException) as ei:
        await my_kitty(db=db, current_user=fac)
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "not_a_promoter"


@requires_docker
@pytest.mark.asyncio
async def test_kitty_returns_zero_balance_for_promoter_without_allocation(db):
    client = await make_client(db)
    fac = await make_onboarded_facilitator(db, client=client)
    await _promote_to_fp(db, user=fac, client=client)

    res = await my_kitty(db=db, current_user=fac)
    assert res["client_id"] == client.id
    assert res["client_short_name"] == client.short_name
    assert res["units_balance"] == 0


@requires_docker
@pytest.mark.asyncio
async def test_kitty_returns_real_balance(db):
    client = await make_client(db)
    fac = await make_onboarded_facilitator(db, client=client)
    await _promote_to_fp(db, user=fac, client=client)
    await _allocate(db, client_id=client.id, promoter_user_id=fac.id, units=7)

    res = await my_kitty(db=db, current_user=fac)
    assert res["units_balance"] == 7


@requires_docker
@pytest.mark.asyncio
async def test_kitty_500_when_multiple_active_promoter_links(db):
    """Spec §11.2 says one Client at a time. If data drift produces
    two ACTIVE F-P rows for the same user, the helper fail-closes."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    fac = await make_onboarded_facilitator(db, client=client_a)
    await _promote_to_fp(db, user=fac, client=client_a)

    # Manually inject a second F-P link on a different client.
    client_b.status = ClientStatus.ACTIVE
    db.add(ClientPromoter(
        client_id=client_b.id, user_id=fac.id,
        promoter_type="FACILITATOR", status="ACTIVE", is_promoter=True,
    ))
    await db.flush()

    with pytest.raises(HTTPException) as ei:
        await my_kitty(db=db, current_user=fac)
    assert ei.value.status_code == 500
    assert ei.value.detail["code"] == "multiple_promoter_links"


# ── /promoter/farmers/{phone}/locations ────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_farmer_locations_404_when_unregistered(db):
    client = await make_client(db)
    fac = await make_onboarded_facilitator(db, client=client)
    await _promote_to_fp(db, user=fac, client=client)

    with pytest.raises(HTTPException) as ei:
        await promoter_farmer_locations(
            phone="+919999999999", db=db, current_user=fac,
        )
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_farmer_locations_returns_primary_location(db):
    client = await make_client(db)
    fac = await make_onboarded_facilitator(db, client=client)
    await _promote_to_fp(db, user=fac, client=client)

    farmer = await make_user(db, name="Farmer Ravi")
    farmer.phone = "+919800000001"
    farmer.state_cosh_id = "state:karnataka"
    farmer.district_cosh_id = "district:mandya"
    farmer.sub_district_cosh_id = "sub:srirangapatna"
    await db.flush()

    res = await promoter_farmer_locations(
        phone=farmer.phone, db=db, current_user=fac,
    )
    assert len(res) == 1
    assert res[0]["state_cosh_id"] == "state:karnataka"
    assert res[0]["district_cosh_id"] == "district:mandya"
    assert res[0]["sub_district_cosh_id"] == "sub:srirangapatna"


# ── /promoter/crops ────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_promoter_crops_returns_locked_client_district_intersection(db):
    client = await make_client(db)
    fac = await make_onboarded_facilitator(db, client=client)
    await _promote_to_fp(db, user=fac, client=client)

    pkg = await make_package(db, client, crop_cosh_id="crop:paddy")
    # Read back the auto-created PackageLocation's district
    pl = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == pkg.id)
    )).scalar_one()
    target_district = pl.district_cosh_id

    res = await promoter_crops(
        district_cosh_id=target_district, db=db, current_user=fac,
    )
    crop_ids = [r["crop_cosh_id"] for r in res]
    assert "crop:paddy" in crop_ids
    # 2026-05-30 — every row carries the `measure` field so the
    # Promoter PWA can auto-route to acres-vs-plants without
    # showing a picker. Value is None for crops whose Cosh
    # classification isn't seeded; that's fine — the PWA defaults
    # to AREA_WISE in that case.
    for row in res:
        assert "measure" in row


@requires_docker
@pytest.mark.asyncio
async def test_promoter_crops_empty_for_unrelated_district(db):
    client = await make_client(db)
    fac = await make_onboarded_facilitator(db, client=client)
    await _promote_to_fp(db, user=fac, client=client)
    await make_package(db, client, crop_cosh_id="crop:paddy")

    res = await promoter_crops(
        district_cosh_id="district:never-seeded", db=db, current_user=fac,
    )
    assert res == []


@requires_docker
@pytest.mark.asyncio
async def test_promoter_crops_excludes_other_clients_packages(db):
    """Packages of OTHER clients in the same district must not leak
    into this F-P's crop list — exclusivity is the point."""
    locked = await make_client(db)
    other = await make_client(db)
    fac = await make_onboarded_facilitator(db, client=locked)
    await _promote_to_fp(db, user=fac, client=locked)

    other_pkg = await make_package(db, other, crop_cosh_id="crop:brinjal")
    other_pl = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == other_pkg.id)
    )).scalar_one()

    res = await promoter_crops(
        district_cosh_id=other_pl.district_cosh_id,
        db=db, current_user=fac,
    )
    assert all(r["crop_cosh_id"] != "crop:brinjal" for r in res)


# ── /promoter/packages/guided-step ─────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_guided_step_403_when_not_a_promoter(db):
    fac = await make_onboarded_facilitator(db)
    with pytest.raises(HTTPException) as ei:
        await promoter_guided_step(
            crop_cosh_id="crop:paddy",
            district_cosh_id="district:mandya",
            db=db, current_user=fac,
        )
    assert ei.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_guided_step_delegates_to_farmer_resolver(db):
    """Promoter guided-step is a thin wrapper around the farmer-side
    BL-01 resolver with a server-derived client_id. Sanity-check the
    delegation by hitting the happy path: one Package in the pool →
    resolver returns either the package (no params) or a parameter
    question."""
    client = await make_client(db)
    fac = await make_onboarded_facilitator(db, client=client)
    await _promote_to_fp(db, user=fac, client=client)

    pkg = await make_package(db, client, crop_cosh_id="crop:paddy")
    pl = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == pkg.id)
    )).scalar_one()

    res = await promoter_guided_step(
        crop_cosh_id="crop:paddy",
        district_cosh_id=pl.district_cosh_id,
        db=db, current_user=fac,
    )
    # The resolver returns dict-shaped output regardless of branch.
    assert isinstance(res, dict)
