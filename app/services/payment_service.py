"""RazorPay payment service — order creation and signature verification."""
import hmac
import hashlib
import razorpay
from app.config import settings

SUBSCRIPTION_AMOUNT_PAISE = 19900  # Rs. 199.00
QUERY_AMOUNT_PAISE = 2500          # Rs. 25.00


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
