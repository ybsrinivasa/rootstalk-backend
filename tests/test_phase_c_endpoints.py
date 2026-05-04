"""Phase C.2 — backend endpoints for promoter allocations.

Covers the four new HTTP endpoints + the self-subscribe pool decoupling.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.subscriptions.models import (
    Subscription, SubscriptionPool, SubscriptionStatus, SubscriptionType,
    SubscriptionWaitlist,
)
from app.modules.subscriptions.promoter_allocation_models import PromoterAllocation
from app.modules.subscriptions.router import (
    PromoterAllocateRequest, SubscribeRequest,
    allocate_to_promoter_endpoint, create_subscription,
    list_promoter_allocations, my_promoter_allocations,
    reclaim_from_promoter_endpoint,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_package, make_user


# ── CA-side: list / allocate / reclaim ─────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_allocate_endpoint_creates_row_and_decrements_unallocated(db):
    user = await make_user(db)        # CA caller (auth only — no role check yet)
    promoter = await make_user(db)
    client = await make_client(db)
    db.add(SubscriptionPool(client_id=client.id, units_purchased=1000, units_consumed=0))
    await db.commit()

    out = await allocate_to_promoter_endpoint(
        client_id=client.id,
        promoter_user_id=promoter.id,
        request=PromoterAllocateRequest(units=50),
        db=db, current_user=user,
    )
    assert out["units_balance"] == 50
    assert out["allocated_total"] == 50

    listing = await list_promoter_allocations(
        client_id=client.id, db=db, current_user=user,
    )
    assert listing["company_unallocated_balance"] == 950
    assert len(listing["promoters"]) == 1
    p = listing["promoters"][0]
    assert p["promoter_user_id"] == promoter.id
    assert p["units_balance"] == 50


@requires_docker
@pytest.mark.asyncio
async def test_allocate_endpoint_rejects_over_company_balance(db):
    user = await make_user(db)
    promoter = await make_user(db)
    client = await make_client(db)
    db.add(SubscriptionPool(client_id=client.id, units_purchased=10, units_consumed=0))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await allocate_to_promoter_endpoint(
            client_id=client.id,
            promoter_user_id=promoter.id,
            request=PromoterAllocateRequest(units=11),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_reclaim_endpoint_returns_units_to_company(db):
    user = await make_user(db)
    promoter = await make_user(db)
    client = await make_client(db)
    db.add(SubscriptionPool(client_id=client.id, units_purchased=100, units_consumed=0))
    await db.commit()

    await allocate_to_promoter_endpoint(
        client_id=client.id, promoter_user_id=promoter.id,
        request=PromoterAllocateRequest(units=80),
        db=db, current_user=user,
    )
    out = await reclaim_from_promoter_endpoint(
        client_id=client.id, promoter_user_id=promoter.id,
        request=PromoterAllocateRequest(units=30),
        db=db, current_user=user,
    )
    assert out["units_balance"] == 50
    assert out["reclaimed_total"] == 30

    listing = await list_promoter_allocations(
        client_id=client.id, db=db, current_user=user,
    )
    # 100 purchased − 50 in promoter balance − 0 consumed = 50 unallocated
    assert listing["company_unallocated_balance"] == 50


@requires_docker
@pytest.mark.asyncio
async def test_reclaim_endpoint_rejects_over_balance(db):
    user = await make_user(db)
    promoter = await make_user(db)
    client = await make_client(db)
    db.add(SubscriptionPool(client_id=client.id, units_purchased=100, units_consumed=0))
    await db.commit()
    await allocate_to_promoter_endpoint(
        client_id=client.id, promoter_user_id=promoter.id,
        request=PromoterAllocateRequest(units=10),
        db=db, current_user=user,
    )

    with pytest.raises(HTTPException) as exc:
        await reclaim_from_promoter_endpoint(
            client_id=client.id, promoter_user_id=promoter.id,
            request=PromoterAllocateRequest(units=11),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_my_allocations_returns_each_client_for_promoter(db):
    promoter = await make_user(db)
    client_a = await make_client(db)
    client_b = await make_client(db)
    client_a.display_name = "Alpha"
    client_b.display_name = "Bravo"
    for c in (client_a, client_b):
        db.add(SubscriptionPool(client_id=c.id, units_purchased=200, units_consumed=0))
    await db.commit()

    ca_user = await make_user(db)
    await allocate_to_promoter_endpoint(
        client_id=client_a.id, promoter_user_id=promoter.id,
        request=PromoterAllocateRequest(units=15),
        db=db, current_user=ca_user,
    )
    await allocate_to_promoter_endpoint(
        client_id=client_b.id, promoter_user_id=promoter.id,
        request=PromoterAllocateRequest(units=25),
        db=db, current_user=ca_user,
    )

    out = await my_promoter_allocations(db=db, current_user=promoter)
    assert len(out) == 2
    by_name = {row["client_name"]: row for row in out}
    assert by_name["Alpha"]["units_balance"] == 15
    assert by_name["Bravo"]["units_balance"] == 25


# ── Self-subscribe is decoupled from the company pool ───────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_self_subscribe_does_not_touch_company_pool(db):
    """Phase C clarification — a farmer self-subscribe creates a
    Subscription as WAITLISTED but DOES NOT consume any unit from the
    company's subscription pool. No SubscriptionWaitlist row is added
    either (the 3-day pool-refill window is gone)."""
    farmer = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    db.add(SubscriptionPool(client_id=client.id, units_purchased=10, units_consumed=0))
    await db.commit()

    out = await create_subscription(
        request=SubscribeRequest(
            client_id=client.id,
            package_id=package.id,
            subscription_type=SubscriptionType.SELF,
        ),
        db=db, current_user=farmer,
    )
    assert out["status"] == SubscriptionStatus.WAITLISTED

    # Subscription exists with no promoter and no reference yet.
    sub = (await db.execute(
        select(Subscription).where(Subscription.farmer_user_id == farmer.id)
    )).scalar_one()
    assert sub.promoter_user_id is None
    assert sub.subscription_date is None  # not activated yet
    # Pool untouched.
    pool = (await db.execute(
        select(SubscriptionPool).where(SubscriptionPool.client_id == client.id)
    )).scalar_one()
    assert pool.units_consumed == 0
    # No waitlist 3-day row.
    wl = (await db.execute(
        select(SubscriptionWaitlist).where(SubscriptionWaitlist.subscription_id == sub.id)
    )).scalar_one_or_none()
    assert wl is None
    # No promoter allocation row written either.
    rows = (await db.execute(select(PromoterAllocation))).scalars().all()
    assert rows == []
