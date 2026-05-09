"""Order-routing auth gate — V1.1 Item 5 (2026-05-09).

Per the five-ecosystem architecture, a self-claimed UserRole.DEALER
or FACILITATOR is the prerequisite to onboarding but doesn't itself
authorise order-side actions. RootsTalk treats company-onboarding
(an active ClientPromoter row of the matching type) as
authentication. Without it, the dealer / facilitator endpoints
return 403 with structured detail.

Two helpers — `_assert_active_dealer`, `_assert_active_facilitator`
— guard every action endpoint. NOT scoped to a specific client:
the user can act on orders from any company once any one company
has onboarded them.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.clients.models import ClientPromoter
from app.modules.orders.router import (
    _assert_active_dealer, _assert_active_facilitator,
    list_dealer_orders, list_facilitator_orders,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_self_registered_user, make_user,
)


# ── Direct helper checks ────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_assert_active_dealer_403_for_unonboarded_user(db):
    user = await make_self_registered_user(db, phone="+919900300001", role="DEALER")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await _assert_active_dealer(db, user.id)
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "not_an_active_dealer"


@requires_docker
@pytest.mark.asyncio
async def test_assert_active_dealer_passes_for_onboarded_user(db):
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    user = await make_self_registered_user(db, phone="+919900300002", role="DEALER")
    db.add(ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="DEALER", status="ACTIVE",
        registered_by=sa.id,
    ))
    await db.commit()

    # No exception.
    await _assert_active_dealer(db, user.id)


@requires_docker
@pytest.mark.asyncio
async def test_assert_active_dealer_403_when_only_onboarding_is_inactive(db):
    """A previously-onboarded but now-deactivated Dealer cannot
    receive orders. Reactivate first."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    user = await make_self_registered_user(db, phone="+919900300003", role="DEALER")
    db.add(ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="DEALER", status="INACTIVE",
        registered_by=sa.id,
    ))
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await _assert_active_dealer(db, user.id)
    assert ei.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_assert_active_dealer_passes_with_at_least_one_active_onboarding(db):
    """User onboarded at A (INACTIVE) and B (ACTIVE) — passes via
    B. The gate is 'at least one ACTIVE onboarding', not all."""
    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    user = await make_self_registered_user(db, phone="+919900300004", role="DEALER")
    db.add(ClientPromoter(
        client_id=client_a.id, user_id=user.id,
        promoter_type="DEALER", status="INACTIVE",
        registered_by=sa.id,
    ))
    db.add(ClientPromoter(
        client_id=client_b.id, user_id=user.id,
        promoter_type="DEALER", status="ACTIVE",
        registered_by=sa.id,
    ))
    await db.commit()

    await _assert_active_dealer(db, user.id)


@requires_docker
@pytest.mark.asyncio
async def test_assert_active_facilitator_independent_from_dealer(db):
    """Onboarded as DEALER doesn't authorise FACILITATOR-side
    endpoints, and vice versa. Each helper checks its own
    promoter_type."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    user = await make_self_registered_user(db, phone="+919900300005", role="DEALER")
    db.add(ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="DEALER", status="ACTIVE",
        registered_by=sa.id,
    ))
    await db.commit()

    # Dealer-side passes.
    await _assert_active_dealer(db, user.id)
    # Facilitator-side fails.
    with pytest.raises(HTTPException) as ei:
        await _assert_active_facilitator(db, user.id)
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "not_an_active_facilitator"


# ── Endpoint-level smoke ────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_dealer_orders_403_without_onboarding(db):
    """End-to-end: hitting /dealer/orders without an onboarding
    surfaces the 403 immediately."""
    user = await make_self_registered_user(db, phone="+919900300006", role="DEALER")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await list_dealer_orders(db=db, current_user=user)
    assert ei.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_list_facilitator_orders_403_without_onboarding(db):
    user = await make_self_registered_user(db, phone="+919900300007", role="FACILITATOR")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await list_facilitator_orders(
            db=db, current_user=user, status_filter=None,
        )
    assert ei.value.status_code == 403
