"""Phase 3.3 — dealer endpoints read from snapshot when item.snapshot_id set.

Verifies the *payoff* of Phases 3.0-3.2: once an order is placed, SE
edits to the master Practice's elements (e.g. removing a brand lock,
or changing a dosage) cannot reach the dealer's view of THIS order.

Targets:
  - GET /dealer/orders/{order_id}/items/{item_id}/brand-options
    (calls get_brand_options with snapshot)
  - GET /dealer/orders/{order_id} (has_locked_brand_item path)
  - Backwards compat: NULL snapshot_id falls back to master
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.modules.advisory.models import Element, PracticeL0, RelationType, TimelineFromType
from app.modules.orders.models import Order, OrderItem, OrderItemStatus, OrderStatus
from app.modules.orders.router import (
    OrderCreate, create_order, get_dealer_order, get_item_brand_options,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_practice, make_relation,
    make_subscription, make_timeline, make_user,
)


async def _seed_brand_locked_order(db):
    """Build an order whose practice has a brand-lock element, with a real
    dealer user so the dealer-side endpoints can be called.
    """
    farmer = await make_user(db, name="Farmer")
    dealer = await make_user(db, name="Dealer")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=package,
    )
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    tl = await make_timeline(
        db, package, name="DEALER_TL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="MANCOZEB",
    )
    # Brand-lock element: element_type='brand' + cosh_ref set.
    await make_element(
        db, p, element_type="brand", value=None,
        unit_cosh_id=None, cosh_ref="brand:dithane-m45",
    )

    payload = OrderCreate(
        subscription_id=sub.id,
        client_id=client.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        practice_ids=[p.id],
        dealer_user_id=dealer.id,
        farm_area_acres=1.0,
        area_unit="acres",
    )
    result = await create_order(request=payload, db=db, current_user=farmer)
    return farmer, dealer, sub, p, result["id"]


# ── BL-07 brand-options endpoint ────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_brand_options_uses_snapshot_after_master_edit(db):
    """SE removes the brand-lock element from master AFTER order placement.
    Dealer's brand-options must still show LOCKED with the original brand."""
    _farmer, dealer, _sub, p, order_id = await _seed_brand_locked_order(db)

    # Find the order item
    item = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order_id)
    )).scalar_one()
    assert item.snapshot_id is not None

    # Sanity: before edit, dealer sees LOCKED.
    pre = await get_item_brand_options(
        order_id=order_id, item_id=item.id, db=db, current_user=dealer,
    )
    assert pre["type"] == "LOCKED"
    assert pre["locked_brand_cosh_id"] == "brand:dithane-m45"

    # SE removes the brand-lock element from master.
    await db.execute(delete(Element).where(Element.practice_id == p.id))
    await db.commit()

    # Dealer's view must still show LOCKED — the snapshot froze the brand.
    post = await get_item_brand_options(
        order_id=order_id, item_id=item.id, db=db, current_user=dealer,
    )
    assert post["type"] == "LOCKED", (
        "snapshot must override master; SE removing the brand element after "
        "order placement should not unlock the dealer's view (Rule 5)"
    )
    assert post["locked_brand_cosh_id"] == "brand:dithane-m45"


@requires_docker
@pytest.mark.asyncio
async def test_brand_options_falls_back_to_master_when_snapshot_id_null(db):
    """Pre-Phase-3 order item (snapshot_id NULL) falls back to master."""
    farmer = await make_user(db, name="Farmer")
    dealer = await make_user(db, name="Dealer")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=package,
    )
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    tl = await make_timeline(
        db, package, name="LEGACY_TL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE")
    await make_element(
        db, p, element_type="brand", value=None,
        unit_cosh_id=None, cosh_ref="brand:legacy-locked",
    )

    # Hand-build a pre-Phase-3 order item (no snapshot_id).
    order = Order(
        subscription_id=sub.id,
        farmer_user_id=farmer.id,
        client_id=client.id,
        dealer_user_id=dealer.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        status=OrderStatus.SENT,
    )
    db.add(order)
    await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=p.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING, snapshot_id=None,
    )
    db.add(item)
    await db.commit()

    out = await get_item_brand_options(
        order_id=order.id, item_id=item.id, db=db, current_user=dealer,
    )
    assert out["type"] == "LOCKED"
    assert out["locked_brand_cosh_id"] == "brand:legacy-locked"


# ── Dealer order detail (has_locked_brand_item) ─────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_dealer_order_detail_locked_brand_uses_snapshot(db):
    """In the dealer order detail, has_locked_brand on relation Options must
    come from the snapshot, not master."""
    farmer = await make_user(db, name="Farmer")
    dealer = await make_user(db, name="Dealer")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=package,
    )
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    tl = await make_timeline(
        db, package, name="REL_TL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    relation = await make_relation(db, tl, relation_type=RelationType.OR)
    # Brand-locked practice in Part 1, Option 1, Position 1
    p_locked = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="MANCOZEB",
        relation=relation, relation_role="PART_1__OPT_1__POS_1",
    )
    await make_element(
        db, p_locked, element_type="brand", value=None,
        unit_cosh_id=None, cosh_ref="brand:dithane-m45",
    )

    payload = OrderCreate(
        subscription_id=sub.id,
        client_id=client.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        practice_ids=[p_locked.id],
        dealer_user_id=dealer.id,
        farm_area_acres=1.0,
        area_unit="acres",
    )
    result = await create_order(request=payload, db=db, current_user=farmer)
    order_id = result["id"]

    # Sanity before SE edit.
    detail_pre = await get_dealer_order(
        order_id=order_id, db=db, current_user=dealer,
    )
    rel_pre = next(r for r in detail_pre["relations"]
                   if r["relation_id"] == relation.id)
    opt_pre = rel_pre["parts"][0]["options"][0]
    assert opt_pre["has_locked_brand"] is True

    # SE removes the brand-lock from master AFTER the order.
    await db.execute(delete(Element).where(Element.practice_id == p_locked.id))
    await db.commit()

    detail_post = await get_dealer_order(
        order_id=order_id, db=db, current_user=dealer,
    )
    rel_post = next(r for r in detail_post["relations"]
                    if r["relation_id"] == relation.id)
    opt_post = rel_post["parts"][0]["options"][0]
    assert opt_post["has_locked_brand"] is True, (
        "snapshot froze the brand-lock; master removal must not unlock "
        "the dealer's view of an existing order"
    )
