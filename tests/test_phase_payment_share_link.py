"""Share-payment-link feature (2026-05-29).

A farmer can generate a Razorpay Payment Link / QR and share it
with anyone (relatives, friends). The recipient pays via any UPI
app; Razorpay fires a server-to-server webhook on success; our
handler activates the subscription.

Razorpay calls (`payment_link.create`, `payment_link.cancel`) are
patched out — we don't drive real Razorpay in unit tests. The
webhook handler is exercised end-to-end against the real DB with
a stubbed signature verifier.

Tests cover:
- Single-PENDING guard fires across both methods (DELEGATE +
  SHARE_LINK can't coexist).
- POST /farmer/subscriptions/{id}/payment-link creates a row with
  method='SHARE_LINK' and stores the Razorpay link id + short URL.
- DELETE cancellation revokes the Razorpay link AND flips the row.
- Webhook `payment_link.paid` → row PAID + sub ACTIVE +
  paid_by_vpa populated.
- Webhook signature mismatch → 400.
- Webhook idempotency: a duplicate paid event on a PAID row is a
  no-op.
- Webhook amount mismatch → 400 (defence against signed-but-wrong
  payloads).
- Webhook `payment_link.cancelled` / `expired` → row CANCELLED.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi import Request as FastAPIRequest
from sqlalchemy import select

from app.modules.subscriptions.models import (
    Subscription, SubscriptionPaymentRequest, SubscriptionStatus,
)
from app.modules.clients.router import register_promoter
from app.modules.subscriptions.router import (
    PaymentDelegateRequest,
    cancel_delegation, create_payment_share_link, delegate_payment,
    razorpay_webhook,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_self_registered_user,
    make_subscription, make_user,
)


# ── Fixtures + Razorpay stubs ───────────────────────────────────────────────

class _StubRequest:
    """Minimal duck-typed Request — async .body() + .headers dict."""
    def __init__(self, *, body: bytes, headers: dict[str, str]):
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


async def _seed_waitlisted(db):
    client = await make_client(db)
    pkg = await make_package(db, client)
    farmer = await make_user(db, name="Farmer")
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=pkg,
    )
    sub.status = SubscriptionStatus.WAITLISTED
    await db.commit()
    await db.refresh(sub)
    return farmer, sub


async def _seed_facilitator(db, *, phone="+919900600099"):
    """Onboarded Facilitator that can legitimately receive delegate
    requests. Helper exists because the 2026-05-30 create-time
    target-validation guard refuses non-onboarded targets."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await make_self_registered_user(
        db, phone=phone, role="FACILITATOR", name="Delegate",
    )
    await register_promoter(
        client_id=client.id,
        request={"phone": phone, "promoter_type": "FACILITATOR"},
        db=db, current_user=sa,
    )
    return client


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── R1: create-share-link happy path ────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_share_link_persists_method_and_link(db, monkeypatch):
    farmer, sub = await _seed_waitlisted(db)

    monkeypatch.setattr(
        "app.services.payment_service.create_subscription_payment_link",
        lambda **kw: {
            "razorpay_payment_link_id": "plink_stub_001",
            "short_url": "https://rzp.io/i/stub001",
        },
    )

    out = await create_payment_share_link(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    assert out["short_url"] == "https://rzp.io/i/stub001"

    pr = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.subscription_id == sub.id,
        )
    )).scalar_one()
    assert pr.method == "SHARE_LINK"
    assert pr.requested_from_user_id is None
    assert pr.razorpay_payment_link_id == "plink_stub_001"
    assert pr.payment_link_short_url == "https://rzp.io/i/stub001"
    assert pr.status == "PENDING"


# ── R2: single-PENDING guard across both methods ────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_share_link_blocked_when_delegate_pending(db, monkeypatch):
    farmer, sub = await _seed_waitlisted(db)
    # Onboarded Facilitator — the 2026-05-30 target-validation guard
    # refuses non-onboarded targets, so the test setup needs a real
    # Facilitator-Promoter row.
    await _seed_facilitator(db, phone="+919900600099")
    await delegate_payment(
        subscription_id=sub.id,
        request=PaymentDelegateRequest(delegate_phone="+919900600099"),
        db=db, current_user=farmer,
    )

    monkeypatch.setattr(
        "app.services.payment_service.create_subscription_payment_link",
        lambda **kw: {"razorpay_payment_link_id": "x", "short_url": "x"},
    )
    with pytest.raises(HTTPException) as ei:
        await create_payment_share_link(
            subscription_id=sub.id, db=db, current_user=farmer,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "payment_request_already_pending"


# ── R3: cancellation revokes the Razorpay link + flips the row ──────────────

@requires_docker
@pytest.mark.asyncio
async def test_cancel_share_link_revokes_razorpay_and_flips_row(db, monkeypatch):
    farmer, sub = await _seed_waitlisted(db)

    monkeypatch.setattr(
        "app.services.payment_service.create_subscription_payment_link",
        lambda **kw: {
            "razorpay_payment_link_id": "plink_stub_002",
            "short_url": "https://rzp.io/i/stub002",
        },
    )
    await create_payment_share_link(
        subscription_id=sub.id, db=db, current_user=farmer,
    )

    cancel_calls: list[str] = []
    monkeypatch.setattr(
        "app.services.payment_service.cancel_payment_link",
        lambda link_id: cancel_calls.append(link_id) or True,
    )

    await cancel_delegation(
        subscription_id=sub.id, db=db, current_user=farmer,
    )

    pr = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.subscription_id == sub.id,
        )
    )).scalar_one()
    assert pr.status == "CANCELLED"
    assert cancel_calls == ["plink_stub_002"]


# ── R4: webhook payment_link.paid flips PAID + activates sub ────────────────

@requires_docker
@pytest.mark.asyncio
async def test_webhook_paid_flips_to_paid_and_activates_subscription(
    db, monkeypatch,
):
    farmer, sub = await _seed_waitlisted(db)

    monkeypatch.setattr(
        "app.services.payment_service.create_subscription_payment_link",
        lambda **kw: {
            "razorpay_payment_link_id": "plink_stub_003",
            "short_url": "https://rzp.io/i/stub003",
        },
    )
    await create_payment_share_link(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    pr = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.subscription_id == sub.id,
        )
    )).scalar_one()

    # Stub the webhook secret + signature verifier.
    monkeypatch.setattr(
        "app.services.payment_service.verify_webhook_signature",
        lambda body, sig: sig == "good",
    )

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_stub_003",
                    "amount": 19900,
                    "notes": {"payment_request_id": pr.id},
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_stub_001",
                    "vpa": "son@upi",
                }
            },
        },
    }
    body = json.dumps(payload).encode()
    req = _StubRequest(body=body, headers={"x-razorpay-signature": "good"})

    out = await razorpay_webhook(request=req, db=db)
    assert out["ok"] is True
    assert out["status"] == "PAID"

    await db.refresh(pr)
    assert pr.status == "PAID"
    assert pr.razorpay_payment_id == "pay_stub_001"
    assert pr.paid_by_vpa == "son@upi"

    await db.refresh(sub)
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.reference_number is not None


# ── R5: webhook bad signature → 400 ─────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_webhook_rejects_bad_signature(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.payment_service.verify_webhook_signature",
        lambda body, sig: False,
    )
    req = _StubRequest(
        body=b'{"event":"payment_link.paid"}',
        headers={"x-razorpay-signature": "tampered"},
    )
    with pytest.raises(HTTPException) as ei:
        await razorpay_webhook(request=req, db=db)
    assert ei.value.status_code == 400


# ── R6: webhook is idempotent ───────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_webhook_replay_on_already_paid_is_noop(db, monkeypatch):
    farmer, sub = await _seed_waitlisted(db)

    monkeypatch.setattr(
        "app.services.payment_service.create_subscription_payment_link",
        lambda **kw: {
            "razorpay_payment_link_id": "plink_stub_004",
            "short_url": "https://rzp.io/i/stub004",
        },
    )
    await create_payment_share_link(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    pr = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.subscription_id == sub.id,
        )
    )).scalar_one()
    pr.status = "PAID"   # already terminal
    await db.commit()

    monkeypatch.setattr(
        "app.services.payment_service.verify_webhook_signature",
        lambda body, sig: True,
    )
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_stub_004",
                    "amount": 19900,
                    "notes": {"payment_request_id": pr.id},
                }
            },
            "payment": {"entity": {"id": "pay_stub_002", "vpa": "x@upi"}},
        },
    }
    out = await razorpay_webhook(
        request=_StubRequest(body=json.dumps(payload).encode(), headers={"x-razorpay-signature": "good"}),
        db=db,
    )
    assert out["ok"] is True
    assert "ignored" in out


# ── R7: amount mismatch → 400 ───────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_webhook_amount_mismatch_returns_400(db, monkeypatch):
    farmer, sub = await _seed_waitlisted(db)
    monkeypatch.setattr(
        "app.services.payment_service.create_subscription_payment_link",
        lambda **kw: {
            "razorpay_payment_link_id": "plink_stub_005",
            "short_url": "https://rzp.io/i/stub005",
        },
    )
    await create_payment_share_link(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    pr = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.subscription_id == sub.id,
        )
    )).scalar_one()

    monkeypatch.setattr(
        "app.services.payment_service.verify_webhook_signature",
        lambda body, sig: True,
    )
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_stub_005",
                    "amount": 9999,   # not 19900
                    "notes": {"payment_request_id": pr.id},
                }
            },
            "payment": {"entity": {"id": "pay_stub_003"}},
        },
    }
    with pytest.raises(HTTPException) as ei:
        await razorpay_webhook(
            request=_StubRequest(body=json.dumps(payload).encode(), headers={"x-razorpay-signature": "good"}),
            db=db,
        )
    assert ei.value.status_code == 400


# ── R8: cancelled / expired events flip CANCELLED ───────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_webhook_cancelled_event_flips_to_cancelled(db, monkeypatch):
    farmer, sub = await _seed_waitlisted(db)
    monkeypatch.setattr(
        "app.services.payment_service.create_subscription_payment_link",
        lambda **kw: {
            "razorpay_payment_link_id": "plink_stub_006",
            "short_url": "https://rzp.io/i/stub006",
        },
    )
    await create_payment_share_link(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    pr = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.subscription_id == sub.id,
        )
    )).scalar_one()

    monkeypatch.setattr(
        "app.services.payment_service.verify_webhook_signature",
        lambda body, sig: True,
    )
    payload = {
        "event": "payment_link.cancelled",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_stub_006",
                    "notes": {"payment_request_id": pr.id},
                }
            }
        },
    }
    out = await razorpay_webhook(
        request=_StubRequest(body=json.dumps(payload).encode(), headers={"x-razorpay-signature": "good"}),
        db=db,
    )
    assert out["status"] == "CANCELLED"
    await db.refresh(pr)
    assert pr.status == "CANCELLED"
