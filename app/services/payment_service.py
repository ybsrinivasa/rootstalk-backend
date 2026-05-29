"""RazorPay payment service — order creation and signature verification."""
import hmac
import hashlib
import razorpay
from app.config import settings

SUBSCRIPTION_AMOUNT_PAISE = 19900  # Rs. 199.00
QUERY_AMOUNT_PAISE = 2000          # Rs. 20.00 (locked 2026-05-27 per spec)


def _client() -> razorpay.Client:
    return razorpay.Client(auth=(
        settings.razorpay_active_key_id,
        settings.razorpay_active_key_secret,
    ))


def create_subscription_order(receipt: str) -> dict:
    """Create a RazorPay order for Rs. 199 subscription fee. Returns order details."""
    client = _client()
    order = client.order.create({
        "amount": SUBSCRIPTION_AMOUNT_PAISE,
        "currency": "INR",
        "receipt": receipt,
        "notes": {"purpose": "RootsTalk Subscription"},
    })
    return {
        "razorpay_order_id": order["id"],
        "amount": SUBSCRIPTION_AMOUNT_PAISE,
        "currency": "INR",
        "key_id": settings.razorpay_active_key_id,
    }


def create_pool_topup_order(
    receipt: str, amount_paise: int, units: int, client_id: str,
) -> dict:
    """Create a RazorPay order for a CA's subscription-pool top-up.

    `amount_paise` MUST come from the server-side pricing service
    (`app.services.subscription_pricing.quote_for`), never from the
    client. `units` and `client_id` are passed as Razorpay notes for
    audit; the verify path also re-computes the quote and cross-checks
    it against the order amount as defence in depth.
    """
    client = _client()
    order = client.order.create({
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
        "notes": {
            "purpose": "RootsTalk Pool Top-Up",
            "units": str(units),
            "client_id": client_id,
        },
    })
    return {
        "razorpay_order_id": order["id"],
        "amount": amount_paise,
        "currency": "INR",
        "key_id": settings.razorpay_active_key_id,
    }


def fetch_order_amount_paise(razorpay_order_id: str) -> int:
    """Fetch the amount of an existing Razorpay order in paise. Used by
    the pool-verify path to cross-check that no client-side tampering
    altered the unit count between create-order and verify."""
    client = _client()
    order = client.order.fetch(razorpay_order_id)
    return int(order["amount"])


def create_query_order(receipt: str) -> dict:
    """Create a RazorPay order for Rs. 25 expert query fee."""
    client = _client()
    order = client.order.create({
        "amount": QUERY_AMOUNT_PAISE,
        "currency": "INR",
        "receipt": receipt,
        "notes": {"purpose": "RootsTalk Expert Query"},
    })
    return {
        "razorpay_order_id": order["id"],
        "amount": QUERY_AMOUNT_PAISE,
        "currency": "INR",
        "key_id": settings.razorpay_active_key_id,
    }


def verify_payment_signature(razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str) -> bool:
    """Verify RazorPay payment signature. Returns True if valid."""
    message = f"{razorpay_order_id}|{razorpay_payment_id}"
    expected = hmac.new(
        settings.razorpay_active_key_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, razorpay_signature)


# ── V1.1 share-payment-link (2026-05-29) ────────────────────────────────────

def create_subscription_payment_link(
    *, payment_request_id: str, farmer_name: str | None,
    expire_in_seconds: int,
) -> dict:
    """Create a Razorpay Payment Link the farmer can share with
    anyone (e.g., a relative in a city). Returns the link's
    `id` (e.g., 'plink_xxx') and `short_url` (e.g.,
    'https://rzp.io/i/xxx').

    Constraints encoded in the request:
      • fixed amount of ₹199 (paise = SUBSCRIPTION_AMOUNT_PAISE)
      • accept_partial=false (no part-pays)
      • expire_by = now + expire_in_seconds (matches our
        SubscriptionPaymentRequest.expires_at so both sides time
        out together)
      • `notes.payment_request_id` carries our row id so the webhook
        handler can reconcile back without ambiguity.
    """
    import time
    client = _client()
    body = {
        "amount": SUBSCRIPTION_AMOUNT_PAISE,
        "currency": "INR",
        "accept_partial": False,
        "description": "RootsTalk Subscription",
        "expire_by": int(time.time()) + int(expire_in_seconds),
        "reminder_enable": True,
        "notes": {
            "purpose": "RootsTalk Subscription",
            "payment_request_id": payment_request_id,
        },
        "callback_url": "",
    }
    if farmer_name:
        body["customer"] = {"name": farmer_name}
    link = client.payment_link.create(body)
    return {
        "razorpay_payment_link_id": link["id"],
        "short_url": link["short_url"],
    }


def cancel_payment_link(payment_link_id: str) -> bool:
    """Cancel a Razorpay Payment Link. Idempotent — already-paid
    or already-cancelled links return their current state without
    error. Wrapped so callers don't need to import razorpay
    exceptions themselves. Returns True if the cancel call
    succeeded (or no-op'd cleanly), False on a real error."""
    try:
        _client().payment_link.cancel(payment_link_id)
        return True
    except Exception:
        return False


def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """Verify a Razorpay webhook signature against the configured
    webhook secret. Razorpay computes HMAC-SHA256 of the raw payload
    bytes using the webhook secret; we compute the same and
    constant-time compare. Per Razorpay docs the signature header
    is `X-Razorpay-Signature`."""
    if not signature_header:
        return False
    secret = settings.razorpay_active_webhook_secret
    if not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
