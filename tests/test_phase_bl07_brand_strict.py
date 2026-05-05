"""BL-07 audit — strict brand validation on mark-item-available.

Spec: brand must be selected from the system list — no free-text entry.
Backend defence-in-depth: even if the PWA misbehaves, an unknown
brand_cosh_id is rejected with a stable error code.

Bonus: brand_name is canonicalised from cosh translations regardless of
what the dealer typed, so downstream analytics see consistent spellings.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import mark_item_available
from app.modules.sync.models import CoshReferenceCache
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


async def _seed_dealer_orderitem(db):
    """Sub + dealer + order + a single PENDING order item ready to mark
    AVAILABLE. Returns the order, item, and dealer."""
    farmer = await make_user(db)
    dealer = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=package,
    )
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    tl = await make_timeline(db, package, name="TL_BL07")
    p = await make_practice(db, tl, l1="PESTICIDE", l2="MANCOZEB")
    await make_element(db, p, value="2", unit_cosh_id="kg_per_acre")

    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id,
        client_id=client.id, dealer_user_id=dealer.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        status=OrderStatus.SENT,
    )
    db.add(order); await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=p.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    )
    db.add(item)
    await db.commit()
    return order, item, dealer


# ── Validation ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_mark_available_rejects_missing_brand(db):
    order, item, dealer = await _seed_dealer_orderitem(db)
    with pytest.raises(HTTPException) as exc:
        await mark_item_available(
            order_id=order.id, item_id=item.id,
            data={"given_volume": 5, "volume_unit": "kg", "price": 800},
            db=db, current_user=dealer,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "BRAND_REQUIRED"
    # Item not changed.
    refreshed = (await db.execute(
        select(OrderItem).where(OrderItem.id == item.id)
    )).scalar_one()
    assert refreshed.status == OrderItemStatus.PENDING


@requires_docker
@pytest.mark.asyncio
async def test_mark_available_rejects_blank_brand(db):
    """Empty string brand_cosh_id is treated the same as missing."""
    order, item, dealer = await _seed_dealer_orderitem(db)
    with pytest.raises(HTTPException) as exc:
        await mark_item_available(
            order_id=order.id, item_id=item.id,
            data={"brand_cosh_id": "   ", "given_volume": 5, "volume_unit": "kg"},
            db=db, current_user=dealer,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "BRAND_REQUIRED"


@requires_docker
@pytest.mark.asyncio
async def test_mark_available_rejects_unknown_brand(db):
    """brand_cosh_id present but not in cosh cache → BRAND_NOT_IN_SYSTEM."""
    order, item, dealer = await _seed_dealer_orderitem(db)
    with pytest.raises(HTTPException) as exc:
        await mark_item_available(
            order_id=order.id, item_id=item.id,
            data={
                "brand_cosh_id": "brand:typed-by-the-dealer",
                "brand_name": "Whatever",
                "given_volume": 5, "volume_unit": "kg",
            },
            db=db, current_user=dealer,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "BRAND_NOT_IN_SYSTEM"
    # Friendly message points to the missing-brand-reports endpoint.
    assert "missing-brand-reports" in exc.value.detail["message"]


@requires_docker
@pytest.mark.asyncio
async def test_mark_available_rejects_inactive_brand(db):
    """A brand that exists but is status='retired' must also be rejected."""
    order, item, dealer = await _seed_dealer_orderitem(db)
    db.add(CoshReferenceCache(
        cosh_id="brand:retired", entity_type="brand",
        translations={"en": "Old Brand"},
        status="retired",
    ))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await mark_item_available(
            order_id=order.id, item_id=item.id,
            data={"brand_cosh_id": "brand:retired"},
            db=db, current_user=dealer,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "BRAND_NOT_IN_SYSTEM"


# ── Happy path + canonicalisation ───────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_mark_available_succeeds_for_valid_brand(db):
    order, item, dealer = await _seed_dealer_orderitem(db)
    db.add(CoshReferenceCache(
        cosh_id="brand:dithane-m45", entity_type="brand",
        translations={"en": "Dithane M-45"},
        status="active",
    ))
    await db.commit()

    await mark_item_available(
        order_id=order.id, item_id=item.id,
        data={
            "brand_cosh_id": "brand:dithane-m45",
            "brand_name": "ignored — server canonicalises",
            "given_volume": 5, "volume_unit": "kg", "price": 800,
        },
        db=db, current_user=dealer,
    )

    refreshed = (await db.execute(
        select(OrderItem).where(OrderItem.id == item.id)
    )).scalar_one()
    assert refreshed.status == OrderItemStatus.AVAILABLE
    assert refreshed.brand_cosh_id == "brand:dithane-m45"
    # Canonical name from cosh, NOT the dealer's typed value.
    assert refreshed.brand_name == "Dithane M-45"
    assert float(refreshed.given_volume) == 5.0
    assert refreshed.volume_unit == "kg"
    assert float(refreshed.price) == 800


@requires_docker
@pytest.mark.asyncio
async def test_canonical_name_falls_back_to_cosh_id_when_no_translation(db):
    """If the brand has no English translation, the cosh_id is used as
    the canonical name (defensive)."""
    order, item, dealer = await _seed_dealer_orderitem(db)
    db.add(CoshReferenceCache(
        cosh_id="brand:no-translation", entity_type="brand",
        translations={},   # empty
        status="active",
    ))
    await db.commit()

    await mark_item_available(
        order_id=order.id, item_id=item.id,
        data={"brand_cosh_id": "brand:no-translation"},
        db=db, current_user=dealer,
    )
    refreshed = (await db.execute(
        select(OrderItem).where(OrderItem.id == item.id)
    )).scalar_one()
    assert refreshed.brand_name == "brand:no-translation"
