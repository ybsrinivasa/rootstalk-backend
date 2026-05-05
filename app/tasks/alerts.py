"""BL-09 — Daily advisory alerts: START_DATE and INPUT-due notifications via SMS.

Wires the live Celery task to the pure-function service in
`app/services/bl09_alerts.py`. The task only does I/O — load
subscriptions / recipients / timelines / orders / today's already-sent
alerts, and fan out SMS to recipients who have a phone number.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from celery import shared_task
from sqlalchemy import select

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.advisory.models import Package, Practice, PracticeL0, Timeline
from app.modules.orders.models import Order, OrderItem, OrderStatus
from app.modules.platform.models import User
from app.modules.subscriptions.models import (
    Alert, AlertRecipient, AlertType, Subscription, SubscriptionStatus,
)
from app.services.bl09_alerts import (
    AlertRecipientSpec, ConfiguredRecipient, SubscriptionView, TimelineWindow,
    find_input_practices_due_today, resolve_alert_recipients,
    should_send_input_alert, should_send_start_date_alert,
)
from app.services.sms_service import send_sms

logger = logging.getLogger(__name__)

START_DATE_ALERT_SMS = (
    "RootsTalk: {name}, your crop advisory for {package} is active "
    "but no start date is set. Please set your sowing date in the app."
)
INPUT_ALERT_SMS = (
    "RootsTalk: {name}, an input is due today for your {package} "
    "crop advisory. Open RootsTalk to place your order."
)

# Order statuses that suppress an INPUT alert. Mirrors the set in
# bl09_alerts._SUPPRESSING_ORDER_STATUSES; held here as enum values for
# the ORM comparison.
_SUPPRESSING_ORDER_STATUSES = (
    OrderStatus.DRAFT, OrderStatus.SENT, OrderStatus.ACCEPTED,
    OrderStatus.PROCESSING, OrderStatus.SENT_FOR_APPROVAL,
    OrderStatus.PARTIALLY_APPROVED, OrderStatus.COMPLETED,
)


def _start_of_today_utc(today: date) -> datetime:
    return datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)


async def _alert_sent_today(db, subscription_id: str, alert_type: AlertType, today: date) -> bool:
    row = (await db.execute(
        select(Alert).where(
            Alert.subscription_id == subscription_id,
            Alert.alert_type == alert_type,
            Alert.sent_at >= _start_of_today_utc(today),
        )
    )).first()
    return row is not None


async def _load_configured_recipients(db, subscription_id: str) -> list[ConfiguredRecipient]:
    rows = (await db.execute(
        select(AlertRecipient).where(
            AlertRecipient.subscription_id == subscription_id,
            AlertRecipient.status == "ACTIVE",
        )
    )).scalars().all()
    return [
        ConfiguredRecipient(user_id=r.recipient_user_id, role=r.recipient_type)
        for r in rows
    ]


async def _load_timeline_windows(db, package_id: str) -> list[TimelineWindow]:
    timelines = (await db.execute(
        select(Timeline).where(Timeline.package_id == package_id)
    )).scalars().all()
    out: list[TimelineWindow] = []
    for tl in timelines:
        practice_ids = (await db.execute(
            select(Practice.id).where(
                Practice.timeline_id == tl.id,
                Practice.l0_type == PracticeL0.INPUT,
            )
        )).scalars().all()
        if not practice_ids:
            continue
        from_type = tl.from_type.value if hasattr(tl.from_type, "value") else str(tl.from_type)
        out.append(TimelineWindow(
            timeline_id=tl.id, from_type=from_type,
            from_value=int(tl.from_value), to_value=int(tl.to_value),
            input_practice_ids=tuple(practice_ids),
        ))
    return out


async def _load_active_order_practice_ids(db, subscription_id: str) -> set[str]:
    """Practice IDs that already have a live order on this subscription —
    these suppress today's INPUT alert."""
    rows = (await db.execute(
        select(OrderItem.practice_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.subscription_id == subscription_id,
            Order.status.in_(_SUPPRESSING_ORDER_STATUSES),
        )
    )).scalars().all()
    return set(rows)


async def _send_to_recipient(
    db, sub_id: str, alert_type: AlertType, recipient: AlertRecipientSpec,
    user: User, message: str,
) -> None:
    if user.phone:
        try:
            await send_sms(user.phone, message)
        except Exception as e:
            logger.error(f"SMS send failed to {user.phone}: {e}")
    db.add(Alert(
        subscription_id=sub_id,
        alert_type=alert_type,
        recipient_user_id=recipient.user_id,
    ))


async def _process_subscription(db, sub: Subscription, today: date) -> None:
    pkg = (await db.execute(
        select(Package).where(Package.id == sub.package_id)
    )).scalar_one_or_none()
    if not pkg:
        return

    crop_start = sub.crop_start_date.date() if sub.crop_start_date else None
    sub_view = SubscriptionView(
        subscription_id=sub.id,
        subscription_type=sub.subscription_type.value if hasattr(sub.subscription_type, "value") else str(sub.subscription_type),
        farmer_user_id=sub.farmer_user_id,
        promoter_user_id=sub.promoter_user_id,
        crop_start_date=crop_start,
    )

    configured = await _load_configured_recipients(db, sub.id)
    recipients = resolve_alert_recipients(sub_view, configured)
    if not recipients:
        return

    # Resolve User rows once for the recipients in this subscription.
    user_ids = [r.user_id for r in recipients]
    users = (await db.execute(
        select(User).where(User.id.in_(user_ids))
    )).scalars().all()
    user_by_id = {u.id: u for u in users}

    # ── START_DATE alert ──────────────────────────────────────────────
    sd_sent_today = await _alert_sent_today(db, sub.id, AlertType.START_DATE, today)
    if should_send_start_date_alert(sub_view, sent_today=sd_sent_today):
        for recipient in recipients:
            user = user_by_id.get(recipient.user_id)
            if not user:
                continue
            msg = START_DATE_ALERT_SMS.format(
                name=user.name or "Farmer", package=pkg.name,
            )
            await _send_to_recipient(db, sub.id, AlertType.START_DATE, recipient, user, msg)
        return  # no INPUT alerts before the farmer has set their start date

    # ── INPUT alert ───────────────────────────────────────────────────
    if crop_start is None:
        return
    day_offset = (today - crop_start).days

    timelines = await _load_timeline_windows(db, sub.package_id)
    due_practice_ids = find_input_practices_due_today(timelines, day_offset)
    if not due_practice_ids:
        return

    ordered_pids = await _load_active_order_practice_ids(db, sub.id)
    in_sent_today = await _alert_sent_today(db, sub.id, AlertType.INPUT, today)
    if not should_send_input_alert(
        sub_view, due_practice_ids, ordered_pids, sent_today=in_sent_today,
    ):
        return

    for recipient in recipients:
        user = user_by_id.get(recipient.user_id)
        if not user:
            continue
        msg = INPUT_ALERT_SMS.format(
            name=user.name or "Farmer", package=pkg.name,
        )
        await _send_to_recipient(db, sub.id, AlertType.INPUT, recipient, user, msg)


async def _run_daily_alerts_with_session(db, today: date | None = None) -> int:
    """Inner loop: takes a session, processes every ACTIVE subscription,
    commits. Split out so integration tests can inject the testcontainer
    session and assert on Alert rows it commits."""
    today = today or datetime.now(timezone.utc).date()
    subs = (await db.execute(
        select(Subscription).where(Subscription.status == SubscriptionStatus.ACTIVE)
    )).scalars().all()
    for sub in subs:
        await _process_subscription(db, sub, today)
    await db.commit()
    logger.info(f"Daily alerts processed for {len(subs)} subscriptions")
    return len(subs)


async def _run_daily_alerts() -> int:
    """Production entry point: opens its own session and runs the inner
    loop. Every-day idempotency is enforced inside `_process_subscription`
    via the `_alert_sent_today` lookups."""
    async with AsyncSessionLocal() as db:
        return await _run_daily_alerts_with_session(db)


@celery_app.task(name="app.tasks.alerts.send_daily_alerts")
def send_daily_alerts() -> None:
    """BL-09: Triggered daily at 06:00 UTC. Sends START_DATE and INPUT
    SMS alerts to the configured recipients, defaulting to farmer plus
    assigning promoter when no preferences are set."""
    asyncio.run(_run_daily_alerts())
