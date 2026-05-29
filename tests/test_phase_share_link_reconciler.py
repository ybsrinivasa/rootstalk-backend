"""Share-link reconciliation safety net (2026-05-29).

Exercises `_reconcile_share_link_payments_with_session` against the
real DB with a stubbed Razorpay client (no network).

Tests cover:
- PENDING + Razorpay 'paid'   → PAID + sub ACTIVE + ref generated.
- CANCELLED + Razorpay 'paid' → resurrected to PAID + sub ACTIVE
  (the race where the expire sweep cancelled before the webhook
  arrived).
- PENDING + Razorpay 'expired'/'cancelled' → CANCELLED.
- PENDING + Razorpay 'created' (still pending) → no change.
- Already PAID row → no change (idempotency).
- DELEGATE rows skipped (only SHARE_LINK is checked).
- Rows older than 48h lookback → skipped (don't hammer Razorpay
  for ancient history).
- Amount mismatch → skipped (defence against tampered payloads).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.subscriptions.models import (
    Subscription, SubscriptionPaymentRequest, SubscriptionStatus,
)
from app.tasks.share_link_reconciler import (
    _reconcile_share_link_payments_with_session,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)


# ── Stub Razorpay client ────────────────────────────────────────────────────

class _StubResource:
    def __init__(self, responses: dict):
        self._responses = responses
    def fetch(self, resource_id: str):
        return self._responses[resource_id]


class _StubClient:
    """Mimics razorpay.Client just enough for our task. Two resources
    used: `payment_link.fetch(id)` + `payment.fetch(id)`."""
    def __init__(self, *, link_responses: dict | None = None,
                 payment_responses: dict | None = None):
        self.payment_link = _StubResource(link_responses or {})
        self.payment = _StubResource(payment_responses or {})


# ── Seed helpers ────────────────────────────────────────────────────────────

async def _seed(
    db, *, link_id="plink_test_001", status="PENDING",
    method="SHARE_LINK", created_offset_hours=0, amount=199.00,
):
    client = await make_client(db)
    pkg = await make_package(db, client)
    farmer = await make_user(db, name="Farmer")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.status = SubscriptionStatus.WAITLISTED
    pr = SubscriptionPaymentRequest(
        subscription_id=sub.id,
        farmer_user_id=farmer.id,
        requested_from_user_id=None,
        amount=amount,
        status=status,
        method=method,
        razorpay_payment_link_id=link_id,
        payment_link_short_url=f"https://rzp.io/i/{link_id[-6:]}",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db.add(pr)
    await db.commit()
    # Bend created_at if the test needs a row that looks older than
    # the lookback window.
    if created_offset_hours:
        from sqlalchemy import update
        await db.execute(
            update(SubscriptionPaymentRequest)
            .where(SubscriptionPaymentRequest.id == pr.id)
            .values(
                created_at=datetime.now(timezone.utc)
                + timedelta(hours=created_offset_hours),
            )
        )
        await db.commit()
    await db.refresh(pr)
    await db.refresh(sub)
    return farmer, sub, pr


def _paid_link_response(link_id: str, payment_id: str, amount: int = 19900):
    return {
        "id": link_id,
        "status": "paid",
        "amount": amount,
        "payments": [{"payment_id": payment_id}],
        "notes": {"payment_request_id": ""},   # task doesn't read this
    }


# ── PENDING + Razorpay 'paid' → reconcile ───────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pending_paid_on_razorpay_reconciles_to_paid(db):
    _, sub, pr = await _seed(db, link_id="plink_a")

    stub = _StubClient(
        link_responses={"plink_a": _paid_link_response("plink_a", "pay_a")},
        payment_responses={"pay_a": {"id": "pay_a", "vpa": "son@upi"}},
    )
    n = await _reconcile_share_link_payments_with_session(
        db, razorpay_client=stub,
    )
    assert n == 1

    await db.refresh(pr)
    assert pr.status == "PAID"
    assert pr.razorpay_payment_id == "pay_a"
    assert pr.paid_by_vpa == "son@upi"

    await db.refresh(sub)
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.reference_number is not None


# ── CANCELLED + Razorpay 'paid' → resurrect to PAID ─────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_cancelled_but_actually_paid_is_resurrected(db):
    """The race: expire sweep cancelled at 24h+1s; Razorpay shows
    the payment landed at 23h+59m. Safety net rescues the row."""
    _, sub, pr = await _seed(db, link_id="plink_b", status="CANCELLED")

    stub = _StubClient(
        link_responses={"plink_b": _paid_link_response("plink_b", "pay_b")},
        payment_responses={"pay_b": {"id": "pay_b", "vpa": "friend@upi"}},
    )
    n = await _reconcile_share_link_payments_with_session(
        db, razorpay_client=stub,
    )
    assert n == 1

    await db.refresh(pr)
    assert pr.status == "PAID"

    await db.refresh(sub)
    assert sub.status == SubscriptionStatus.ACTIVE


# ── PENDING + Razorpay 'expired' → CANCELLED ────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pending_expired_on_razorpay_cancels(db):
    _, _, pr = await _seed(db, link_id="plink_c")
    stub = _StubClient(
        link_responses={"plink_c": {"id": "plink_c", "status": "expired"}},
    )
    n = await _reconcile_share_link_payments_with_session(
        db, razorpay_client=stub,
    )
    assert n == 1
    await db.refresh(pr)
    assert pr.status == "CANCELLED"


# ── Still pending on Razorpay → no change ───────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_still_pending_on_razorpay_no_change(db):
    _, _, pr = await _seed(db, link_id="plink_d")
    stub = _StubClient(
        link_responses={"plink_d": {"id": "plink_d", "status": "created"}},
    )
    n = await _reconcile_share_link_payments_with_session(
        db, razorpay_client=stub,
    )
    assert n == 0
    await db.refresh(pr)
    assert pr.status == "PENDING"


# ── DELEGATE rows are not touched ──────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_delegate_rows_are_skipped(db):
    _, _, pr = await _seed(db, link_id="plink_delegate", method="DELEGATE")
    # DELEGATE doesn't normally have a payment_link_id, but we set one
    # in the seed; the WHERE clause must still exclude it via method.
    stub = _StubClient(
        link_responses={"plink_delegate": _paid_link_response("plink_delegate", "pay_x")},
    )
    n = await _reconcile_share_link_payments_with_session(
        db, razorpay_client=stub,
    )
    assert n == 0
    await db.refresh(pr)
    assert pr.status == "PENDING"


# ── Lookback: rows older than 48h are skipped ───────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_rows_older_than_lookback_are_skipped(db):
    _, _, pr = await _seed(
        db, link_id="plink_old", created_offset_hours=-72,
    )
    stub = _StubClient(
        link_responses={"plink_old": _paid_link_response("plink_old", "pay_o")},
    )
    n = await _reconcile_share_link_payments_with_session(
        db, razorpay_client=stub,
    )
    assert n == 0
    await db.refresh(pr)
    assert pr.status == "PENDING"   # untouched, even though Razorpay reports paid


# ── Idempotency: already-PAID rows are skipped ──────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_already_paid_row_is_skipped(db):
    _, _, pr = await _seed(db, link_id="plink_e", status="PAID")
    stub = _StubClient(
        link_responses={"plink_e": _paid_link_response("plink_e", "pay_e")},
    )
    n = await _reconcile_share_link_payments_with_session(
        db, razorpay_client=stub,
    )
    # PAID is not in the WHERE filter (PENDING/CANCELLED only), so
    # the row never even gets fetched.
    assert n == 0


# ── Amount mismatch → skipped ───────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_amount_mismatch_is_skipped(db):
    _, _, pr = await _seed(db, link_id="plink_f")
    stub = _StubClient(
        link_responses={
            "plink_f": _paid_link_response("plink_f", "pay_f", amount=999),
        },
    )
    n = await _reconcile_share_link_payments_with_session(
        db, razorpay_client=stub,
    )
    assert n == 0
    await db.refresh(pr)
    assert pr.status == "PENDING"
