"""Phase C.1 — promoter_pool service.

DB-backed tests for the four service operations + the two read
accessors. Verifies invariants, validation, and the formula for
company unallocated balance under a mix of allocations and
consumptions.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.subscriptions.models import SubscriptionPool
from app.modules.subscriptions.promoter_allocation_models import PromoterAllocation
from app.services.promoter_pool import (
    allocate_to_promoter,
    consume_for_assignment,
    get_company_unallocated_balance,
    get_promoter_balance,
    reclaim_from_promoter,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


# ── Fixtures helpers ────────────────────────────────────────────────────────

async def _seed_company_with_pool(db, units_purchased: int = 1000):
    client = await make_client(db)
    db.add(SubscriptionPool(
        client_id=client.id,
        units_purchased=units_purchased,
        units_consumed=0,
    ))
    await db.commit()
    return client


# ── allocate_to_promoter ────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_allocate_creates_row_on_first_call(db):
    client = await _seed_company_with_pool(db, 1000)
    promoter = await make_user(db)
    await db.commit()

    row = await allocate_to_promoter(
        db, client_id=client.id, promoter_user_id=promoter.id, units=50,
    )
    await db.commit()

    assert row.units_balance == 50
    assert row.allocated_total == 50
    assert row.reclaimed_total == 0
    assert row.consumed_total == 0


@requires_docker
@pytest.mark.asyncio
async def test_allocate_increments_existing_row(db):
    client = await _seed_company_with_pool(db, 1000)
    promoter = await make_user(db)
    await db.commit()

    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=promoter.id, units=50)
    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=promoter.id, units=20)
    await db.commit()

    rows = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.promoter_user_id == promoter.id,
        )
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].units_balance == 70
    assert rows[0].allocated_total == 70


@requires_docker
@pytest.mark.asyncio
async def test_allocate_rejects_over_company_balance(db):
    client = await _seed_company_with_pool(db, 100)
    promoter = await make_user(db)
    await db.commit()

    with pytest.raises(ValueError, match="insufficient"):
        await allocate_to_promoter(
            db, client_id=client.id, promoter_user_id=promoter.id, units=101,
        )


@requires_docker
@pytest.mark.asyncio
async def test_allocate_rejects_non_positive(db):
    client = await _seed_company_with_pool(db, 1000)
    promoter = await make_user(db)
    await db.commit()

    with pytest.raises(ValueError, match="positive"):
        await allocate_to_promoter(
            db, client_id=client.id, promoter_user_id=promoter.id, units=0,
        )
    with pytest.raises(ValueError, match="positive"):
        await allocate_to_promoter(
            db, client_id=client.id, promoter_user_id=promoter.id, units=-5,
        )


# ── reclaim_from_promoter ───────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_reclaim_returns_units_to_company(db):
    client = await _seed_company_with_pool(db, 1000)
    promoter = await make_user(db)
    await db.commit()

    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=promoter.id, units=100)
    await reclaim_from_promoter(db, client_id=client.id, promoter_user_id=promoter.id, units=30)
    await db.commit()

    bal = await get_promoter_balance(db, client.id, promoter.id)
    assert bal == 70

    company_unalloc = await get_company_unallocated_balance(db, client.id)
    # 1000 purchased − 70 in promoter balance − 0 consumed = 930
    assert company_unalloc == 930


@requires_docker
@pytest.mark.asyncio
async def test_reclaim_cannot_exceed_balance(db):
    client = await _seed_company_with_pool(db, 1000)
    promoter = await make_user(db)
    await db.commit()

    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=promoter.id, units=10)
    with pytest.raises(ValueError, match="cannot reclaim more"):
        await reclaim_from_promoter(
            db, client_id=client.id, promoter_user_id=promoter.id, units=11,
        )


@requires_docker
@pytest.mark.asyncio
async def test_reclaim_no_row_raises(db):
    client = await _seed_company_with_pool(db, 1000)
    promoter = await make_user(db)
    await db.commit()

    with pytest.raises(ValueError, match="no allocation"):
        await reclaim_from_promoter(
            db, client_id=client.id, promoter_user_id=promoter.id, units=1,
        )


# ── consume_for_assignment ──────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_consume_decrements_balance_and_increments_consumed(db):
    client = await _seed_company_with_pool(db, 1000)
    promoter = await make_user(db)
    await db.commit()

    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=promoter.id, units=5)
    row = await consume_for_assignment(db, client_id=client.id, promoter_user_id=promoter.id)
    assert row.units_balance == 4
    assert row.consumed_total == 1
    assert row.allocated_total == 5


@requires_docker
@pytest.mark.asyncio
async def test_consume_with_zero_balance_raises(db):
    client = await _seed_company_with_pool(db, 1000)
    promoter = await make_user(db)
    await db.commit()

    # Promoter has no allocation row at all.
    with pytest.raises(ValueError, match="no allocated units"):
        await consume_for_assignment(
            db, client_id=client.id, promoter_user_id=promoter.id,
        )


@requires_docker
@pytest.mark.asyncio
async def test_consume_after_drained_raises(db):
    client = await _seed_company_with_pool(db, 1000)
    promoter = await make_user(db)
    await db.commit()
    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=promoter.id, units=1)
    await consume_for_assignment(db, client_id=client.id, promoter_user_id=promoter.id)
    with pytest.raises(ValueError, match="no allocated units"):
        await consume_for_assignment(
            db, client_id=client.id, promoter_user_id=promoter.id,
        )


# ── Company unallocated balance — formula correctness ──────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_company_unallocated_with_promoters_and_consumption(db):
    """The user's worked example — 1000 purchased, 10 promoters, 865
    distributed (with various consumption), 135 unallocated."""
    client = await _seed_company_with_pool(db, 1000)
    promoters = [await make_user(db) for _ in range(3)]
    await db.commit()

    # Allocate 300 / 400 / 165 = 865 across three promoters.
    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=promoters[0].id, units=300)
    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=promoters[1].id, units=400)
    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=promoters[2].id, units=165)
    # Promoter 0 consumed 50, promoter 1 consumed 200, promoter 2 consumed 0.
    for _ in range(50):
        await consume_for_assignment(db, client_id=client.id, promoter_user_id=promoters[0].id)
    for _ in range(200):
        await consume_for_assignment(db, client_id=client.id, promoter_user_id=promoters[1].id)
    await db.commit()

    # Per-promoter balances after consumption.
    assert await get_promoter_balance(db, client.id, promoters[0].id) == 250
    assert await get_promoter_balance(db, client.id, promoters[1].id) == 200
    assert await get_promoter_balance(db, client.id, promoters[2].id) == 165

    unalloc = await get_company_unallocated_balance(db, client.id)
    # 1000 − (250 + 200 + 165 = 615) − (50 + 200 + 0 = 250) = 135
    assert unalloc == 135


@requires_docker
@pytest.mark.asyncio
async def test_company_unallocated_zero_when_nothing_purchased(db):
    client = await make_client(db)  # no SubscriptionPool rows
    await db.commit()
    assert await get_company_unallocated_balance(db, client.id) == 0


@requires_docker
@pytest.mark.asyncio
async def test_invariant_holds_after_mixed_operations(db):
    client = await _seed_company_with_pool(db, 1000)
    promoter = await make_user(db)
    await db.commit()

    await allocate_to_promoter(db, client_id=client.id, promoter_user_id=promoter.id, units=100)
    await reclaim_from_promoter(db, client_id=client.id, promoter_user_id=promoter.id, units=10)
    for _ in range(20):
        await consume_for_assignment(db, client_id=client.id, promoter_user_id=promoter.id)
    await db.commit()

    row = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.promoter_user_id == promoter.id,
        )
    )).scalar_one()
    # Invariant: balance == allocated − reclaimed − consumed
    assert row.units_balance == row.allocated_total - row.reclaimed_total - row.consumed_total
    assert row.units_balance == 70  # 100 − 10 − 20
