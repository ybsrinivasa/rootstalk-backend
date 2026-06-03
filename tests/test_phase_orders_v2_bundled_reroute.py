"""Orders V2 Batch 10 — bundled re-route of Returned items.

The 2026-05-31 narrative (FU-9): three items returned by the
dealer should give the farmer ONE re-route action, not three. The
farmer doesn't see item names — that detail belongs to the dealer.

Mechanically the bundle is identical to cancel-migrate but moves
only the Returned-flavoured items (NOT_AVAILABLE, REJECTED,
POSTPONED). The source order keeps the fulfilled items and its
status recomputes through `_update_order_status`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemEvent, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import reroute_returned_items
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


async def _order_with_mixed_item_statuses(db):
    """Five items: 2 NOT_AVAILABLE, 1 POSTPONED, 1 REJECTED, 1 AVAILABLE.
    Bundle should pull the 4 non-fulfilled ones and leave the
    AVAILABLE one on the source order.
    """
    user = await make_user(db, name="Farmer Bundle")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_b",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    practices = []
    for _ in range(5):
        p = await make_practice(
            db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES",
        )
        practices.append(p)
    await db.commit()

    dealer = await make_onboarded_dealer(db, name="D-Bundle")
    await db.commit()

    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.PROCESSING,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    items = []
    statuses = [
        OrderItemStatus.NOT_AVAILABLE,
        OrderItemStatus.NOT_AVAILABLE,
        OrderItemStatus.POSTPONED,
        OrderItemStatus.REJECTED,
        OrderItemStatus.AVAILABLE,
    ]
    for p, st in zip(practices, statuses):
        it = OrderItem(
            order_id=order.id, practice_id=p.id, timeline_id=tl.id, status=st,
        )
        db.add(it)
        items.append(it)
    await db.commit()
    for it in items:
        await db.refresh(it)
    return user, dealer, order, items


@requires_docker
@pytest.mark.asyncio
async def test_bundled_reroute_pulls_returned_into_one_draft(db):
    user, _, order, items = await _order_with_mixed_item_statuses(db)
    not_avail_a, not_avail_b, postponed, rejected, available = items

    # 2026-06-03 — include_postponed=True needed now that POSTPONED
    # is excluded by default (farmer's nudge-modal choice).
    res = await reroute_returned_items(
        order_id=order.id, data={"include_postponed": True},
        db=db, current_user=user,
    )
    assert res["rerouted_count"] == 4  # everything except AVAILABLE

    # Source items: the four returned now REROUTED, AVAILABLE stays.
    for it in (not_avail_a, not_avail_b, postponed, rejected):
        await db.refresh(it)
        assert it.status == OrderItemStatus.REROUTED
    await db.refresh(available)
    assert available.status == OrderItemStatus.AVAILABLE

    # New DRAFT exists with four PENDING items, no recipient.
    draft = (await db.execute(
        select(Order).where(Order.id == res["new_draft_order_id"])
    )).scalar_one()
    assert draft.status == OrderStatus.DRAFT
    assert draft.dealer_user_id is None
    assert draft.category == "PESTICIDE"

    draft_items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == draft.id)
    )).scalars().all()
    assert len(draft_items) == 4
    for di in draft_items:
        assert di.status == OrderItemStatus.PENDING
        # Brand/price reset; next dealer fills these afresh.
        assert di.brand_cosh_id is None
        assert di.price is None


@requires_docker
@pytest.mark.asyncio
async def test_bundled_reroute_preserves_lineage(db):
    user, _, order, items = await _order_with_mixed_item_statuses(db)
    lineages = {it.id: it.lineage_id for it in items[:4]}  # the returned ones

    res = await reroute_returned_items(
        order_id=order.id, data={"include_postponed": True},
        db=db, current_user=user,
    )

    draft_items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == res["new_draft_order_id"])
    )).scalars().all()
    draft_lineages = {di.lineage_id for di in draft_items}
    assert draft_lineages == set(lineages.values())


@requires_docker
@pytest.mark.asyncio
async def test_bundled_reroute_emits_paired_events(db):
    user, _, order, items = await _order_with_mixed_item_statuses(db)
    sample_lineage = items[0].lineage_id  # one of the NOT_AVAILABLE

    await reroute_returned_items(
        order_id=order.id, db=db, current_user=user,
    )

    events = (await db.execute(
        select(OrderItemEvent).where(OrderItemEvent.lineage_id == sample_lineage)
    )).scalars().all()
    types = sorted(e.event_type for e in events)
    assert types == ["REROUTED_FROM", "REROUTED_TO"]
    for e in events:
        assert e.actor_role == "FARMER"
        assert e.event_metadata is not None
        assert e.event_metadata.get("reason") == "bundled_reroute"


@requires_docker
@pytest.mark.asyncio
async def test_bundled_reroute_refuses_when_nothing_to_reroute(db):
    user = await make_user(db, name="Farmer Clean")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    await db.commit()
    tl = await make_timeline(
        db, pkg, name="TL_c",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()
    dealer = await make_onboarded_dealer(db, name="D-Clean")
    await db.commit()

    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.PROCESSING,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=p.id, timeline_id=tl.id,
        status=OrderItemStatus.AVAILABLE,  # nothing to reroute
    ))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await reroute_returned_items(
            order_id=order.id, db=db, current_user=user,
        )
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "nothing_to_reroute"
