"""Orders V2 Batch 17 — DBS V1 carve-out.

Locks the 2026-05-31 design:
- DBS bulk-order create endpoint takes only (sub_id, category,
  recipient). Server resolves practices, synthesises dates, drops
  already-ordered practices.
- Annual-only. Perennial packages refuse with `dbs_not_supported_
  for_perennial`.
- DBS window closes when `crop_start_date <= today`.
- When the farmer advances start_date to today/past, live DBS
  items synchronously archive + emit `TIMELINE_EXPIRED` events.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageType, PracticeL0, TimelineFromType,
)
from app.modules.orders.models import (
    DealerProfile, Order, OrderItem, OrderItemEvent, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import (
    DBSBulkCreate, create_dbs_bulk_order,
)
from app.modules.subscriptions.router import set_start_date
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


async def _annual_pkg_with_dbs(db, *, future_start: bool = True):
    user = await make_user(db, name="Farmer DBS")
    client = await make_client(db)
    pkg = await make_package(db, client)
    # Default factory may not set ANNUAL — force it.
    pkg.package_type = PackageType.ANNUAL
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    if future_start:
        sub.crop_start_date = datetime.now(timezone.utc) + timedelta(days=10)
        sub.crop_start_date_first_set_at = datetime.now(timezone.utc)
    else:
        sub.crop_start_date = None
    await db.commit()

    # Two DBS-pesticide practices on different DBS timelines.
    tl1 = await make_timeline(
        db, pkg, name="TL_DBS_1",
        from_type=TimelineFromType.DBS, from_value=15, to_value=8,
    )
    tl2 = await make_timeline(
        db, pkg, name="TL_DBS_2",
        from_type=TimelineFromType.DBS, from_value=7, to_value=2,
    )
    p1 = await make_practice(db, tl1, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    p2 = await make_practice(db, tl2, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()
    return user, client, pkg, sub, tl1, tl2, p1, p2


# ── Annual-only gate ─────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_dbs_refused_for_perennial_package(db):
    user, client, pkg, sub, *_ = await _annual_pkg_with_dbs(db)
    pkg.package_type = PackageType.PERENNIAL
    await db.commit()

    dealer = await make_onboarded_dealer(db, client=client, name="D-DBS")
    db.add(DealerProfile(
        user_id=dealer.id, shop_name="S", sell_categories=["PESTICIDES"],
        shop_gps_lat=12.0, shop_gps_lng=77.0,
    ))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_dbs_bulk_order(
            request=DBSBulkCreate(
                subscription_id=sub.id, client_id=client.id,
                category="PESTICIDE", dealer_user_id=dealer.id,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "dbs_not_supported_for_perennial"


# ── DBS window closed once start_date <= today ───────────────────


@requires_docker
@pytest.mark.asyncio
async def test_dbs_refused_when_start_today_or_past(db):
    user, client, pkg, sub, *_ = await _annual_pkg_with_dbs(db)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=1)
    await db.commit()

    dealer = await make_onboarded_dealer(db, client=client, name="D-DBS2")
    db.add(DealerProfile(
        user_id=dealer.id, shop_name="S", sell_categories=["PESTICIDES"],
        shop_gps_lat=12.0, shop_gps_lng=77.0,
    ))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_dbs_bulk_order(
            request=DBSBulkCreate(
                subscription_id=sub.id, client_id=client.id,
                category="PESTICIDE", dealer_user_id=dealer.id,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "dbs_window_closed"


# ── DBS purchase allowed pre-start-date (the V1 carve-out) ────────


@requires_docker
@pytest.mark.asyncio
async def test_dbs_bulk_creates_order_pre_start_date(db):
    user, client, pkg, sub, _, _, p1, p2 = await _annual_pkg_with_dbs(db, future_start=False)
    dealer = await make_onboarded_dealer(db, client=client, name="D-DBS3")
    db.add(DealerProfile(
        user_id=dealer.id, shop_name="S", sell_categories=["PESTICIDES"],
        shop_gps_lat=12.0, shop_gps_lng=77.0,
    ))
    await db.commit()

    res = await create_dbs_bulk_order(
        request=DBSBulkCreate(
            subscription_id=sub.id, client_id=client.id,
            category="PESTICIDE", dealer_user_id=dealer.id,
            farm_area_acres=1.0,
        ),
        db=db, current_user=user,
    )
    assert res["item_count"] == 2
    assert res["category"] == "PESTICIDE"
    assert res["is_dbs_bulk"] is True

    order = (await db.execute(select(Order).where(Order.id == res["id"]))).scalar_one()
    assert order.status == OrderStatus.SENT
    assert order.category == "PESTICIDE"
    assert order.dealer_user_id == dealer.id

    items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order.id)
    )).scalars().all()
    assert {it.practice_id for it in items} == {p1.id, p2.id}


@requires_docker
@pytest.mark.asyncio
async def test_dbs_bulk_excludes_already_ordered_practices(db):
    user, client, pkg, sub, _, _, p1, p2 = await _annual_pkg_with_dbs(db)
    dealer = await make_onboarded_dealer(db, client=client, name="D-DBS4")
    db.add(DealerProfile(
        user_id=dealer.id, shop_name="S", sell_categories=["PESTICIDES"],
        shop_gps_lat=12.0, shop_gps_lng=77.0,
    ))
    await db.commit()

    # First call: takes both p1 and p2.
    first = await create_dbs_bulk_order(
        request=DBSBulkCreate(
            subscription_id=sub.id, client_id=client.id,
            category="PESTICIDE", dealer_user_id=dealer.id,
            farm_area_acres=1.0,
        ),
        db=db, current_user=user,
    )
    assert first["item_count"] == 2

    # Second call: nothing left — both practices are in the live order.
    with pytest.raises(HTTPException) as exc:
        await create_dbs_bulk_order(
            request=DBSBulkCreate(
                subscription_id=sub.id, client_id=client.id,
                category="PESTICIDE", dealer_user_id=dealer.id,
                farm_area_acres=1.0,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "nothing_to_order"


# ── Sync-close on start-date advance ─────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_start_date_advance_archives_dbs_items(db):
    """Farmer advances crop_start_date to today → live DBS items
    on this sub's orders archive synchronously + emit
    TIMELINE_EXPIRED."""
    user, client, _, sub, _, _, p1, _ = await _annual_pkg_with_dbs(db)
    dealer = await make_onboarded_dealer(db, client=client, name="D-DBS5")
    db.add(DealerProfile(
        user_id=dealer.id, shop_name="S", sell_categories=["PESTICIDES"],
        shop_gps_lat=12.0, shop_gps_lng=77.0,
    ))
    await db.commit()

    first = await create_dbs_bulk_order(
        request=DBSBulkCreate(
            subscription_id=sub.id, client_id=client.id,
            category="PESTICIDE", dealer_user_id=dealer.id,
            farm_area_acres=1.0,
        ),
        db=db, current_user=user,
    )

    # Sanity: items live + non-archived.
    items_pre = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == first["id"])
    )).scalars().all()
    assert all(it.archived_at is None for it in items_pre)

    # Advance start_date to today.
    new_start = datetime.now(timezone.utc)
    await set_start_date(
        subscription_id=sub.id,
        data={"crop_start_date": new_start.isoformat()},
        db=db, current_user=user,
    )

    items_post = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == first["id"])
    )).scalars().all()
    # PENDING items must now be archived.
    assert all(it.archived_at is not None for it in items_post)

    events = (await db.execute(
        select(OrderItemEvent).where(
            OrderItemEvent.order_id == first["id"],
            OrderItemEvent.event_type == "TIMELINE_EXPIRED",
        )
    )).scalars().all()
    assert len(events) == len(items_post)
    for ev in events:
        assert ev.actor_role == "SYSTEM"
        assert ev.event_metadata.get("reason") == "dbs_start_date_advanced"


@requires_docker
@pytest.mark.asyncio
async def test_start_date_advance_preserves_purchased_items(db):
    """An item that already reached APPROVED before start_date
    advance keeps its status — the purchase already happened."""
    user, client, _, sub, _, _, p1, p2 = await _annual_pkg_with_dbs(db)
    dealer = await make_onboarded_dealer(db, client=client, name="D-DBS6")
    db.add(DealerProfile(
        user_id=dealer.id, shop_name="S", sell_categories=["PESTICIDES"],
        shop_gps_lat=12.0, shop_gps_lng=77.0,
    ))
    await db.commit()

    first = await create_dbs_bulk_order(
        request=DBSBulkCreate(
            subscription_id=sub.id, client_id=client.id,
            category="PESTICIDE", dealer_user_id=dealer.id,
            farm_area_acres=1.0,
        ),
        db=db, current_user=user,
    )

    # Flip one item to APPROVED directly.
    items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == first["id"])
    )).scalars().all()
    items[0].status = OrderItemStatus.APPROVED
    await db.commit()

    new_start = datetime.now(timezone.utc)
    await set_start_date(
        subscription_id=sub.id,
        data={"crop_start_date": new_start.isoformat()},
        db=db, current_user=user,
    )

    await db.refresh(items[0])
    await db.refresh(items[1])
    # APPROVED item survives — purchase already happened.
    assert items[0].status == OrderItemStatus.APPROVED
    assert items[0].archived_at is None
    # PENDING item closes.
    assert items[1].archived_at is not None
