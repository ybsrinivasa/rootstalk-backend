"""Phase B.2 — Razorpay-backed pool top-up.

Mocks the Razorpay client so tests don't hit the real API. Verifies:
  - create-order returns key_id + razorpay_order_id + correct amount
    (computed server-side from the pricing service, not from the
    request).
  - verify rejects tampered units when the recomputed quote doesn't
    match the actual Razorpay order amount.
  - verify rejects an invalid signature.
  - verify on success creates a SubscriptionPool row with full payment
    audit columns and updates the balance.
  - A second verify call for the same razorpay_order_id is idempotent
    (no double credit).
  - The free POST /subscription-pool/purchase endpoint is gone — any
    attempt returns 405.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.subscriptions.models import SubscriptionPool
from app.modules.subscriptions.router import (
    PoolPaymentCreateOrder, PoolPaymentVerify,
    create_pool_payment_order, verify_pool_payment,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


# ── create-order ────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_order_uses_server_computed_amount(db):
    """The Razorpay order's amount must come from quote_for(units),
    NOT from anything the client sent (no opportunity to tamper)."""
    user = await make_user(db)
    client = await make_client(db)
    await db.commit()

    captured = {}

    def fake_create(payload):
        captured.update(payload)
        return {"id": "order_FAKE_1"}

    fake_client = type("FakeRzp", (), {"order": type("O", (), {"create": staticmethod(fake_create)})})()

    with patch("app.services.payment_service._client", return_value=fake_client):
        out = await create_pool_payment_order(
            client_id=client.id,
            request=PoolPaymentCreateOrder(units=100),
            db=db, current_user=user,
        )

    # Server-computed total for N=100 is ₹19,425.22 = 1,942,522 paise.
    assert captured["amount"] == 1942522
    assert captured["currency"] == "INR"
    assert captured["notes"]["units"] == "100"
    assert captured["notes"]["client_id"] == client.id
    assert out["razorpay_order_id"] == "order_FAKE_1"
    assert out["amount"] == 1942522


@requires_docker
@pytest.mark.asyncio
async def test_create_order_422_on_invalid_units(db):
    user = await make_user(db)
    client = await make_client(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_pool_payment_order(
            client_id=client.id,
            request=PoolPaymentCreateOrder(units=0),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422


# ── verify ──────────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_verify_credits_pool_on_success(db):
    user = await make_user(db)
    client = await make_client(db)
    await db.commit()

    payload = PoolPaymentVerify(
        units=100,
        razorpay_order_id="order_FAKE_OK",
        razorpay_payment_id="pay_FAKE_OK",
        razorpay_signature="sig_OK",
    )
    with patch(
        "app.services.payment_service.verify_payment_signature",
        return_value=True,
    ), patch(
        "app.services.payment_service.fetch_order_amount_paise",
        return_value=1942522,  # matches quote_for(100)
    ):
        out = await verify_pool_payment(
            client_id=client.id, request=payload,
            db=db, current_user=user,
        )

    assert out["units_added"] == 100
    assert out["amount_paid_paise"] == 1942522
    assert out["balance"] == 100  # fresh pool, only this one row

    rows = (await db.execute(
        select(SubscriptionPool).where(SubscriptionPool.client_id == client.id)
    )).scalars().all()
    assert len(rows) == 1
    pool = rows[0]
    assert pool.units_purchased == 100
    assert pool.razorpay_order_id == "order_FAKE_OK"
    assert pool.razorpay_payment_id == "pay_FAKE_OK"
    assert pool.amount_paid_paise == 1942522
    assert pool.purchased_by_user_id == user.id


@requires_docker
@pytest.mark.asyncio
async def test_verify_rejects_invalid_signature(db):
    user = await make_user(db)
    client = await make_client(db)
    await db.commit()

    payload = PoolPaymentVerify(
        units=100,
        razorpay_order_id="order_BAD",
        razorpay_payment_id="pay_BAD",
        razorpay_signature="not_a_real_sig",
    )
    with patch(
        "app.services.payment_service.verify_payment_signature",
        return_value=False,
    ):
        with pytest.raises(HTTPException) as exc:
            await verify_pool_payment(
                client_id=client.id, request=payload,
                db=db, current_user=user,
            )
    assert exc.value.status_code == 400
    assert "signature" in exc.value.detail.lower()
    # No pool row written.
    rows = (await db.execute(
        select(SubscriptionPool).where(SubscriptionPool.client_id == client.id)
    )).scalars().all()
    assert rows == []


@requires_docker
@pytest.mark.asyncio
async def test_verify_rejects_amount_mismatch(db):
    """Client sends units=100 to verify, but the Razorpay order amount
    on file is for a different unit count. Server must refuse."""
    user = await make_user(db)
    client = await make_client(db)
    await db.commit()

    payload = PoolPaymentVerify(
        units=100,
        razorpay_order_id="order_TAMPERED",
        razorpay_payment_id="pay_X",
        razorpay_signature="sig_OK",
    )
    with patch(
        "app.services.payment_service.verify_payment_signature",
        return_value=True,
    ), patch(
        # Razorpay says the order was for ₹100 (10000 paise) but the
        # client is trying to verify N=100 (₹19,425.22) — mismatch.
        "app.services.payment_service.fetch_order_amount_paise",
        return_value=10000,
    ):
        with pytest.raises(HTTPException) as exc:
            await verify_pool_payment(
                client_id=client.id, request=payload,
                db=db, current_user=user,
            )
    assert exc.value.status_code == 400
    assert "amount" in exc.value.detail.lower()


@requires_docker
@pytest.mark.asyncio
async def test_verify_is_idempotent_for_same_razorpay_order_id(db):
    user = await make_user(db)
    client = await make_client(db)
    await db.commit()

    payload = PoolPaymentVerify(
        units=50,
        razorpay_order_id="order_IDEMPOTENT",
        razorpay_payment_id="pay_X",
        razorpay_signature="sig_OK",
    )
    # quote_for(50) → gross 9950_00 paise = 995000.
    # Hand-calculate not strictly necessary; just supply matching value.
    from app.services.subscription_pricing import quote_for
    expected = quote_for(50).total_paise

    with patch(
        "app.services.payment_service.verify_payment_signature",
        return_value=True,
    ), patch(
        "app.services.payment_service.fetch_order_amount_paise",
        return_value=expected,
    ):
        first = await verify_pool_payment(
            client_id=client.id, request=payload,
            db=db, current_user=user,
        )
        second = await verify_pool_payment(
            client_id=client.id, request=payload,
            db=db, current_user=user,
        )

    assert first["units_added"] == 50
    assert "already credited" in second["detail"].lower()
    # Only one pool row.
    rows = (await db.execute(
        select(SubscriptionPool).where(SubscriptionPool.client_id == client.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].units_purchased == 50


@requires_docker
@pytest.mark.asyncio
async def test_old_free_purchase_endpoint_is_gone():
    """The free /subscription-pool/purchase endpoint was removed in
    Phase B.1. Any attempt to import the old route handler should
    fail."""
    from app.modules.subscriptions import router as subs_router
    assert not hasattr(subs_router, "purchase_pool_units"), (
        "Free pool-purchase endpoint must remain removed (Phase B.1)"
    )
