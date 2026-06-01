"""Orders V2 Batch 28 — dealer draft (server side).

User spec (2026-05-31): the dealer's in-flight per-item edits must
survive a network drop, a screen change, or a different device
picking the order up. The PWA debounces edits into PUT /draft/{id};
the server keeps the map on `orders.dealer_draft`; an item moving
to AVAILABLE clears its entry; aborting wipes the whole map.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import (
    abort_order, clear_dealer_draft, get_dealer_order,
    mark_item_available, upsert_dealer_draft,
)
from app.modules.sync.models import CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


async def _seed_order(db):
    farmer = await make_user(db, name="F-draft")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.farm_area_acres = 1.0
    await db.commit()
    tl = await make_timeline(
        db, pkg, name="TL_draft",
        from_type=TimelineFromType.DAS, from_value=0, to_value=20,
    )
    p = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES",
    )
    await db.commit()
    dealer = await make_onboarded_dealer(db, name="D-draft")
    await db.commit()
    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.PROCESSING, dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=p.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    )
    db.add(item)
    await db.commit()
    return dealer, order, item


@requires_docker
@pytest.mark.asyncio
async def test_default_draft_is_empty_dict(db):
    dealer, order, _ = await _seed_order(db)
    res = await get_dealer_order(order_id=order.id, db=db, current_user=dealer)
    assert res["dealer_draft"] == {}


@requires_docker
@pytest.mark.asyncio
async def test_upsert_writes_entry(db):
    dealer, order, item = await _seed_order(db)
    payload = {
        "brand_cosh_id": "cosh:brand-x", "brand_name": "Brand X",
        "given_volume": 1.5, "volume_unit": "kg", "price": 250,
    }
    res = await upsert_dealer_draft(
        order_id=order.id, item_id=item.id, data=payload,
        db=db, current_user=dealer,
    )
    assert res["draft"][item.id] == payload

    # Whole-draft replace on second call — different shape lands clean.
    res2 = await upsert_dealer_draft(
        order_id=order.id, item_id=item.id,
        data={"given_volume": 2.0, "volume_unit": "kg"},
        db=db, current_user=dealer,
    )
    assert res2["draft"][item.id] == {"given_volume": 2.0, "volume_unit": "kg"}


@requires_docker
@pytest.mark.asyncio
async def test_empty_body_removes_entry(db):
    """PUT with nothing allowed drops the entry — handy for the client
    to clear a row without a separate DELETE roundtrip."""
    dealer, order, item = await _seed_order(db)
    await upsert_dealer_draft(
        order_id=order.id, item_id=item.id,
        data={"brand_cosh_id": "cosh:x"},
        db=db, current_user=dealer,
    )
    res = await upsert_dealer_draft(
        order_id=order.id, item_id=item.id, data={},
        db=db, current_user=dealer,
    )
    assert item.id not in res["draft"]


@requires_docker
@pytest.mark.asyncio
async def test_delete_removes_entry(db):
    dealer, order, item = await _seed_order(db)
    await upsert_dealer_draft(
        order_id=order.id, item_id=item.id,
        data={"brand_cosh_id": "cosh:x"},
        db=db, current_user=dealer,
    )
    res = await clear_dealer_draft(
        order_id=order.id, item_id=item.id,
        db=db, current_user=dealer,
    )
    assert item.id not in res["draft"]


@requires_docker
@pytest.mark.asyncio
async def test_mark_available_clears_matching_draft(db):
    dealer, order, item = await _seed_order(db)
    # Brand needs to be in cosh — mark_item_available validates it.
    db.add(CoshCoreItem(
        cosh_id="cosh:brand-mark", core_type="brand",
        parent_cosh_id=None, translations={"en": "Brand Mark"},
        status="active",
    ))
    await db.commit()
    await upsert_dealer_draft(
        order_id=order.id, item_id=item.id,
        data={"brand_cosh_id": "cosh:brand-mark", "given_volume": 1},
        db=db, current_user=dealer,
    )

    await mark_item_available(
        order_id=order.id, item_id=item.id,
        data={
            "brand_cosh_id": "cosh:brand-mark", "brand_name": "Brand Mark",
            "given_volume": 1, "volume_unit": "kg",
        },
        db=db, current_user=dealer,
    )

    res = await get_dealer_order(order_id=order.id, db=db, current_user=dealer)
    assert item.id not in res["dealer_draft"]


@requires_docker
@pytest.mark.asyncio
async def test_abort_wipes_draft(db):
    dealer, order, item = await _seed_order(db)
    await upsert_dealer_draft(
        order_id=order.id, item_id=item.id,
        data={"brand_cosh_id": "cosh:x", "given_volume": 1},
        db=db, current_user=dealer,
    )

    await abort_order(order_id=order.id, db=db, current_user=dealer)

    res = await get_dealer_order(order_id=order.id, db=db, current_user=dealer)
    assert res["dealer_draft"] == {}


@requires_docker
@pytest.mark.asyncio
async def test_abort_preserves_dealer_acceptance(db):
    """Spec correction 2026-06-01: Abort is now Reset items; it must
    NOT reverse the dealer's acceptance. Status stays as it was."""
    dealer, order, item = await _seed_order(db)
    # _seed_order leaves the order in PROCESSING.
    assert order.status == OrderStatus.PROCESSING

    await abort_order(order_id=order.id, db=db, current_user=dealer)

    res = await get_dealer_order(order_id=order.id, db=db, current_user=dealer)
    assert res["status"] == OrderStatus.PROCESSING


@requires_docker
@pytest.mark.asyncio
async def test_unknown_keys_ignored(db):
    """Server only persists whitelisted fields — a client sending
    garbage can't poison the draft store."""
    dealer, order, item = await _seed_order(db)
    res = await upsert_dealer_draft(
        order_id=order.id, item_id=item.id,
        data={
            "brand_cosh_id": "cosh:x",
            "evil": "drop tables",
            "status": "AVAILABLE",  # not allowed via this path
        },
        db=db, current_user=dealer,
    )
    assert res["draft"][item.id] == {"brand_cosh_id": "cosh:x"}
