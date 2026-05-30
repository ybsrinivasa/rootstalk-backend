"""Promoter-assignment expiry background job (runs hourly).

F-P B3 (2026-05-29). A PromoterAssignment that's still
PENDING_FARMER_APPROVAL beyond `EXPIRY_HOURS` (72h after
`assigned_at`) is auto-cancelled: the Assignment flips to EXPIRED,
the linked Subscription flips to CANCELLED, and the unit is
refunded to the F-P's kitty via `refund_to_promoter`. The F-P is
notified via FCM (`PROMOTER_ASSIGNMENT_AUTO_EXPIRED`).

Why 72h: farmer takes longer to respond than a payment delegate
(decision-vs-money). Picked in the F-P design lock 2026-05-29.

Scheduling: hourly at minute=30 (`crontab(minute=30)`). The
payment-request expiry runs at :00 and the share-link reconciler
at :15; this slot leaves clear airspace from them. Each task's
unit-of-work is tiny so collision wouldn't matter, but staggering
keeps the per-minute event log readable.

Mirrors `app/tasks/payment_request_expiry.py` in shape: inner
async helper drives the sweep against an injected session (so
tests can drive it without Celery); thin `@shared_task` entrypoint
wraps it for beat invocation.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy import select

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.platform.models import User
from app.modules.subscriptions.models import (
    AssignmentStatus, PromoterAssignment, Subscription, SubscriptionStatus,
)
from app.services.fcm_service import send_fcm
from app.services.promoter_pool import refund_to_promoter

logger = logging.getLogger(__name__)

EXPIRY_HOURS = 72

EXPIRY_FCM_TITLE = "Assignment expired"
EXPIRY_FCM_BODY = (
    "A farmer didn't respond to your subscription assignment within "
    "72 hours. The unit is back in your kitty."
)


async def _expire_assignments_with_session(db, now=None) -> int:
    """Inner sweep: expire every PENDING PromoterAssignment older than
    EXPIRY_HOURS. Returns the number of rows transitioned.

    Per row:
      - Assignment → EXPIRED, farmer_responded_at stays NULL
      - Subscription (linked, ACTIVE since 2026-05-30 Option A) →
        CANCELLED
      - refund_to_promoter (kitty +1, refunded_total +1)
    The status filter is the idempotency gate — a second sweep over
    an already-EXPIRED row leaves it untouched."""
    now = now or datetime.now(timezone.utc)
    threshold = now - timedelta(hours=EXPIRY_HOURS)

    rows = (await db.execute(
        select(PromoterAssignment).where(
            PromoterAssignment.status == AssignmentStatus.PENDING_FARMER_APPROVAL,
            PromoterAssignment.assigned_at <= threshold,
        )
    )).scalars().all()

    if not rows:
        return 0

    # Bulk-fetch the linked Subscriptions + promoter users so we issue
    # IN-queries instead of one-per-row.
    sub_ids = [a.subscription_id for a in rows]
    subs_by_id = {
        s.id: s for s in (await db.execute(
            select(Subscription).where(Subscription.id.in_(sub_ids))
        )).scalars().all()
    }
    promoter_ids = {a.promoter_user_id for a in rows}
    promoters_by_id = {
        u.id: u for u in (await db.execute(
            select(User).where(User.id.in_(promoter_ids))
        )).scalars().all()
    }

    transitioned = 0
    for a in rows:
        sub = subs_by_id.get(a.subscription_id)
        if sub is None:
            # Orphan assignment — surface in logs, skip; CA can
            # investigate. Don't refund — the consume side may not
            # have a coherent allocation either.
            logger.warning(
                "assignment_expiry: orphan assignment id=%s (no sub %s)",
                a.id, a.subscription_id,
            )
            continue

        # Defensive: only refund + cancel if the Sub is still ACTIVE
        # and ASSIGNED. Option A (2026-05-30) created these subs
        # ACTIVE from the start; the assignment.status filter above
        # guarantees the farmer hasn't accepted yet (which would
        # have flipped assignment to ACTIVE). If the Sub already
        # moved (rare race, e.g. CA-side cancel), don't double-process.
        if sub.status != SubscriptionStatus.ACTIVE:
            logger.info(
                "assignment_expiry: sub %s already %s; flipping "
                "assignment to EXPIRED without refund",
                sub.id, sub.status,
            )
            a.status = AssignmentStatus.EXPIRED
            continue

        try:
            await refund_to_promoter(
                db,
                client_id=sub.client_id,
                promoter_user_id=a.promoter_user_id,
            )
        except ValueError:
            # No allocation row — surface but proceed with the status
            # flips. The CA's audit-totals view will show the gap.
            logger.warning(
                "assignment_expiry: refund target missing for "
                "assignment=%s client=%s promoter=%s",
                a.id, sub.client_id, a.promoter_user_id,
            )

        a.status = AssignmentStatus.EXPIRED
        sub.status = SubscriptionStatus.CANCELLED
        transitioned += 1

    await db.commit()

    # FCM after commit — even if a push fails the DB is correct.
    for a in rows:
        if a.status != AssignmentStatus.EXPIRED:
            continue
        promoter = promoters_by_id.get(a.promoter_user_id)
        if not promoter or not promoter.fcm_token:
            continue
        try:
            await send_fcm(
                token=promoter.fcm_token,
                title=EXPIRY_FCM_TITLE,
                body=EXPIRY_FCM_BODY,
                data={
                    "type": "PROMOTER_ASSIGNMENT_AUTO_EXPIRED",
                    "assignment_id": a.id,
                    "subscription_id": a.subscription_id,
                },
            )
        except Exception:
            logger.exception(
                "FCM send failed for expired assignment_id=%s "
                "(promoter_user_id=%s); state already committed.",
                a.id, a.promoter_user_id,
            )

    return transitioned


@shared_task(name="app.tasks.assignment_expiry.expire_assignments")
def expire_assignments():
    """Celery entrypoint — hourly per beat schedule in
    `app/celery_app.py`. Errors raised in the inner sweep propagate to
    Celery's normal retry / dead-letter policy."""

    async def _run() -> int:
        async with AsyncSessionLocal() as db:
            return await _expire_assignments_with_session(db)

    n = asyncio.run(_run())
    if n:
        logger.info("assignment_expiry: expired %d assignment(s)", n)
    return n
