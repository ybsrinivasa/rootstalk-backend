"""Share-link reconciliation safety net (2026-05-29).

Razorpay's `payment_link.paid` webhook is the primary reconciliation
path for SHARE_LINK payments. If that webhook fails to deliver
(dashboard misconfiguration, transient network blip, max-retries
exhausted), the row stays PENDING forever and the farmer's
subscription never activates even though the money is in the
account.

This hourly task is the safety net: for each recent SHARE_LINK
row that's still PENDING (or was just-CANCELLED by the expire
sweep), fetch the link's status from Razorpay and reconcile based
on what they say.

Transitions handled:

  - Our PENDING + Razorpay 'paid'             → PAID + activate sub.
  - Our CANCELLED + Razorpay 'paid'           → PAID + activate sub.
        (The expire sweep flipped to CANCELLED at +24h; payment
        actually landed before that but the webhook never arrived.
        This resurrects it.)
  - Our PENDING + Razorpay 'expired'/'cancelled' → CANCELLED.
  - Else: no change.

Lookback window is 48h — long enough to catch the just-CANCELLED
race, short enough to avoid hammering Razorpay with fetches for
ancient rows. Anything older than that is considered terminal.

Beat schedule: hourly at minute :15. The expire sweep
(`payment_request_expiry.py`) runs at minute :00 of the same hour;
deliberately scheduled so the reconciler runs AFTER and can
resurrect any rows the expire sweep cancelled before Razorpay's
status was checked.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from celery import shared_task
from sqlalchemy import select

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.platform.models import User
from app.modules.subscriptions.models import (
    Subscription, SubscriptionPaymentRequest, SubscriptionStatus,
)
from app.services.bl11_subscription_state import (
    DEALER as BL11_DEALER, validate_transition as validate_sub_transition,
)
from app.services.fcm_service import send_fcm
from app.config import settings
from app.services.payment_service import _client

logger = logging.getLogger(__name__)

LOOKBACK_HOURS = 48


async def _reconcile_share_link_payments_with_session(
    db,
    *,
    now: Optional[datetime] = None,
    razorpay_client=None,
) -> int:
    """Inner sweep. Returns the number of rows transitioned.

    `razorpay_client` is injectable for testing — pass a stub with
    `.payment_link.fetch(id)` and `.payment.fetch(id)` and avoid
    real Razorpay calls."""
    now = now or datetime.now(timezone.utc)
    threshold = now - timedelta(hours=LOOKBACK_HOURS)
    if razorpay_client is None:
        razorpay_client = _client()

    rows = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.method == "SHARE_LINK",
            SubscriptionPaymentRequest.status.in_(("PENDING", "CANCELLED")),
            SubscriptionPaymentRequest.razorpay_payment_link_id.isnot(None),
            SubscriptionPaymentRequest.created_at >= threshold,
        )
    )).scalars().all()

    if not rows:
        return 0

    transitioned = 0
    for pr in rows:
        try:
            link = razorpay_client.payment_link.fetch(pr.razorpay_payment_link_id)
        except Exception:
            logger.exception(
                "share-link reconcile: failed to fetch link %s; will retry next sweep",
                pr.razorpay_payment_link_id,
            )
            continue

        rp_status = link.get("status")

        if rp_status == "paid":
            if pr.status == "PAID":
                continue   # webhook arrived in parallel; nothing to do.

            # Amount sanity check — same defence as the webhook handler.
            expected_amount = settings.subscription_amount_paise
            if int(link.get("amount", 0)) != expected_amount:
                logger.warning(
                    "share-link reconcile: amount mismatch on link %s "
                    "(razorpay=%s, expected=%s) — skipping",
                    pr.razorpay_payment_link_id,
                    link.get("amount"), expected_amount,
                )
                continue

            payments = link.get("payments") or []
            if not payments:
                # Razorpay reports paid but no captured payment yet —
                # transient state during settlement. Skip and retry.
                continue
            pay_id = payments[0].get("payment_id")
            vpa: Optional[str] = None
            if pay_id:
                try:
                    payment = razorpay_client.payment.fetch(pay_id)
                    vpa = payment.get("vpa")
                except Exception:
                    vpa = None

            pr.status = "PAID"
            pr.razorpay_payment_id = pay_id
            pr.paid_by_vpa = vpa

            sub = (await db.execute(
                select(Subscription).where(Subscription.id == pr.subscription_id)
            )).scalar_one_or_none()
            if sub:
                res = validate_sub_transition(
                    sub.status, SubscriptionStatus.ACTIVE.value, BL11_DEALER,
                )
                if res.allowed:
                    sub.status = SubscriptionStatus.ACTIVE
                    sub.subscription_date = now
                    if not sub.reference_number:
                        # Local import to avoid a circular import at
                        # module load — router → models → task →
                        # router would otherwise be a cycle.
                        from app.modules.subscriptions.router import (
                            _generate_reference_for_sub,
                        )
                        sub.reference_number = await _generate_reference_for_sub(
                            db, sub.client_id,
                        )

            await db.commit()
            transitioned += 1

            farmer = (await db.execute(
                select(User).where(User.id == pr.farmer_user_id)
            )).scalar_one_or_none()
            if farmer and farmer.fcm_token:
                try:
                    payer = vpa or "Your contact"
                    await send_fcm(
                        token=farmer.fcm_token,
                        title="Subscription active",
                        body=f"{payer} paid for your subscription. Open the app to see your crop advisory.",
                        data={
                            "type": "SUBSCRIPTION_ACTIVATED",
                            "subscription_id": pr.subscription_id,
                            "reference_number": sub.reference_number if sub else "",
                            "via": "reconciler",
                        },
                    )
                except Exception:
                    logger.exception(
                        "share-link reconcile: FCM send failed for farmer=%s",
                        pr.farmer_user_id,
                    )

        elif rp_status in ("expired", "cancelled"):
            if pr.status == "PENDING":
                pr.status = "CANCELLED"
                await db.commit()
                transitioned += 1
            # If our row is already CANCELLED, no-op — same terminal state.

        # rp_status in ('created', 'partially_paid', ...) or unknown:
        # leave alone. The next sweep will check again.

    return transitioned


@shared_task(name="app.tasks.share_link_reconciler.reconcile_share_link_payments")
def reconcile_share_link_payments():
    """Celery entrypoint — runs hourly at :15 per beat schedule."""

    async def _run() -> int:
        async with AsyncSessionLocal() as db:
            return await _reconcile_share_link_payments_with_session(db)

    n = asyncio.run(_run())
    if n:
        logger.info(
            "share_link_reconciler: transitioned %d row(s)", n,
        )
    return n
