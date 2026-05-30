"""BL-09 — Daily advisory alerts: START_DATE and INPUT-due notifications.

Wires the live Celery task to the pure-function service in
`app/services/bl09_alerts.py`. The task only does I/O — load
subscriptions / recipients / timelines / orders / today's already-sent
alerts, and fan out notifications to recipients via SMS (if they
have a phone) and FCM push (if they have an fcm_token registered
via the PWA). Both channels are independent: a recipient with both
contact methods receives both; with one, only that one fires; with
neither, only the Alert row is written for the audit trail.
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
from app.services.fcm_service import send_fcm
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

# FCM payloads — short title for the lock-screen banner, body
# tightened from the SMS version (no "RootsTalk:" prefix, no
# salutation; the user knows it's from us because they installed
# the app).
START_DATE_ALERT_FCM_TITLE = "Set your sowing date"
START_DATE_ALERT_FCM_BODY = (
    "Your {package} advisory is active. Set your sowing date to start receiving "
    "daily guidance."
)
INPUT_ALERT_FCM_TITLE = "Input due today"
INPUT_ALERT_FCM_BODY = (
    "An input is due today for your {package} advisory. Open RootsTalk to place "
    "your order."
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
    """Given an IST date, return the corresponding UTC timestamp at IST
    midnight. Pairs with the IST-date `today` chosen in
    `_run_daily_alerts_with_session` (2026-05-31 change). Used by the
    per-day idempotency check on Alert.sent_at — without this
    conversion, a manual trigger between IST midnight and UTC midnight
    would see "no alert today" against UTC-stamped sent_at rows and
    fire duplicate alerts."""
    from datetime import timedelta as _td
    ist_offset = _td(hours=5, minutes=30)
    ist_midnight_naive = datetime.combine(today, datetime.min.time())
    return (ist_midnight_naive - ist_offset).replace(tzinfo=timezone.utc)


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
    user: User, sms_body: str, fcm_title: str, fcm_body: str,
) -> None:
    """Fan out one alert to one recipient via every channel they
    accept. SMS fires if user.phone is set; FCM push fires if
    user.fcm_token is set; both run independently of each other.
    The Alert row is written unconditionally so the audit trail
    captures every intended notification regardless of delivery
    channel availability.
    """
    if user.phone:
        try:
            await send_sms(user.phone, sms_body)
        except Exception as e:
            logger.error(f"SMS send failed to {user.phone}: {e}")
    if user.fcm_token:
        try:
            await send_fcm(
                token=user.fcm_token, title=fcm_title, body=fcm_body,
                data={
                    "alert_type": alert_type.value if hasattr(alert_type, "value") else str(alert_type),
                    "subscription_id": sub_id,
                },
            )
        except Exception as e:
            # send_fcm itself catches and returns False, but the
            # try/except is belt-and-braces in case a future change
            # removes that guard.
            logger.error(f"FCM send raised unexpectedly for user {user.id}: {e}")
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
        extra_alert_user_id=sub.extra_alert_user_id,
        alerts_extra_disabled=sub.alerts_extra_disabled,
    )

    # Alerts A (2026-05-29): the resolver now reads farmer's override
    # straight off the Subscription columns; the legacy `alert_recipients`
    # table was being read before but never written to — nothing reached
    # the sender. _load_configured_recipients is kept for now (called
    # below with no callers reading the result) so the legacy table
    # writes from any straggler code path still gather visibility in
    # logs; the resolver ignores them.
    recipients = resolve_alert_recipients(sub_view)
    if not recipients:
        return

    # Resolve User rows once for the recipients in this subscription.
    user_ids = [r.user_id for r in recipients]
    users = (await db.execute(
        select(User).where(User.id.in_(user_ids))
    )).scalars().all()
    user_by_id = {u.id: u for u in users}

    # PERENNIAL packages are calendar-driven — they don't have a sowing
    # date, so START_DATE alerts don't apply. INPUT alerts on CALENDAR
    # timelines fire purely off today's day-of-year (Alerts D, 2026-05-29).
    pkg_type = pkg.package_type.value if hasattr(pkg.package_type, "value") else str(pkg.package_type)
    is_perennial = pkg_type == "PERENNIAL"

    # ── START_DATE alert ──────────────────────────────────────────────
    sd_sent_today = await _alert_sent_today(db, sub.id, AlertType.START_DATE, today)
    if not is_perennial and should_send_start_date_alert(sub_view, sent_today=sd_sent_today):
        for recipient in recipients:
            user = user_by_id.get(recipient.user_id)
            if not user:
                continue
            sms = START_DATE_ALERT_SMS.format(
                name=user.name or "Farmer", package=pkg.name,
            )
            fcm_body = START_DATE_ALERT_FCM_BODY.format(package=pkg.name)
            await _send_to_recipient(
                db, sub.id, AlertType.START_DATE, recipient, user,
                sms_body=sms,
                fcm_title=START_DATE_ALERT_FCM_TITLE,
                fcm_body=fcm_body,
            )
        return  # no INPUT alerts before the farmer has set their start date

    # ── INPUT alert ───────────────────────────────────────────────────
    timelines = await _load_timeline_windows(db, sub.package_id)
    if crop_start is not None:
        # Annual / typed sub — pass everything through. DAS/DBS use
        # day_offset; CALENDAR uses today_date inside cca_window_active.
        day_offset = (today - crop_start).days
        due_practice_ids = find_input_practices_due_today(
            timelines, day_offset, today_date=today,
        )
    else:
        # No sowing date — only CALENDAR timelines are meaningful.
        # Filter then evaluate so DAS/DBS rows with `from_value <= 0`
        # don't accidentally match a sentinel day_offset.
        cal_only = [tl for tl in timelines if tl.from_type == "CALENDAR"]
        if not cal_only:
            return
        due_practice_ids = find_input_practices_due_today(
            cal_only, day_offset=0, today_date=today,
        )
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
        sms = INPUT_ALERT_SMS.format(
            name=user.name or "Farmer", package=pkg.name,
        )
        fcm_body = INPUT_ALERT_FCM_BODY.format(package=pkg.name)
        await _send_to_recipient(
            db, sub.id, AlertType.INPUT, recipient, user,
            sms_body=sms,
            fcm_title=INPUT_ALERT_FCM_TITLE,
            fcm_body=fcm_body,
        )


async def _run_daily_alerts_with_session(db, today: date | None = None) -> int:
    """Inner loop: takes a session, processes every ACTIVE subscription,
    commits. Split out so integration tests can inject the testcontainer
    session and assert on Alert rows it commits.

    2026-05-30 — Promoter-assigned subs are ACTIVE from initiate (no
    WAITLISTED hop), so we filter out any whose PromoterAssignment
    is still PENDING_FARMER_APPROVAL. Without this gate, daily
    advisory would start firing the moment the Promoter assigns —
    before the farmer has seen the approval card. The gate
    naturally lifts as soon as the farmer accepts (PromoterAssignment
    flips to ACTIVE).
    """
    from app.modules.subscriptions.models import (
        AssignmentStatus, PromoterAssignment,
    )
    # 2026-05-31 — "today" is IST-local, not UTC. RootsTalk operates in
    # India; farmers set their crop start date in IST. Using UTC for the
    # day-offset comparison breaks 5h30m a day (between IST midnight and
    # UTC midnight) where a freshly-set "today" crop has day_offset=-1
    # under UTC reckoning. The scheduled beat runs at 06:00 UTC = 11:30
    # IST so UTC date == IST date at the scheduled hour; this change
    # only affects the off-hours manual triggers + matches farmer-side
    # date perception.
    from datetime import timedelta as _td
    IST_OFFSET = _td(hours=5, minutes=30)
    today = today or (datetime.now(timezone.utc) + IST_OFFSET).date()
    pending_assignment_sub_ids = (await db.execute(
        select(PromoterAssignment.subscription_id).where(
            PromoterAssignment.status == AssignmentStatus.PENDING_FARMER_APPROVAL,
        )
    )).scalars().all()
    q = select(Subscription).where(Subscription.status == SubscriptionStatus.ACTIVE)
    if pending_assignment_sub_ids:
        q = q.where(Subscription.id.notin_(pending_assignment_sub_ids))
    subs = (await db.execute(q)).scalars().all()
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
