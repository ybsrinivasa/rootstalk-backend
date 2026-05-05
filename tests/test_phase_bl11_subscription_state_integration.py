"""BL-11 audit — DB-backed integration tests for the transition guards
shipped in batch 2.

Pure-function coverage of the state machine lives in
`tests/test_bl11.py` (15 tests). This file drives the FastAPI route
handlers directly, with seeded rows in the testcontainer DB, to
verify the guards land correctly end-to-end. The headline test is
the double-spend prevention on `pay_subscription` — pre-fix a
duplicate hit silently consumed an extra unit from the promoter's
allocation; post-fix the second hit raises NO_OP_TRANSITION before
reaching `consume_for_assignment`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.subscriptions.models import (
    Subscription, SubscriptionPaymentRequest, SubscriptionPool,
    SubscriptionStatus, SubscriptionType,
)
from app.modules.subscriptions.promoter_allocation_models import PromoterAllocation
from app.modules.subscriptions.router import (
    pay_subscription, respond_to_assignment, unsubscribe, verify_payment,
)
from app.services.promoter_pool import allocate_to_promoter
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)


async def _seed_waitlisted_assigned_sub_with_payment_request(db, *, allocation_units: int = 1):
    """Seed: client + 1000-unit pool, dealer with `allocation_units` units
    allocated, farmer with a WAITLISTED ASSIGNED sub pointing at that
    dealer, and a PENDING SubscriptionPaymentRequest from farmer →
    dealer. Returns (sub, dealer, farmer, payment_request)."""
    client = await make_client(db)
    db.add(SubscriptionPool(
        client_id=client.id, units_purchased=1000, units_consumed=0,
    ))
    package = await make_package(db, client)
    dealer = await make_user(db, name="Dealer A")
    farmer = await make_user(db, name="Farmer A")
    await db.commit()

    if allocation_units > 0:
        await allocate_to_promoter(
            db, client_id=client.id, promoter_user_id=dealer.id, units=allocation_units,
        )

    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.subscription_type = SubscriptionType.ASSIGNED
    sub.status = SubscriptionStatus.WAITLISTED
    sub.promoter_user_id = dealer.id
    await db.commit()

    pr = SubscriptionPaymentRequest(
        subscription_id=sub.id, farmer_user_id=farmer.id,
        requested_from_user_id=dealer.id, amount=199,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
    )
    db.add(pr)
    await db.commit()
    return sub, dealer, farmer, pr


# ── Headline: double-spend prevention on pay_subscription ─────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pay_subscription_does_not_double_consume_allocation_on_replay(db):
    """The most consequential fix in this audit. Pre-fix, hitting
    pay_subscription twice (network retry, double-tap, replay) silently
    consumed an extra unit from the dealer's allocation each time,
    AND reset subscription_date / reference_number. Post-fix the
    second call raises NO_OP_TRANSITION before reaching the
    consume_for_assignment call, so the allocation is untouched."""
    sub, dealer, _, pr = await _seed_waitlisted_assigned_sub_with_payment_request(
        db, allocation_units=1,
    )

    # First pay — should succeed: allocation 1 → 0, sub WAITLISTED → ACTIVE.
    out = await pay_subscription(
        request_id=pr.id, db=db, current_user=dealer,
    )
    assert out["status"] == SubscriptionStatus.ACTIVE
    first_subscription_date = (await db.execute(
        select(Subscription.subscription_date).where(Subscription.id == sub.id)
    )).scalar_one()
    first_reference = out["reference_number"]
    balance_after_first = (await db.execute(
        select(PromoterAllocation.units_balance).where(
            PromoterAllocation.promoter_user_id == dealer.id,
        )
    )).scalar_one()
    assert balance_after_first == 0

    # Second pay — must raise NO_OP_TRANSITION, allocation untouched.
    with pytest.raises(HTTPException) as exc:
        await pay_subscription(
            request_id=pr.id, db=db, current_user=dealer,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "NO_OP_TRANSITION"

    balance_after_replay = (await db.execute(
        select(PromoterAllocation.units_balance).where(
            PromoterAllocation.promoter_user_id == dealer.id,
        )
    )).scalar_one()
    assert balance_after_replay == 0  # NOT -1
    later_subscription_date = (await db.execute(
        select(Subscription.subscription_date).where(Subscription.id == sub.id)
    )).scalar_one()
    assert later_subscription_date == first_subscription_date  # unchanged
    later_reference = (await db.execute(
        select(Subscription.reference_number).where(Subscription.id == sub.id)
    )).scalar_one()
    assert later_reference == first_reference  # unchanged


# ── Replay protection on the farmer Razorpay verify path ──────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_verify_payment_blocks_replay_on_already_active_sub(db, monkeypatch):
    """Razorpay signatures stay valid until the order is invalidated,
    so a replayed verify payload must not bounce a sub back to ACTIVE
    and reset subscription_date. Mirrors the dealer-side guard."""
    farmer = await make_user(db, name="Farmer B")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.subscription_type = SubscriptionType.SELF
    sub.status = SubscriptionStatus.ACTIVE  # already activated by a previous verify
    sub.subscription_date = datetime.now(timezone.utc) - timedelta(hours=2)
    await db.commit()

    # Bypass the actual Razorpay HMAC check.
    from app.services import payment_service
    monkeypatch.setattr(payment_service, "verify_payment_signature", lambda *a: True)

    with pytest.raises(HTTPException) as exc:
        await verify_payment(
            subscription_id=sub.id,
            data={
                "razorpay_order_id": "order_x", "razorpay_payment_id": "pay_x",
                "razorpay_signature": "sig_x",
            },
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "NO_OP_TRANSITION"


# ── respond_to_assignment guard ───────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_respond_to_assignment_blocks_revival_of_cancelled_sub(db):
    """A stale or duplicated respond request must not un-cancel a
    rejection by re-approving. CANCELLED is terminal."""
    sub, _, farmer, _ = await _seed_waitlisted_assigned_sub_with_payment_request(
        db, allocation_units=1,
    )
    sub.status = SubscriptionStatus.CANCELLED  # farmer rejected previously
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await respond_to_assignment(
            subscription_id=sub.id, data={"approved": True},
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "ILLEGAL_TRANSITION"


@requires_docker
@pytest.mark.asyncio
async def test_respond_to_assignment_blocks_re_approving_an_active_sub(db):
    """A duplicate approval on an already-ACTIVE sub used to silently
    reset subscription_date. Now blocked with NO_OP_TRANSITION."""
    sub, _, farmer, _ = await _seed_waitlisted_assigned_sub_with_payment_request(
        db, allocation_units=1,
    )
    sub.status = SubscriptionStatus.ACTIVE
    sub.subscription_date = datetime.now(timezone.utc) - timedelta(days=1)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await respond_to_assignment(
            subscription_id=sub.id, data={"approved": True},
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "NO_OP_TRANSITION"


# ── unsubscribe respects SELF-vs-ASSIGNED ────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_unsubscribe_assigned_sub_returns_400(db):
    """ASSIGNED subscriptions cannot be cancelled by the farmer — must
    go through the company. is_self_unsubscribable enforces this."""
    sub, _, farmer, _ = await _seed_waitlisted_assigned_sub_with_payment_request(
        db, allocation_units=1,
    )
    sub.status = SubscriptionStatus.ACTIVE
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await unsubscribe(
            subscription_id=sub.id, db=db, current_user=farmer,
        )
    assert exc.value.status_code == 400
    assert "company-assigned" in exc.value.detail.lower()


@requires_docker
@pytest.mark.asyncio
async def test_unsubscribe_self_active_sub_succeeds(db):
    """The other half of the predicate matrix: SELF + ACTIVE → CANCELLED."""
    farmer = await make_user(db, name="Farmer C")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.subscription_type = SubscriptionType.SELF
    sub.status = SubscriptionStatus.ACTIVE
    await db.commit()

    out = await unsubscribe(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    assert out["status"] == SubscriptionStatus.CANCELLED
