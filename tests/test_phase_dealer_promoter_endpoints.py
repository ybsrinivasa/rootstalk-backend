"""Dealer parity — /promoter/crops + /promoter/packages/guided-step
accept optional client_id (2026-05-30).

When client_id is supplied, the binding-check helper
`_resolve_promoter_at_client` gates on an ACTIVE ClientPromoter at
that client (any role). When omitted, the legacy F-P-locked path
runs unchanged. This file covers:

  - Dealer with binding at Client A reaches crops at Client A.
  - Dealer without binding at Client B → 403 not_a_promoter_at_client.
  - F-P trying to use a different client_id than their locked
    binding naturally fails through the same gate.
  - Old F-P call path (no client_id) still works.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import PackageLocation
from app.modules.clients.models import ClientPromoter, ClientStatus
from app.modules.platform.models import RoleType, StatusEnum, UserRole
from app.modules.subscriptions.router import (
    promoter_crops, promoter_guided_step,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_onboarded_facilitator,
    make_package, make_user,
)


# ── Dealer happy path ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_dealer_can_read_crops_at_bound_client(db):
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    dealer = await make_onboarded_dealer(db, client=client)
    pkg = await make_package(db, client, crop_cosh_id="crop:tomato")
    pl = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == pkg.id)
    )).scalar_one()
    await db.commit()

    out = await promoter_crops(
        district_cosh_id=pl.district_cosh_id,
        client_id=client.id,
        db=db, current_user=dealer,
    )
    assert any(r["crop_cosh_id"] == "crop:tomato" for r in out)


# ── Dealer without binding ────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_dealer_403_at_client_with_no_binding(db):
    """Dealer-Promoters are multi-client; they can still only read
    crops at companies where they have an ACTIVE binding."""
    bound = await make_client(db)
    bound.status = ClientStatus.ACTIVE
    unbound = await make_client(db)
    unbound.status = ClientStatus.ACTIVE
    dealer = await make_onboarded_dealer(db, client=bound)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await promoter_crops(
            district_cosh_id="district:test",
            client_id=unbound.id,
            db=db, current_user=dealer,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "not_a_promoter_at_client"


# ── F-P trying to use a different client ──────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_fp_with_client_id_for_other_client_403(db):
    """F-P locked to client A who tries to call /promoter/crops with
    client_id=B — naturally fails the binding check at B since §11.2
    prevents them from having a second F-P binding."""
    locked = await make_client(db)
    locked.status = ClientStatus.ACTIVE
    other = await make_client(db)
    other.status = ClientStatus.ACTIVE
    fac = await make_onboarded_facilitator(db, client=locked)
    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == fac.id,
            ClientPromoter.client_id == locked.id,
        )
    )).scalar_one()
    cp.is_promoter = True
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await promoter_crops(
            district_cosh_id="district:test",
            client_id=other.id,
            db=db, current_user=fac,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "not_a_promoter_at_client"


# ── Legacy F-P path (no client_id) still works ────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_fp_path_without_client_id_still_works(db):
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    fac = await make_onboarded_facilitator(db, client=client)
    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == fac.id,
            ClientPromoter.client_id == client.id,
        )
    )).scalar_one()
    cp.is_promoter = True
    pkg = await make_package(db, client, crop_cosh_id="crop:brinjal")
    pl = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == pkg.id)
    )).scalar_one()
    await db.commit()

    out = await promoter_crops(
        district_cosh_id=pl.district_cosh_id,
        db=db, current_user=fac,
    )
    assert any(r["crop_cosh_id"] == "crop:brinjal" for r in out)


# ── guided-step Dealer path ───────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_dealer_guided_step_resolves_with_client_id(db):
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    dealer = await make_onboarded_dealer(db, client=client)
    pkg = await make_package(db, client, crop_cosh_id="crop:bhindi")
    pl = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == pkg.id)
    )).scalar_one()
    await db.commit()

    res = await promoter_guided_step(
        crop_cosh_id="crop:bhindi",
        district_cosh_id=pl.district_cosh_id,
        client_id=client.id,
        db=db, current_user=dealer,
    )
    assert isinstance(res, dict)


@requires_docker
@pytest.mark.asyncio
async def test_dealer_guided_step_403_at_unbound_client(db):
    bound = await make_client(db)
    bound.status = ClientStatus.ACTIVE
    unbound = await make_client(db)
    unbound.status = ClientStatus.ACTIVE
    dealer = await make_onboarded_dealer(db, client=bound)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await promoter_guided_step(
            crop_cosh_id="crop:x",
            district_cosh_id="district:y",
            client_id=unbound.id,
            db=db, current_user=dealer,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "not_a_promoter_at_client"
