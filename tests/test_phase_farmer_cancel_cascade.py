"""Cancel cascade across the lineage.

User direction 2026-06-07: when the farmer cancels an Order, every
non-terminal sibling sub-order in the same lineage_root_id chain
silently cancels too — no popup, no FCM. All migrated items
consolidate onto ONE new DRAFT so the farmer ends up with a single
re-route target instead of as many drafts as there were siblings.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from sqlalchemy import select

from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import cancel_order
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


async def _seed_lineage_with_siblings(db, *, sibling_count: int):
    """Seed one root SENT order + N sibling SENT sub-orders (sharing
    lineage_root_id = root.id). Each carries one PENDING item.
    Returns (farmer, root_order, sibling_orders)."""
    farmer = await make_user(db, name="Khaza Farmer")
    client = await make_client(db, full_name="Padmashali Seeds")
    package = await make_package(db, client, name="Tomato Pack")
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    timeline = await make_timeline(db, package)
    practice = await make_practice(db, timeline)

    root = Order(
        subscription_id=sub.id,
        farmer_user_id=farmer.id, client_id=client.id,
        dealer_user_id=None, facilitator_user_id=None,
        status=OrderStatus.SENT,
        date_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 1, tzinfo=timezone.utc),
        expires_at=datetime(2026, 6, 14, tzinfo=timezone.utc),
        reference_number="RT-26-000777",
    )
    db.add(root)
    await db.flush()
    root.lineage_root_id = root.id
    db.add(OrderItem(
        order_id=root.id,
        practice_id=practice.id, timeline_id=timeline.id,
        status=OrderItemStatus.PENDING,
    ))

    siblings = []
    for i in range(sibling_count):
        s = Order(
            subscription_id=sub.id,
            farmer_user_id=farmer.id, client_id=client.id,
            dealer_user_id=None, facilitator_user_id=None,
            status=OrderStatus.SENT,
            date_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
            date_to=datetime(2026, 6, 1, tzinfo=timezone.utc),
            expires_at=datetime(2026, 6, 14, tzinfo=timezone.utc),
            reference_number="RT-26-000777",
            lineage_root_id=root.id,
        )
        db.add(s)
        await db.flush()
        db.add(OrderItem(
            order_id=s.id,
            practice_id=practice.id, timeline_id=timeline.id,
            status=OrderItemStatus.PENDING,
        ))
        siblings.append(s)

    await db.commit()
    return farmer, root, siblings


@requires_docker
@pytest.mark.asyncio
async def test_cancel_cascades_to_all_non_terminal_siblings(db):
    """Cancel the root → both siblings also flip to CANCELLED."""
    farmer, root, siblings = await _seed_lineage_with_siblings(db, sibling_count=2)
    result = await cancel_order(order_id=root.id, db=db, current_user=farmer)
    # Sanity: response surfaces the cascade count.
    assert result["cascaded_sibling_count"] == 2

    # Each sibling now CANCELLED.
    for s in siblings:
        await db.refresh(s)
        assert s.status == OrderStatus.CANCELLED


@requires_docker
@pytest.mark.asyncio
async def test_cancel_consolidates_all_items_onto_one_draft(db):
    """The new DRAFT contains items from the source AND every
    cancelled sibling, so the farmer has a single re-route target."""
    farmer, root, siblings = await _seed_lineage_with_siblings(db, sibling_count=2)
    result = await cancel_order(order_id=root.id, db=db, current_user=farmer)
    new_draft_id = result["new_draft_order_id"]
    # 1 root item + 2 sibling items = 3 migrated onto the new draft.
    assert result["total_migrated_item_count"] == 3
    items_on_draft = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == new_draft_id)
    )).scalars().all()
    assert len(items_on_draft) == 3
    # All migrated items reset to PENDING.
    assert all(i.status == OrderItemStatus.PENDING for i in items_on_draft)


@requires_docker
@pytest.mark.asyncio
async def test_cancel_skips_already_terminal_siblings(db):
    """A sibling already in CANCELLED / COMPLETED is left alone —
    no double-cancel."""
    farmer, root, siblings = await _seed_lineage_with_siblings(db, sibling_count=2)
    # Pre-mark one sibling as COMPLETED.
    siblings[0].status = OrderStatus.COMPLETED
    await db.commit()

    result = await cancel_order(order_id=root.id, db=db, current_user=farmer)
    # Only the live (still-SENT) sibling cascades; the COMPLETED one
    # stays untouched.
    assert result["cascaded_sibling_count"] == 1
    await db.refresh(siblings[0])
    assert siblings[0].status == OrderStatus.COMPLETED
    await db.refresh(siblings[1])
    assert siblings[1].status == OrderStatus.CANCELLED


@requires_docker
@pytest.mark.asyncio
async def test_cancel_with_no_siblings_still_works(db):
    """Single-order cancel (no siblings in the lineage) keeps its
    original cancel-and-migrate behaviour."""
    farmer, root, _ = await _seed_lineage_with_siblings(db, sibling_count=0)
    result = await cancel_order(order_id=root.id, db=db, current_user=farmer)
    assert result["cascaded_sibling_count"] == 0
    assert result["migrated_item_count"] == 1
    await db.refresh(root)
    assert root.status == OrderStatus.CANCELLED


@requires_docker
@pytest.mark.asyncio
async def test_cancel_from_a_sibling_also_cascades(db):
    """The cancel handler doesn't require the caller to tap the root.
    Cancelling any sub-order in the chain cascades to the others."""
    farmer, root, siblings = await _seed_lineage_with_siblings(db, sibling_count=2)
    # Cancel via the FIRST sibling (not the root).
    result = await cancel_order(order_id=siblings[0].id, db=db, current_user=farmer)
    assert result["cascaded_sibling_count"] == 2  # root + sibling[1]

    await db.refresh(root)
    assert root.status == OrderStatus.CANCELLED
    await db.refresh(siblings[0])
    assert siblings[0].status == OrderStatus.CANCELLED
    await db.refresh(siblings[1])
    assert siblings[1].status == OrderStatus.CANCELLED
