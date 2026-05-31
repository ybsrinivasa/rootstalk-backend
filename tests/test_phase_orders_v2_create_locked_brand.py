"""Orders V2 Batch 9 — locked-brand gate on create_order + new-order
eligible-recipients endpoint.

Closes the consistency gap where the cancel→re-send picker (Batch 5)
enforced locked-brand routing but POST /farmer/orders did not. Any
stale picker on /order/new could have shipped a brand-locked order
to the wrong recipient.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    DealerProfile, Order, OrderStatus,
)
from app.modules.orders.router import (
    OrderCreate, create_order,
    list_eligible_recipients_for_new_order,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_onboarded_facilitator,
    make_package, make_practice, make_subscription, make_timeline,
    make_user,
)


async def _farmer_pkg_practice_with_brand_lock(db, *, brand_lock: bool = True):
    user = await make_user(db, name="Farmer Create")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_create",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES",
    )
    if brand_lock:
        p.is_brand_locked = True
    await db.commit()
    return user, sub, client, p


@requires_docker
@pytest.mark.asyncio
async def test_create_order_with_brand_lock_to_onboarded_dealer_succeeds(db):
    user, sub, client, practice = await _farmer_pkg_practice_with_brand_lock(db)
    dealer = await make_onboarded_dealer(db, client=client, name="Onboarded")
    db.add(DealerProfile(
        user_id=dealer.id, shop_name="On",
        sell_categories=["PESTICIDES"],
        shop_gps_lat=12.0, shop_gps_lng=77.0,
    ))
    await db.commit()

    res = await create_order(
        request=OrderCreate(
            subscription_id=sub.id, client_id=client.id,
            date_from=datetime.now(timezone.utc),
            date_to=datetime.now(timezone.utc) + timedelta(days=10),
            practice_ids=[practice.id],
            dealer_user_id=dealer.id,
            farm_area_acres=1.0,
        ),
        db=db, current_user=user,
    )
    order_id = res["id"]

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one()
    assert order.status == OrderStatus.SENT
    assert order.category == "PESTICIDE"
    assert order.dealer_user_id == dealer.id


@requires_docker
@pytest.mark.asyncio
async def test_create_order_with_brand_lock_to_non_onboarded_dealer_refused(db):
    user, sub, client, practice = await _farmer_pkg_practice_with_brand_lock(db)
    stranger = await make_onboarded_dealer(db, name="Stranger")
    db.add(DealerProfile(
        user_id=stranger.id, shop_name="Off",
        sell_categories=["PESTICIDES"],
        shop_gps_lat=12.0, shop_gps_lng=77.0,
    ))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_order(
            request=OrderCreate(
                subscription_id=sub.id, client_id=client.id,
                date_from=datetime.now(timezone.utc),
                date_to=datetime.now(timezone.utc) + timedelta(days=10),
                practice_ids=[practice.id],
                dealer_user_id=stranger.id,
                farm_area_acres=1.0,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "locked_brand_requires_onboarded_dealer"


@requires_docker
@pytest.mark.asyncio
async def test_create_order_with_brand_lock_to_facilitator_refused(db):
    user, sub, client, practice = await _farmer_pkg_practice_with_brand_lock(db)
    fac = await make_onboarded_facilitator(db, name="FNew")
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_order(
            request=OrderCreate(
                subscription_id=sub.id, client_id=client.id,
                date_from=datetime.now(timezone.utc),
                date_to=datetime.now(timezone.utc) + timedelta(days=10),
                practice_ids=[practice.id],
                facilitator_user_id=fac.id,
                farm_area_acres=1.0,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "locked_brand_requires_onboarded_dealer"


@requires_docker
@pytest.mark.asyncio
async def test_create_order_without_brand_lock_to_facilitator_succeeds(db):
    user, sub, client, practice = await _farmer_pkg_practice_with_brand_lock(db, brand_lock=False)
    fac = await make_onboarded_facilitator(db, name="FFree")
    await db.commit()

    res = await create_order(
        request=OrderCreate(
            subscription_id=sub.id, client_id=client.id,
            date_from=datetime.now(timezone.utc),
            date_to=datetime.now(timezone.utc) + timedelta(days=10),
            practice_ids=[practice.id],
            facilitator_user_id=fac.id,
            farm_area_acres=1.0,
        ),
        db=db, current_user=user,
    )
    order = (await db.execute(select(Order).where(Order.id == res["id"]))).scalar_one()
    assert order.facilitator_user_id == fac.id
    assert order.category == "PESTICIDE"


@requires_docker
@pytest.mark.asyncio
async def test_new_order_eligible_recipients_reflects_locked_brand(db):
    user, sub, client, practice = await _farmer_pkg_practice_with_brand_lock(db)
    # Onboarded by THIS client — should appear.
    onboarded = await make_onboarded_dealer(db, client=client, name="On")
    db.add(DealerProfile(
        user_id=onboarded.id, shop_name="On",
        sell_categories=["PESTICIDES"],
        shop_gps_lat=12.0, shop_gps_lng=77.0,
    ))
    # Different client — should be hidden.
    other = await make_onboarded_dealer(db, name="Other")
    db.add(DealerProfile(
        user_id=other.id, shop_name="Other",
        sell_categories=["PESTICIDES"],
        shop_gps_lat=12.1, shop_gps_lng=77.1,
    ))
    await db.commit()

    res = await list_eligible_recipients_for_new_order(
        subscription_id=sub.id,
        category="PESTICIDE",
        practice_ids=practice.id,
        db=db, current_user=user,
    )
    dealer_ids = {d["user_id"] for d in res["dealers"]}
    assert res["has_locked_brand"] is True
    assert onboarded.id in dealer_ids
    assert other.id not in dealer_ids
    assert res["facilitators"] == []


@requires_docker
@pytest.mark.asyncio
async def test_new_order_eligible_recipients_unlocked_includes_facilitators(db):
    user, sub, _, practice = await _farmer_pkg_practice_with_brand_lock(db, brand_lock=False)
    fac = await make_onboarded_facilitator(db, name="FOK")
    await db.commit()

    res = await list_eligible_recipients_for_new_order(
        subscription_id=sub.id,
        category="PESTICIDE",
        practice_ids=practice.id,
        db=db, current_user=user,
    )
    assert res["has_locked_brand"] is False
    assert any(f["user_id"] == fac.id for f in res["facilitators"])
