"""Orders V2 Batch 8 — tandem archive sweep.

Locks the 2026-05-31 rule: items past their timeline window vanish
from the active order surfaces in tandem with the advisory side.

The archive is soft (`archived_at` stamp), not a hard delete —
the farmer's History view and `order_item_events` lineage trail
both survive.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemEvent, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import get_farmer_order_detail
from app.tasks.timeline_archive import _archive_expired_timeline_items_with_session
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


async def _make_order_with_expired_timeline_item(db):
    """Helper — an order whose timeline window is already in the past."""
    user = await make_user(db, name="Farmer Arc")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    # Crop sown 100 days ago. A DAS timeline that ends day 30 is now
    # 70 days expired.
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=100)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_old",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()

    dealer = await make_onboarded_dealer(db, name="D-Arc")
    await db.commit()

    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc) - timedelta(days=100),
        date_to=datetime.now(timezone.utc) - timedelta(days=70),
        status=OrderStatus.PROCESSING,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=p.id, timeline_id=tl.id,
        status=OrderItemStatus.POSTPONED,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return user, order, item


async def _make_order_with_active_timeline_item(db):
    user = await make_user(db, name="Farmer Live")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    await db.commit()
    tl = await make_timeline(
        db, pkg, name="TL_live",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()
    dealer = await make_onboarded_dealer(db, name="D-Live")
    await db.commit()
    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=30),
        status=OrderStatus.PROCESSING,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=p.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return user, order, item


@requires_docker
@pytest.mark.asyncio
async def test_sweep_archives_expired_item(db):
    _, _, item = await _make_order_with_expired_timeline_item(db)

    archived = await _archive_expired_timeline_items_with_session(db)
    assert archived == 1

    await db.refresh(item)
    assert item.archived_at is not None
    # Item's status doesn't change — only the archive flag does.
    assert item.status == OrderItemStatus.POSTPONED


@requires_docker
@pytest.mark.asyncio
async def test_sweep_emits_timeline_expired_event(db):
    _, _, item = await _make_order_with_expired_timeline_item(db)

    await _archive_expired_timeline_items_with_session(db)

    ev = (await db.execute(
        select(OrderItemEvent).where(
            OrderItemEvent.lineage_id == item.lineage_id,
            OrderItemEvent.event_type == "TIMELINE_EXPIRED",
        )
    )).scalar_one()
    assert ev.actor_role == "SYSTEM"
    assert ev.event_metadata is not None
    assert "timeline_end" in ev.event_metadata


@requires_docker
@pytest.mark.asyncio
async def test_sweep_skips_in_window_item(db):
    _, _, item = await _make_order_with_active_timeline_item(db)

    archived = await _archive_expired_timeline_items_with_session(db)
    assert archived == 0

    await db.refresh(item)
    assert item.archived_at is None


@requires_docker
@pytest.mark.asyncio
async def test_archived_items_hidden_from_farmer_detail(db):
    user, order, item = await _make_order_with_expired_timeline_item(db)

    # Pre-archive: detail returns the item.
    pre = await get_farmer_order_detail(order_id=order.id, db=db, current_user=user)
    assert len(pre["items"]) == 1

    # Run the sweep, then re-fetch.
    await _archive_expired_timeline_items_with_session(db)
    post = await get_farmer_order_detail(order_id=order.id, db=db, current_user=user)
    assert post["items"] == []


@requires_docker
@pytest.mark.asyncio
async def test_sweep_idempotent(db):
    _, _, _ = await _make_order_with_expired_timeline_item(db)

    first = await _archive_expired_timeline_items_with_session(db)
    second = await _archive_expired_timeline_items_with_session(db)
    assert first == 1
    assert second == 0  # already archived; no new work
