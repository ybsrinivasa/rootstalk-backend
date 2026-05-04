"""Phase B.1 — pool-balance guard on promoter assignments.

Verifies the policy: a promoter may not initiate an assignment for a
client whose pool balance is zero. The proactive can-assign endpoint
exposes the same signal up-front so the PWA can disable the company
option before the promoter walks the BL-01 flow.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.subscriptions.models import (
    Subscription, SubscriptionPool,
)
from app.modules.subscriptions.router import (
    PromoterAssignRequest,
    get_pool_can_assign,
    initiate_assignment,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_user,
)


# ── can-assign endpoint ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_can_assign_false_when_pool_empty(db):
    user = await make_user(db)
    client = await make_client(db)
    await db.commit()

    out = await get_pool_can_assign(
        client_id=client.id, db=db, current_user=user,
    )
    assert out["client_id"] == client.id
    assert out["available_units"] == 0
    assert out["can_assign"] is False


@requires_docker
@pytest.mark.asyncio
async def test_can_assign_true_when_promoter_has_allocation(db):
    """Phase C semantics — can_assign now reflects the promoter's own
    allocation balance for this client, not the company-wide pool."""
    from app.services.promoter_pool import allocate_to_promoter
    user = await make_user(db)
    client = await make_client(db)
    db.add(SubscriptionPool(client_id=client.id, units_purchased=10, units_consumed=0))
    await db.commit()
    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=user.id, units=3)
    await db.commit()

    out = await get_pool_can_assign(
        client_id=client.id, db=db, current_user=user,
    )
    assert out["available_units"] == 3
    assert out["can_assign"] is True


@requires_docker
@pytest.mark.asyncio
async def test_can_assign_false_when_promoter_balance_exhausted(db):
    """Phase C — even if the company has unallocated units, a promoter
    with 0 in their own row can't assign. Their gate is their own
    allocation, not the company-wide pool."""
    from app.services.promoter_pool import allocate_to_promoter, consume_for_assignment
    user = await make_user(db)
    client = await make_client(db)
    db.add(SubscriptionPool(client_id=client.id, units_purchased=100, units_consumed=0))
    await db.commit()
    # Allocate 1, then drain it.
    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=user.id, units=1)
    await consume_for_assignment(db, client_id=client.id, promoter_user_id=user.id)
    await db.commit()

    out = await get_pool_can_assign(
        client_id=client.id, db=db, current_user=user,
    )
    assert out["available_units"] == 0
    assert out["can_assign"] is False


# ── initiate guard ──────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_initiate_blocked_when_promoter_has_no_allocation(db):
    """Phase C — even if the company has units, a promoter with no
    allocation row (or a zero balance) is blocked. Returns 422 with
    a "you have no subscriptions allocated" message and creates no
    Subscription row."""
    promoter = await make_user(db)
    farmer = await make_user(db)
    farmer.phone = "+918888888881"
    client = await make_client(db)
    package = await make_package(db, client)
    db.add(SubscriptionPool(client_id=client.id, units_purchased=100, units_consumed=0))
    await db.commit()

    payload = PromoterAssignRequest(
        farmer_phone=farmer.phone,
        package_id=package.id,
        client_id=client.id,
        promoter_type="DEALER",
    )
    with pytest.raises(HTTPException) as exc:
        await initiate_assignment(
            request=payload, db=db, current_user=promoter,
        )
    assert exc.value.status_code == 422
    assert "no subscriptions allocated" in exc.value.detail.lower()

    rows = (await db.execute(
        select(Subscription).where(Subscription.farmer_user_id == farmer.id)
    )).scalars().all()
    assert rows == []


@requires_docker
@pytest.mark.asyncio
async def test_initiate_succeeds_when_promoter_has_allocation(db):
    """Phase C — initiate works when the promoter has a non-zero
    allocation row. The Subscription is created and the promoter's
    balance is debited by 1."""
    from app.modules.subscriptions.promoter_allocation_models import PromoterAllocation
    from app.services.promoter_pool import allocate_to_promoter

    promoter = await make_user(db)
    farmer = await make_user(db)
    farmer.phone = "+918888888882"
    client = await make_client(db)
    package = await make_package(db, client)
    db.add(SubscriptionPool(client_id=client.id, units_purchased=10, units_consumed=0))
    await db.commit()
    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=promoter.id, units=5)
    await db.commit()

    payload = PromoterAssignRequest(
        farmer_phone=farmer.phone,
        package_id=package.id,
        client_id=client.id,
        promoter_type="DEALER",
    )
    out = await initiate_assignment(
        request=payload, db=db, current_user=promoter,
    )
    assert "subscription_id" in out
    rows = (await db.execute(
        select(Subscription).where(Subscription.farmer_user_id == farmer.id)
    )).scalars().all()
    assert len(rows) == 1

    alloc = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.client_id == client.id,
            PromoterAllocation.promoter_user_id == promoter.id,
        )
    )).scalar_one()
    assert alloc.units_balance == 4  # was 5, consumed 1
    assert alloc.consumed_total == 1


@requires_docker
@pytest.mark.asyncio
async def test_initiate_404_when_farmer_unregistered(db):
    """Existing 404 path still triggers BEFORE the pool check —
    farmer-not-found is a clearer error than pool-empty when both apply."""
    promoter = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    await db.commit()

    payload = PromoterAssignRequest(
        farmer_phone="+919999999999",  # not registered
        package_id=package.id,
        client_id=client.id,
        promoter_type="DEALER",
    )
    with pytest.raises(HTTPException) as exc:
        await initiate_assignment(
            request=payload, db=db, current_user=promoter,
        )
    assert exc.value.status_code == 404
