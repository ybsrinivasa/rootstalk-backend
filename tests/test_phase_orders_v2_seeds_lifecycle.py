"""Orders V2 Batch 12 — seeds share the OrderItem vocabulary.

Per the 2026-05-31 narrative (Q10): "Seeds can also be
returned/postponed. Share the same vocabulary." Closes the parity
gap so a seed order can be postponed-with-days, marked Not
Available, cancelled-and-migrated to a fresh DRAFT, and re-sent.

The advisory does NOT show seeds (seeds aren't part of the advisory
walk) — that's intentional and outside this batch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.orders.models import OrderItemEvent
from app.modules.seed_mgmt.models import (
    SeedOrderFull, SeedOrderStatus, SeedVariety,
)
from app.modules.seed_mgmt.router import (
    SeedOrderSend, cancel_seed_order, delete_cancelled_seed_order,
    mark_seed_order_not_available, postpone_seed_order,
    seed_postpone_window, send_draft_seed_order,
)
from app.tasks.postpone_expiry import _sweep_expired_postpones_with_session
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_package, make_subscription,
    make_user,
)


async def _seed_variety_and_sub(db):
    """Setup helper — a farmer with a subscription and one variety
    they can order against. Returns the live SeedOrder bound to a
    dealer onboarded under the same client."""
    user = await make_user(db, name="Farmer Seeds")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    await db.commit()

    variety = SeedVariety(
        client_id=client.id, crop_cosh_id="crop:test",
        name="TestVariety",
        variety_type="OPEN_POLLINATED",
        status="ACTIVE",
    )
    db.add(variety)
    await db.flush()

    dealer = await make_onboarded_dealer(db, client=client, name="D-Seed")
    await db.commit()
    return user, client, sub, variety, dealer


async def _live_seed_order(db, user, sub, variety, dealer):
    order = SeedOrderFull(
        subscription_id=sub.id,
        farmer_user_id=user.id,
        variety_id=variety.id,
        client_id=sub.client_id,
        dealer_user_id=dealer.id,
        unit="kg",
        quantity=5,
        status=SeedOrderStatus.SENT,
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return order


# ── Dealer side ────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_seed_postpone_window_returns_max_days(db):
    user, _, sub, variety, dealer = await _seed_variety_and_sub(db)
    order = await _live_seed_order(db, user, sub, variety, dealer)

    res = await seed_postpone_window(
        order_id=order.id, db=db, current_user=dealer,
    )
    assert res["can_postpone"] is True
    assert res["max_days"] >= 7  # fixed cap is 14


@requires_docker
@pytest.mark.asyncio
async def test_seed_postpone_within_window(db):
    user, _, sub, variety, dealer = await _seed_variety_and_sub(db)
    order = await _live_seed_order(db, user, sub, variety, dealer)

    await postpone_seed_order(
        order_id=order.id, data={"days": 3},
        db=db, current_user=dealer,
    )
    await db.refresh(order)
    assert order.status == SeedOrderStatus.POSTPONED
    assert order.postponed_until is not None

    ev = (await db.execute(
        select(OrderItemEvent).where(
            OrderItemEvent.seed_order_id == order.id,
            OrderItemEvent.event_type == "MARKED_POSTPONED",
        )
    )).scalar_one()
    assert ev.actor_role == "DEALER"
    assert ev.event_metadata.get("days") == 3


@requires_docker
@pytest.mark.asyncio
async def test_seed_postpone_out_of_range_refused(db):
    user, _, sub, variety, dealer = await _seed_variety_and_sub(db)
    order = await _live_seed_order(db, user, sub, variety, dealer)

    with pytest.raises(HTTPException) as exc:
        await postpone_seed_order(
            order_id=order.id, data={"days": 50},
            db=db, current_user=dealer,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "postpone_days_out_of_range"


@requires_docker
@pytest.mark.asyncio
async def test_seed_mark_not_available(db):
    user, _, sub, variety, dealer = await _seed_variety_and_sub(db)
    order = await _live_seed_order(db, user, sub, variety, dealer)

    await mark_seed_order_not_available(
        order_id=order.id, db=db, current_user=dealer,
    )
    await db.refresh(order)
    assert order.status == SeedOrderStatus.NOT_AVAILABLE

    ev = (await db.execute(
        select(OrderItemEvent).where(
            OrderItemEvent.seed_order_id == order.id,
            OrderItemEvent.event_type == "MARKED_NOT_AVAILABLE",
        )
    )).scalar_one()
    assert ev.actor_role == "DEALER"


# ── Cancel-migrate flow ─────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_seed_cancel_migrates_to_fresh_draft(db):
    user, _, sub, variety, dealer = await _seed_variety_and_sub(db)
    order = await _live_seed_order(db, user, sub, variety, dealer)
    original_lineage = order.lineage_id

    res = await cancel_seed_order(
        order_id=order.id, db=db, current_user=user,
    )
    assert res["status"] == SeedOrderStatus.CANCELLED
    new_draft_id = res["new_draft_seed_order_id"]
    assert new_draft_id and new_draft_id != order.id

    draft = (await db.execute(
        select(SeedOrderFull).where(SeedOrderFull.id == new_draft_id)
    )).scalar_one()
    assert draft.status == SeedOrderStatus.DRAFT
    assert draft.dealer_user_id is None
    assert draft.facilitator_user_id is None
    assert draft.lineage_id == original_lineage
    assert draft.variety_id == variety.id
    assert draft.quantity == 5

    # Husk is terminal and remembers the variety; reports walk
    # lineage_id to see the full chain.
    await db.refresh(order)
    assert order.status == SeedOrderStatus.CANCELLED


@requires_docker
@pytest.mark.asyncio
async def test_seed_send_draft_to_dealer(db):
    user, client, sub, variety, dealer = await _seed_variety_and_sub(db)
    order = await _live_seed_order(db, user, sub, variety, dealer)
    cancel_result = await cancel_seed_order(
        order_id=order.id, db=db, current_user=user,
    )
    draft_id = cancel_result["new_draft_seed_order_id"]

    # New dealer for the re-send.
    new_dealer = await make_onboarded_dealer(db, client=client, name="D-Seed-2")
    await db.commit()

    res = await send_draft_seed_order(
        order_id=draft_id,
        body=SeedOrderSend(dealer_user_id=new_dealer.id),
        db=db, current_user=user,
    )
    assert res["status"] == SeedOrderStatus.SENT
    assert res["dealer_user_id"] == new_dealer.id


@requires_docker
@pytest.mark.asyncio
async def test_seed_delete_cancelled_husk(db):
    user, _, sub, variety, dealer = await _seed_variety_and_sub(db)
    order = await _live_seed_order(db, user, sub, variety, dealer)
    await cancel_seed_order(order_id=order.id, db=db, current_user=user)

    await delete_cancelled_seed_order(
        order_id=order.id, db=db, current_user=user,
    )
    deleted = (await db.execute(
        select(SeedOrderFull).where(SeedOrderFull.id == order.id)
    )).scalar_one_or_none()
    assert deleted is None


# ── Postpone-expiry sweep covers seeds ──────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_postpone_sweep_flips_expired_seed_to_not_available(db):
    user, _, sub, variety, dealer = await _seed_variety_and_sub(db)
    order = await _live_seed_order(db, user, sub, variety, dealer)
    order.status = SeedOrderStatus.POSTPONED
    order.postponed_until = datetime.now(timezone.utc) - timedelta(hours=1)
    await db.commit()

    flipped = await _sweep_expired_postpones_with_session(db)
    assert flipped >= 1

    await db.refresh(order)
    assert order.status == SeedOrderStatus.NOT_AVAILABLE

    ev = (await db.execute(
        select(OrderItemEvent).where(
            OrderItemEvent.seed_order_id == order.id,
            OrderItemEvent.event_type == "POSTPONE_EXPIRED",
        )
    )).scalar_one()
    assert ev.actor_role == "SYSTEM"
