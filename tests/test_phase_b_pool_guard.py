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
async def test_can_assign_true_when_pool_has_units(db):
    user = await make_user(db)
    client = await make_client(db)
    pool = SubscriptionPool(client_id=client.id, units_purchased=5, units_consumed=2)
    db.add(pool)
    await db.commit()

    out = await get_pool_can_assign(
        client_id=client.id, db=db, current_user=user,
    )
    assert out["available_units"] == 3
    assert out["can_assign"] is True


@requires_docker
@pytest.mark.asyncio
async def test_can_assign_false_when_pool_fully_consumed(db):
    """Pool exists but all units already consumed → still no."""
    user = await make_user(db)
    client = await make_client(db)
    pool = SubscriptionPool(client_id=client.id, units_purchased=10, units_consumed=10)
    db.add(pool)
    await db.commit()

    out = await get_pool_can_assign(
        client_id=client.id, db=db, current_user=user,
    )
    assert out["available_units"] == 0
    assert out["can_assign"] is False


# ── initiate guard ──────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_initiate_blocked_when_pool_empty(db):
    """Pool=0 — initiate returns 422 with the company-top-up message
    and does NOT create any Subscription row."""
    promoter = await make_user(db)
    farmer = await make_user(db)
    farmer.phone = "+918888888881"  # routable through farmer-lookup
    client = await make_client(db)
    package = await make_package(db, client)
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
    assert "no available subscriptions" in exc.value.detail.lower()

    # No Subscription row was created.
    rows = (await db.execute(
        select(Subscription).where(Subscription.farmer_user_id == farmer.id)
    )).scalars().all()
    assert rows == []


@requires_docker
@pytest.mark.asyncio
async def test_initiate_succeeds_when_pool_has_units(db):
    """Pool>0 — initiate works as before. We don't assert the full
    end-state shape (that's covered by upstream tests); just that no
    422 is raised and the Subscription row exists."""
    promoter = await make_user(db)
    farmer = await make_user(db)
    farmer.phone = "+918888888882"
    client = await make_client(db)
    package = await make_package(db, client)
    pool = SubscriptionPool(client_id=client.id, units_purchased=5, units_consumed=0)
    db.add(pool)
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
