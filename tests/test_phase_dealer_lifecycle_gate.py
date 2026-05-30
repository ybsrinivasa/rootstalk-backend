"""Dealer lifecycle gate audit (V1.1 Item 5 — 2026-05-30).

Pins the user's stated rule:

  "A dealer should be onboarded by at least one company to become
   functional and should remain onboarded by at least one company
   to remain functional."

Translation: every dealer-action endpoint must call
`_assert_active_dealer`, which checks for an ACTIVE ClientPromoter
row of `promoter_type=DEALER`. Bootstrap/config endpoints
(/dealer/profile, /dealer/dealerships CRUD, /dealer/manufacturers-
catalog) stay ungated so a freshly self-claimed dealer can finish
shop setup before getting onboarded.

Also covers the new GET /dealer/me/onboarding-status endpoint that
drives the PWA empty-state.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.clients.models import ClientPromoter, ClientStatus
from app.modules.orders.router import (
    dealer_onboarding_status,
)
from app.modules.platform.models import RoleType, StatusEnum, UserRole
from app.modules.seed_mgmt.router import list_dealer_seed_orders
from app.modules.subscriptions.router import dealer_district_advisories
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_user,
)


async def _self_claimed_only_dealer(db, *, name="Lonely"):
    """Seed a User who has self-claimed DEALER but is not onboarded by
    any company. The lifecycle gate must refuse them."""
    user = await make_user(db, name=name)
    db.add(UserRole(
        user_id=user.id, role_type=RoleType.DEALER, status=StatusEnum.ACTIVE,
    ))
    await db.flush()
    return user


# ── /dealer/me/onboarding-status ──────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_onboarding_status_returns_false_for_unonboarded(db):
    user = await _self_claimed_only_dealer(db)
    await db.commit()

    out = await dealer_onboarding_status(db=db, current_user=user)
    assert out == {"onboarded": False, "client_count": 0}


@requires_docker
@pytest.mark.asyncio
async def test_onboarding_status_returns_true_and_count_for_onboarded(db):
    client_a = await make_client(db)
    client_a.status = ClientStatus.ACTIVE
    dealer = await make_onboarded_dealer(db, client=client_a)
    # Add a second onboarding at a different client.
    client_b = await make_client(db)
    client_b.status = ClientStatus.ACTIVE
    db.add(ClientPromoter(
        client_id=client_b.id, user_id=dealer.id,
        promoter_type="DEALER", status="ACTIVE",
    ))
    await db.commit()

    out = await dealer_onboarding_status(db=db, current_user=dealer)
    assert out["onboarded"] is True
    assert out["client_count"] == 2


@requires_docker
@pytest.mark.asyncio
async def test_onboarding_status_drops_to_false_when_all_revoked(db):
    """Pins the second half of the user's rule: 'should remain
    onboarded to remain functional'. An ACTIVE → INACTIVE flip on
    every binding flips the flag."""
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    dealer = await make_onboarded_dealer(db, client=client)
    await db.commit()

    pre = await dealer_onboarding_status(db=db, current_user=dealer)
    assert pre["onboarded"] is True

    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == dealer.id,
            ClientPromoter.client_id == client.id,
        )
    )).scalar_one()
    cp.status = "INACTIVE"
    await db.commit()

    post = await dealer_onboarding_status(db=db, current_user=dealer)
    assert post["onboarded"] is False
    assert post["client_count"] == 0


# ── Newly-gated action endpoints refuse a self-claimed un-onboarded user ──

@requires_docker
@pytest.mark.asyncio
async def test_district_advisories_refuses_unonboarded(db):
    user = await _self_claimed_only_dealer(db)
    user.district_cosh_id = "district:test"
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await dealer_district_advisories(db=db, current_user=user)
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "not_an_active_dealer"


@requires_docker
@pytest.mark.asyncio
async def test_seed_orders_list_refuses_unonboarded(db):
    user = await _self_claimed_only_dealer(db)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await list_dealer_seed_orders(db=db, current_user=user)
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "not_an_active_dealer"


# ── Gates pass for properly-onboarded dealers ─────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_district_advisories_passes_for_onboarded(db):
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    dealer = await make_onboarded_dealer(db, client=client)
    dealer.district_cosh_id = "district:test"
    await db.commit()

    # No advisories seeded, but the call should reach the read path
    # (returns []) instead of 403.
    out = await dealer_district_advisories(db=db, current_user=dealer)
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_seed_orders_list_passes_for_onboarded(db):
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    dealer = await make_onboarded_dealer(db, client=client)
    await db.commit()

    out = await list_dealer_seed_orders(db=db, current_user=dealer)
    assert isinstance(out, list)
