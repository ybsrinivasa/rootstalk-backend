"""Payment-request expiry background job (runs hourly).

Cancel-and-route rule (2026-05-29): a SubscriptionPaymentRequest
that's still PENDING beyond its `expires_at` (24h after creation by
default) is auto-cancelled, and the farmer is FCM-notified so they
can take further action (delegate to someone else or pay themselves).

The 24-hour timer always runs from request creation — there is no
soft-accept intermediate state (per Option ii of the design
conversation 2026-05-29). The Facilitator either declines, completes
payment within 24h, or the request times out.

Mirrors the BL-12b query expiry pattern in
`app/tasks/query_expiry.py` — hourly crontab, async session, single
SELECT-then-UPDATE pass, FCM dispatched per row with the same
graceful-degrade behaviour as the rest of the codebase.
"""
import asyncio
import logging
from datetime import datetime, timezone

from celery import shared_task
from sqlalchemy import select

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.platform.models import User
from app.modules.subscriptions.models import SubscriptionPaymentRequest
from app.services.fcm_service import send_fcm

logger = logging.getLogger(__name__)

EXPIRY_FCM_TITLE = "Payment request expired"
EXPIRY_FCM_BODY = (
    "Your payment request was not completed within 24 hours. It has "
    "been cancelled — please choose someone else or pay yourself."
)


async def _expire_payment_requests_with_session(db, now=None) -> int:
    """Inner sweep: cancel every PENDING SubscriptionPaymentRequest
    past its expires_at. Returns the number of rows flipped.

    Split out so integration tests can inject a testcontainer session
    and assert on the rows the task commits + the FCM payloads."""
    now = now or datetime.now(timezone.utc)
    rows = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.status == "PENDING",
            SubscriptionPaymentRequest.expires_at <= now,
        )
    )).scalars().all()

    if not rows:
        return 0

    # Bulk-fetch farmers so we issue one IN-query, not one per row.
    farmer_ids = {pr.farmer_user_id for pr in rows}
    farmers_by_id = {
        u.id: u for u in (await db.execute(
            select(User).where(User.id.in_(farmer_ids))
        )).scalars().all()
    }

    for pr in rows:
        pr.status = "CANCELLED"

    await db.commit()

    # FCM after commit — even if a push fails, the DB state is
    # already correct and the next refresh will surface it.
    for pr in rows:
        farmer = farmers_by_id.get(pr.farmer_user_id)
        if not farmer or not farmer.fcm_token:
            continue
        try:
            await send_fcm(
                token=farmer.fcm_token,
                title=EXPIRY_FCM_TITLE,
                body=EXPIRY_FCM_BODY,
                data={
                    "type": "PAYMENT_REQUEST_AUTO_EXPIRED",
                    "subscription_id": pr.subscription_id,
                    "payment_request_id": pr.id,
                },
            )
        except Exception:
            logger.exception(
                "FCM send failed for expired payment_request_id=%s "
                "(farmer_user_id=%s); state already committed.",
                pr.id, pr.farmer_user_id,
            )

    return len(rows)


@shared_task(name="app.tasks.payment_request_expiry.expire_payment_requests")
def expire_payment_requests():
    """Celery entrypoint — runs hourly per `beat_schedule` in
    `app/celery_app.py`. Opens a session, runs the inner sweep,
    logs the count. Errors raised by the inner function are not
    swallowed here — Celery's normal retry / dead-letter policy
    applies."""

    async def _run() -> int:
        async with AsyncSessionLocal() as db:
            return await _expire_payment_requests_with_session(db)

    n = asyncio.run(_run())
    if n:
        logger.info("payment_request_expiry: cancelled %d expired request(s)", n)
    return n
