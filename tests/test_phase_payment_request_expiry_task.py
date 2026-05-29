"""24-hour auto-expiry sweep for SubscriptionPaymentRequest (C5).

Exercises `_expire_payment_requests_with_session` against a real DB:
- PENDING + expires_at <= now → flipped to CANCELLED.
- PENDING but still in window → untouched.
- Already PAID / DECLINED / CANCELLED rows are untouched.

The Celery entrypoint `expire_payment_requests` is a thin wrapper
around the inner function (opens an AsyncSessionLocal, runs the
sweep, logs the count). We don't need to drive Celery in tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.subscriptions.models import (
    Subscription, SubscriptionPaymentRequest, SubscriptionStatus,
)
from app.tasks.payment_request_expiry import (
    _expire_payment_requests_with_session,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_self_registered_user,
    make_subscription, make_user,
)


async def _seed_request(
    db, *, status="PENDING", expires_in_hours=24,
    delegate_phone="+919900500099",
):
    client = await make_client(db)
    pkg = await make_package(db, client)
    farmer = await make_user(db, name="Farmer")
    delegate = await make_self_registered_user(
        db, phone=delegate_phone, role="FACILITATOR", name="Delegate",
    )
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=pkg,
    )
    sub.status = SubscriptionStatus.WAITLISTED   # factory hardcodes ACTIVE
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
    pr = SubscriptionPaymentRequest(
        subscription_id=sub.id,
        farmer_user_id=farmer.id,
        requested_from_user_id=delegate.id,
        amount=199.00,
        status=status,
        expires_at=expires_at,
    )
    db.add(pr)
    await db.commit()
    return farmer, delegate, sub, pr


@requires_docker
@pytest.mark.asyncio
async def test_expires_pending_past_window(db):
    farmer, _, sub, pr = await _seed_request(db, expires_in_hours=-1)

    n = await _expire_payment_requests_with_session(db)
    assert n == 1

    await db.refresh(pr)
    assert pr.status == "CANCELLED"


@requires_docker
@pytest.mark.asyncio
async def test_leaves_pending_inside_window_untouched(db):
    _, _, _, pr = await _seed_request(db, expires_in_hours=12)

    n = await _expire_payment_requests_with_session(db)
    assert n == 0

    await db.refresh(pr)
    assert pr.status == "PENDING"


@requires_docker
@pytest.mark.asyncio
async def test_leaves_non_pending_rows_alone(db):
    """Already PAID / DECLINED / CANCELLED rows are terminal — the
    sweep must not re-flip them, even if their expires_at is in the
    past."""
    _, _, _, pr_paid = await _seed_request(
        db, status="PAID", expires_in_hours=-2,
    )
    _, _, _, pr_declined = await _seed_request(
        db, status="DECLINED", expires_in_hours=-3,
        delegate_phone="+919900500098",
    )
    _, _, _, pr_cancelled = await _seed_request(
        db, status="CANCELLED", expires_in_hours=-4,
        delegate_phone="+919900500097",
    )

    n = await _expire_payment_requests_with_session(db)
    assert n == 0

    for pr in (pr_paid, pr_declined, pr_cancelled):
        await db.refresh(pr)
        assert pr.status in ("PAID", "DECLINED", "CANCELLED")


@requires_docker
@pytest.mark.asyncio
async def test_mixed_batch_only_flips_expired_pending(db):
    """A pool with one expired-pending + one in-window-pending + one
    already-paid: only the expired-pending row flips."""
    _, _, _, pr_expired = await _seed_request(
        db, expires_in_hours=-1,
    )
    _, _, _, pr_in_window = await _seed_request(
        db, expires_in_hours=6,
        delegate_phone="+919900500098",
    )
    _, _, _, pr_paid = await _seed_request(
        db, status="PAID", expires_in_hours=-12,
        delegate_phone="+919900500097",
    )

    n = await _expire_payment_requests_with_session(db)
    assert n == 1

    await db.refresh(pr_expired); assert pr_expired.status == "CANCELLED"
    await db.refresh(pr_in_window); assert pr_in_window.status == "PENDING"
    await db.refresh(pr_paid); assert pr_paid.status == "PAID"
