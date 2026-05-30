"""Dealer payments parity — decorated GET + hardened decline (2026-05-30).

The dealer-side payment-request surface lagged behind the F-P side
after today's earlier cancel-and-route batch:
  - GET /dealer/payment-requests returned raw rows (no farmer name /
    phone / hours_remaining / package context).
  - PUT /dealer/payment-requests/{id}/decline had no ownership check,
    no PENDING gate, no FCM notify.

These tests pin the new parity behaviour. Mirrors the existing
F-P-side coverage in test_phase_payment_request_cancel_and_route.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.clients.models import ClientStatus
from app.modules.subscriptions.models import (
    Subscription, SubscriptionPaymentRequest, SubscriptionStatus,
)
from app.modules.subscriptions.router import (
    decline_payment, list_payment_requests, pay_subscription,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_package, make_subscription,
    make_user,
)


async def _seed(db, *, expiry_hours: int = 24):
    """Build a Dealer onboarded at a client + a WAITLISTED sub for a
    farmer + a PENDING payment request addressed to the Dealer."""
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    dealer = await make_onboarded_dealer(db, client=client)
    farmer = await make_user(db, name="Asha")
    package = await make_package(db, client, crop_cosh_id="crop:onion")
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=package,
    )
    sub.status = SubscriptionStatus.WAITLISTED
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
    pr = SubscriptionPaymentRequest(
        subscription_id=sub.id,
        farmer_user_id=farmer.id,
        requested_from_user_id=dealer.id,
        amount=199.00,
        status="PENDING",
        method="DELEGATE",
        expires_at=expires_at,
    )
    db.add(pr)
    await db.commit()
    await db.refresh(pr)
    return client, dealer, farmer, package, sub, pr


# ── GET decoration ────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_returns_decorated_row(db):
    client, dealer, farmer, package, sub, pr = await _seed(db)

    out = await list_payment_requests(db=db, current_user=dealer)
    assert len(out) == 1
    row = out[0]

    assert row["id"] == pr.id
    assert row["subscription_id"] == sub.id
    assert row["farmer_name"] == "Asha"
    assert row["farmer_phone"] == farmer.phone
    assert row["package_id"] == package.id
    assert row["package_name"] == package.name
    assert row["crop_cosh_id"] == "crop:onion"
    assert float(row["amount"]) == 199.0
    assert row["status"] == "PENDING"
    assert 0 < row["hours_remaining"] <= 24


@requires_docker
@pytest.mark.asyncio
async def test_list_excludes_terminal_rows(db):
    """Only PENDING shows up. PAID / DECLINED / CANCELLED drop off."""
    client, dealer, _, _, _, pr = await _seed(db)
    pr.status = "DECLINED"
    await db.commit()

    out = await list_payment_requests(db=db, current_user=dealer)
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_list_only_returns_requests_addressed_to_me(db):
    """If another dealer has a PENDING request elsewhere, my list
    only includes mine."""
    _, mine, _, _, _, _ = await _seed(db)
    other = await make_user(db, name="Other dealer")
    other_client = await make_client(db)
    other_client.status = ClientStatus.ACTIVE
    other_pkg = await make_package(db, other_client)
    other_farmer = await make_user(db, name="OtherFarmer")
    other_sub = await make_subscription(
        db, farmer=other_farmer, client=other_client, package=other_pkg,
    )
    other_sub.status = SubscriptionStatus.WAITLISTED
    other_pr = SubscriptionPaymentRequest(
        subscription_id=other_sub.id,
        farmer_user_id=other_farmer.id,
        requested_from_user_id=other.id,
        amount=199.00,
        status="PENDING",
        method="DELEGATE",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=10),
    )
    db.add(other_pr)
    await db.commit()

    out = await list_payment_requests(db=db, current_user=mine)
    ids = [r["id"] for r in out]
    assert other_pr.id not in ids


@requires_docker
@pytest.mark.asyncio
async def test_list_refuses_dealer_not_onboarded(db):
    """V1.1 Item 5: a self-claimed Dealer who isn't onboarded by any
    client cannot use the dealer-side endpoints. Parity with the
    F-P-side `_assert_active_facilitator` gate."""
    from app.modules.platform.models import RoleType, StatusEnum, UserRole
    lonely = await make_user(db, name="Lonely Dealer")
    db.add(UserRole(
        user_id=lonely.id, role_type=RoleType.DEALER, status=StatusEnum.ACTIVE,
    ))
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await list_payment_requests(db=db, current_user=lonely)
    # _assert_active_dealer raises 403 / 401 / 404 — the exact status
    # is whatever the existing helper returns. Either way the call
    # must fail before we touch any data.
    assert ei.value.status_code in (401, 403, 404)


# ── Decline gates ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_decline_flips_to_declined(db):
    _, dealer, _, _, _, pr = await _seed(db)

    out = await decline_payment(
        request_id=pr.id, db=db, current_user=dealer,
    )
    assert out["status"] == "DECLINED"
    await db.refresh(pr)
    assert pr.status == "DECLINED"


@requires_docker
@pytest.mark.asyncio
async def test_decline_404_when_not_owner(db):
    """A different dealer tries to decline a request addressed to
    someone else — 404, not silent state-flip."""
    _, _, _, _, _, pr = await _seed(db)
    intruder = await make_user(db, name="Intruder")
    intruder_client = await make_client(db)
    intruder_client.status = ClientStatus.ACTIVE
    intruder = await make_onboarded_dealer(db, client=intruder_client, name="Intruder")

    with pytest.raises(HTTPException) as ei:
        await decline_payment(
            request_id=pr.id, db=db, current_user=intruder,
        )
    assert ei.value.status_code == 404


# ── /pay ownership + PENDING gate (2026-05-30 follow-up) ──────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pay_404_when_not_addressed_to_caller(db):
    """Pre-fix: any user with an allocation at the request's client
    could pay someone else's payment request. The lookup now joins on
    requested_from_user_id so only the designated payer matches."""
    client, _, _, _, _, pr = await _seed(db)
    intruder_client = await make_client(db)
    intruder_client.status = ClientStatus.ACTIVE
    intruder = await make_onboarded_dealer(db, client=intruder_client, name="Intruder")

    with pytest.raises(HTTPException) as ei:
        await pay_subscription(
            request_id=pr.id, db=db, current_user=intruder,
        )
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_pay_404_when_already_terminal(db):
    """Replaying /pay on a PAID or DECLINED row 404s instead of
    silently re-flipping. Same family of replay-safety bug as the
    decline endpoint."""
    _, dealer, _, _, _, pr = await _seed(db)
    pr.status = "PAID"
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await pay_subscription(
            request_id=pr.id, db=db, current_user=dealer,
        )
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_decline_404_when_already_terminal(db):
    """Replaying decline on an already-DECLINED row 404s instead of
    re-flipping. Same family of replay-safety bug as BL-08 / BL-10 /
    BL-11."""
    _, dealer, _, _, _, pr = await _seed(db)
    pr.status = "DECLINED"
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await decline_payment(
            request_id=pr.id, db=db, current_user=dealer,
        )
    assert ei.value.status_code == 404
