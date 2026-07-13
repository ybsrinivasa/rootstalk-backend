"""Orders V2 Batch 11 — per-practice fulfilment surfaced on the
advisory walk so the farmer sees status badges + can drill down
into "what happened with this item".

Narrative (2026-05-31): "The status on the advisory changes
accordingly — if it is purchased, then all the details are shown
to the farmer; the item could be 'returned', or 'postponed'.
It would help to make the status 'tapable'."
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.subscriptions.router import get_today_advisory
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_onboarded_dealer, make_package,
    make_practice, make_subscription, make_timeline, make_user,
)


async def _sub_with_practice(db):
    user = await make_user(db, name="Farmer Fulf")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=5)
    await db.commit()
    tl = await make_timeline(
        db, pkg, name="TL_F",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p, value="2", unit_cosh_id="l_per_acre")
    await db.commit()
    return user, sub, tl, p


def _find_practice_in(out, practice_id):
    for s in out:
        for tl in s["timelines"]:
            for p in tl.get("practices", []):
                if p["id"] == practice_id:
                    return p
    return None


@requires_docker
@pytest.mark.asyncio
async def test_no_order_means_no_fulfilment(db):
    user, _, _, practice = await _sub_with_practice(db)
    out = await get_today_advisory(db=db, current_user=user)
    p = _find_practice_in(out, practice.id)
    assert p is not None
    assert p["fulfilment"] is None
    assert p["is_purchased"] is False


@requires_docker
@pytest.mark.asyncio
async def test_pending_item_shows_pending_status(db):
    user, sub, tl, practice = await _sub_with_practice(db)
    dealer = await make_onboarded_dealer(db, name="DP")
    await db.commit()
    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=sub.client_id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.SENT,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    ))
    await db.commit()

    out = await get_today_advisory(db=db, current_user=user)
    p = _find_practice_in(out, practice.id)
    assert p is not None
    assert p["fulfilment"] is not None
    f = p["fulfilment"]
    assert f["status"] == "PENDING"
    assert f["dealer_user_id"] == dealer.id
    # Brand/price hidden until APPROVED — same as the order detail rule.
    assert f["brand_name"] is None
    assert f["price"] is None


@requires_docker
@pytest.mark.asyncio
async def test_postponed_item_includes_days_remaining(db):
    user, sub, tl, practice = await _sub_with_practice(db)
    dealer = await make_onboarded_dealer(db, name="DPo")
    await db.commit()
    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=sub.client_id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.PROCESSING,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        status=OrderItemStatus.POSTPONED,
        postponed_until=datetime.now(timezone.utc) + timedelta(days=4),
    ))
    await db.commit()

    out = await get_today_advisory(db=db, current_user=user)
    f = _find_practice_in(out, practice.id)["fulfilment"]
    assert f["status"] == "POSTPONED"
    assert f["postpone_days_remaining"] is not None
    assert 0 <= f["postpone_days_remaining"] <= 5


@requires_docker
@pytest.mark.asyncio
async def test_not_available_item_shows_returned_status(db):
    user, sub, tl, practice = await _sub_with_practice(db)
    dealer = await make_onboarded_dealer(db, name="DNa")
    await db.commit()
    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=sub.client_id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.PROCESSING,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        status=OrderItemStatus.NOT_AVAILABLE,
    ))
    await db.commit()

    out = await get_today_advisory(db=db, current_user=user)
    f = _find_practice_in(out, practice.id)["fulfilment"]
    assert f["status"] == "NOT_AVAILABLE"
    assert f["order_id"] is not None


@requires_docker
@pytest.mark.asyncio
async def test_approved_reveals_brand_and_price(db):
    user, sub, tl, practice = await _sub_with_practice(db)
    dealer = await make_onboarded_dealer(db, name="DAp")
    await db.commit()
    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=sub.client_id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.COMPLETED,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        status=OrderItemStatus.APPROVED,
        brand_name="Acme-X",
        given_volume=2.5,
        volume_unit="l",
        price=499,
    ))
    await db.commit()

    out = await get_today_advisory(db=db, current_user=user)
    p = _find_practice_in(out, practice.id)
    f = p["fulfilment"]
    assert f["status"] == "APPROVED"
    assert f["brand_name"] == "Acme-X"
    assert f["price"] == 499
    assert f["given_volume"] == 2.5
    assert f["volume_unit"] == "l"
    # is_purchased still set for the legacy "hide INPUT until
    # purchased" rule that runs alongside fulfilment.
    assert p["is_purchased"] is True


@requires_docker
@pytest.mark.asyncio
async def test_rerouted_item_is_hidden_from_fulfilment(db):
    user, sub, tl, practice = await _sub_with_practice(db)
    dealer = await make_onboarded_dealer(db, name="DR")
    await db.commit()
    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=sub.client_id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.CANCELLED,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        status=OrderItemStatus.REROUTED,
    ))
    await db.commit()

    out = await get_today_advisory(db=db, current_user=user)
    p = _find_practice_in(out, practice.id)
    # REROUTED is on a cancelled husk — the practice should look
    # un-ordered on the advisory walk (the farmer's DRAFT carries
    # the live story now).
    assert p["fulfilment"] is None


@requires_docker
@pytest.mark.asyncio
async def test_npk_and_siblings_surface_on_fulfilment(db):
    """Spec §3.2 — an NPK Chemical/Fertigation practice with a Mixed +
    Straight pair rendered as an AND group. Both OrderItems share
    the same `practice_id`, `relation_id`, and `relation_type='AND'`
    (stamped by /npk-select). Advisory read must surface the
    non-primary AND siblings on `fulfilment.siblings[]` so the
    farmer sees "Iffco NPK 10:26:26 + GSFC Urea — Apply together"
    instead of just the primary picked by ORDER BY updated_at.
    """
    user, sub, tl, practice = await _sub_with_practice(db)
    dealer = await make_onboarded_dealer(db, name="D-NPK")
    await db.commit()
    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=sub.client_id,
        category="FERTILIZER",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.COMPLETED,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    rel_id = "rel-npk-test-1"
    db.add(OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        status=OrderItemStatus.APPROVED,
        brand_name="Iffco NPK 10:26:26", brand_cosh_id="tn-mixed-1",
        given_volume=385, volume_unit="kg", price=15400,
        relation_id=rel_id, relation_type="AND",
        relation_role="PART_1__OPT_1__POS_1",
    ))
    db.add(OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        status=OrderItemStatus.APPROVED,
        brand_name="GSFC Urea", brand_cosh_id="tn-straight-1",
        given_volume=109, volume_unit="kg", price=1200,
        relation_id=rel_id, relation_type="AND",
        relation_role="PART_1__OPT_1__POS_2",
    ))
    await db.commit()

    out = await get_today_advisory(db=db, current_user=user)
    p = _find_practice_in(out, practice.id)
    f = p["fulfilment"]
    assert f is not None and f["status"] == "APPROVED"
    # Primary is one of the two brands; the other lands in siblings[].
    primary_brand = f["brand_name"]
    assert primary_brand in ("Iffco NPK 10:26:26", "GSFC Urea")
    sibs = f.get("siblings") or []
    assert len(sibs) == 1
    sib = sibs[0]
    assert sib["brand_name"] in ("Iffco NPK 10:26:26", "GSFC Urea")
    assert sib["brand_name"] != primary_brand
    assert sib["given_volume"] in (385.0, 109.0)
    assert sib["volume_unit"] == "kg"
    assert sib["relation_role"] in (
        "PART_1__OPT_1__POS_1", "PART_1__OPT_1__POS_2",
    )


@requires_docker
@pytest.mark.asyncio
async def test_non_npk_practice_gets_no_siblings_field(db):
    """Legacy non-NPK practices (or NPK practices with only a single
    pick — Mixed alone or Straight alone) must not sprout a `siblings`
    key on the fulfilment payload. Regression guard: the new sibling
    attach block should be a no-op when there's no AND stamp.
    """
    user, sub, tl, practice = await _sub_with_practice(db)
    dealer = await make_onboarded_dealer(db, name="D-Solo")
    await db.commit()
    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=sub.client_id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.COMPLETED,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
        status=OrderItemStatus.APPROVED,
        brand_name="SoloBrand", brand_cosh_id="tn-solo",
        given_volume=1.0, volume_unit="l", price=99,
    ))
    await db.commit()

    out = await get_today_advisory(db=db, current_user=user)
    p = _find_practice_in(out, practice.id)
    f = p["fulfilment"]
    assert f["status"] == "APPROVED"
    assert "siblings" not in f or not f["siblings"]
