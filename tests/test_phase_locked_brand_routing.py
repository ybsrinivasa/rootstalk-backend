"""Locked-Brand priority routing — V1.1 Item 6 (2026-05-09).

Per the user's clarification 2026-05-09, the routing logic is:

  - Dealer pool always restricted to onboarded dealers (any active
    ClientPromoter of type DEALER, anywhere).
  - Order with at least one Locked Brand item → restricted to
    dealers onboarded by the ORDER'S CLIENT. Not the manufacturer.
    Tier = LOCKED_MATCH.
  - Order without locked items → client-onboarded dealers get
    first-dealer advantage (FIRST_DEALER_ADVANTAGE tier); other
    onboarded dealers get OPEN tier. Both returned, sorted by
    tier then distance.

A Locked Brand is signalled in the schema by an Element on the
Practice with element_type='brand' AND cosh_ref non-null. Existing
helpers (bl07_brand_options, has_locked_brand_item) already use
this signal — V1.1 Item 6 reuses the same check at routing time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest

from app.modules.advisory.models import Element
from app.modules.orders.models import (
    DealerProfile, Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import nearby_dealers
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_onboarded_facilitator,
    make_package, make_practice, make_subscription, make_timeline, make_user,
)


async def _seed_dealer_with_profile(
    db, *, client, name="Dealer", lat=12.97, lng=77.59, sell="PESTICIDES",
):
    """Seed an onboarded Dealer with a fleshed-out DealerProfile so
    nearby_dealers' GPS + category filters can find them."""
    user = await make_onboarded_dealer(db, client=client, name=name)
    db.add(DealerProfile(
        user_id=user.id, shop_name=f"{name} Shop",
        shop_address=f"{name} Address",
        sell_categories=[sell],
        shop_gps_lat=lat, shop_gps_lng=lng,
    ))
    await db.flush()
    return user


async def _seed_facilitator_caller(db, *, client):
    """Active Facilitator that nearby_dealers' auth gate accepts."""
    facilitator = await make_onboarded_facilitator(
        db, client=client, name=f"FM-{uuid.uuid4().hex[:4]}",
    )
    facilitator.gps_lat = 12.97
    facilitator.gps_lng = 77.59
    return facilitator


async def _seed_order_with_locked_brand(
    db, *, client, farmer, has_locked: bool,
):
    """Seed an order with one OrderItem whose Practice has (or
    doesn't have) a brand-lock element. has_locked=True attaches an
    Element with element_type='brand' + cosh_ref set."""
    package = await make_package(db, client, name=f"PoP {uuid.uuid4().hex[:6]}")
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=10)
    await db.flush()

    tl = await make_timeline(db, package, name="TL")
    practice = await make_practice(
        db, tl, l1="PESTICIDE", l2="MANCOZEB",
        is_brand_locked=has_locked,
    )
    if has_locked:
        # Brand-lock signature (Batch 39I-a): is_brand_locked=True on
        # the Practice, plus the BRAND_NAME / legacy 'brand' element
        # carrying the locked Trade Name cosh_ref.
        db.add(Element(
            practice_id=practice.id,
            element_type="brand",
            cosh_ref="brand:locked-mancozeb",
        ))
    await db.flush()

    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id,
        client_id=client.id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        status=OrderStatus.PROCESSING,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    ))
    await db.flush()
    return order


# ── No order context ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_nearby_dealers_filter_to_onboarded_only(db):
    """Without order/client context, the pool is all onboarded
    dealers, tier=OPEN, sorted by distance."""
    client = await make_client(db)
    facilitator = await _seed_facilitator_caller(db, client=client)
    # Two onboarded dealers + one un-onboarded user with a profile
    # (the latter must not appear).
    await _seed_dealer_with_profile(db, client=client, name="Near", lat=12.97, lng=77.59)
    await _seed_dealer_with_profile(db, client=client, name="Far",  lat=13.50, lng=77.59)
    unonboarded = await make_user(db, name="Not onboarded")
    db.add(DealerProfile(
        user_id=unonboarded.id, shop_name="Ghost",
        sell_categories=["PESTICIDES"], shop_gps_lat=12.97, shop_gps_lng=77.59,
    ))
    await db.commit()

    out = await nearby_dealers(
        order_type="PESTICIDE", lat=12.97, lng=77.59,
        order_id=None, client_id=None,
        db=db, current_user=facilitator,
    )
    names = {r["shop_name"] for r in out}
    assert "Near Shop" in names and "Far Shop" in names
    assert "Ghost" not in names
    assert all(r["tier"] == "OPEN" for r in out)
    # Sorted by distance.
    assert out[0]["shop_name"] == "Near Shop"


# ── client_id without locked brand: first-dealer advantage ──────────────────

@requires_docker
@pytest.mark.asyncio
async def test_client_onboarded_dealers_sorted_first(db):
    """When client_id is supplied (or derived from an unlocked
    order), client-onboarded dealers come first (FIRST_DEALER_
    ADVANTAGE tier), even if they're farther than the 'OPEN' tier."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    facilitator = await _seed_facilitator_caller(db, client=client_a)

    # A's onboarded dealer is FAR; B's onboarded dealer is NEAR.
    await _seed_dealer_with_profile(
        db, client=client_a, name="Far-A", lat=13.50, lng=77.59,
    )
    await _seed_dealer_with_profile(
        db, client=client_b, name="Near-B", lat=12.97, lng=77.59,
    )
    await db.commit()

    out = await nearby_dealers(
        order_type="PESTICIDE", lat=12.97, lng=77.59,
        order_id=None, client_id=client_a.id,
        db=db, current_user=facilitator,
    )
    # A's far dealer comes BEFORE B's near dealer because tier wins.
    assert out[0]["shop_name"] == "Far-A Shop"
    assert out[0]["tier"] == "FIRST_DEALER_ADVANTAGE"
    assert out[1]["shop_name"] == "Near-B Shop"
    assert out[1]["tier"] == "OPEN"


# ── Order context with locked brand: hard restriction ───────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_locked_brand_order_excludes_other_clients_dealers(db):
    """Order has a locked brand → only dealers onboarded by the
    order's client appear, with tier LOCKED_MATCH. Other onboarded
    dealers are excluded entirely."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    farmer = await make_user(db, name="Farmer")
    facilitator = await _seed_facilitator_caller(db, client=client_a)
    await _seed_dealer_with_profile(
        db, client=client_a, name="A-Dealer", lat=12.97, lng=77.59,
    )
    await _seed_dealer_with_profile(
        db, client=client_b, name="B-Dealer", lat=12.97, lng=77.59,
    )
    order = await _seed_order_with_locked_brand(
        db, client=client_a, farmer=farmer, has_locked=True,
    )
    await db.commit()

    out = await nearby_dealers(
        order_type="PESTICIDE", lat=12.97, lng=77.59,
        order_id=order.id, client_id=None,
        db=db, current_user=facilitator,
    )
    names = {r["shop_name"] for r in out}
    assert "A-Dealer Shop" in names
    assert "B-Dealer Shop" not in names
    assert all(r["tier"] == "LOCKED_MATCH" for r in out)


@requires_docker
@pytest.mark.asyncio
async def test_unlocked_order_uses_first_dealer_advantage_sort(db):
    """Unlocked order with order_id → derives client_id from order,
    applies first-dealer advantage. Other onboarded dealers are
    included but ranked second."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    farmer = await make_user(db, name="Farmer")
    facilitator = await _seed_facilitator_caller(db, client=client_a)
    await _seed_dealer_with_profile(
        db, client=client_a, name="A-Dealer", lat=13.50, lng=77.59,  # far
    )
    await _seed_dealer_with_profile(
        db, client=client_b, name="B-Dealer", lat=12.97, lng=77.59,  # near
    )
    order = await _seed_order_with_locked_brand(
        db, client=client_a, farmer=farmer, has_locked=False,
    )
    await db.commit()

    out = await nearby_dealers(
        order_type="PESTICIDE", lat=12.97, lng=77.59,
        order_id=order.id, client_id=None,
        db=db, current_user=facilitator,
    )
    assert out[0]["shop_name"] == "A-Dealer Shop"
    assert out[0]["tier"] == "FIRST_DEALER_ADVANTAGE"
    assert out[1]["shop_name"] == "B-Dealer Shop"
    assert out[1]["tier"] == "OPEN"


# ── Edge cases ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_empty_pool_when_no_dealers_match_locked(db):
    """Locked brand at a client with NO onboarded dealers → empty
    pool, even though other onboarded dealers exist elsewhere."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    farmer = await make_user(db, name="Farmer")
    facilitator = await _seed_facilitator_caller(db, client=client_a)
    # Only B has dealers.
    await _seed_dealer_with_profile(
        db, client=client_b, name="B-Dealer", lat=12.97, lng=77.59,
    )
    order = await _seed_order_with_locked_brand(
        db, client=client_a, farmer=farmer, has_locked=True,
    )
    await db.commit()

    out = await nearby_dealers(
        order_type="PESTICIDE", lat=12.97, lng=77.59,
        order_id=order.id, client_id=None,
        db=db, current_user=facilitator,
    )
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_inactive_dealer_excluded(db):
    """Deactivated ClientPromoter (status=INACTIVE) doesn't satisfy
    the onboarded-pool predicate even if a DealerProfile exists."""
    from app.modules.clients.models import ClientPromoter

    client = await make_client(db)
    facilitator = await _seed_facilitator_caller(db, client=client)
    user = await make_user(db, name="Was Dealer")
    db.add(ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="DEALER", status="INACTIVE",
    ))
    db.add(DealerProfile(
        user_id=user.id, shop_name="Shuttered",
        sell_categories=["PESTICIDES"],
        shop_gps_lat=12.97, shop_gps_lng=77.59,
    ))
    await db.commit()

    out = await nearby_dealers(
        order_type="PESTICIDE", lat=12.97, lng=77.59,
        db=db, current_user=facilitator,
    )
    assert out == []
