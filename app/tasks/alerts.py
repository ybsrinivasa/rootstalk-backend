"""BL-09 — Daily advisory alerts: start-date and input-due notifications via SMS."""
import asyncio
import logging
from datetime import date, datetime, timezone
from celery import shared_task
from sqlalchemy import select
from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.subscriptions.models import Subscription, SubscriptionStatus, AlertRecipient, Alert, AlertType
from app.modules.advisory.models import Package, Timeline, Practice, PracticeL0
from app.modules.platform.models import User
from app.services.sms_service import send_otp_sms

logger = logging.getLogger(__name__)

SUBSCRIPTION_ALERT_SMS = "RootsTalk: {name}, your crop advisory for {package} is active but no start date is set. Please set your sowing date in the app."
INPUT_ALERT_SMS = "RootsTalk: {name}, an input is due today for your {package} crop advisory. Open RootsTalk to place your order."


async def _run_daily_alerts():
    async with AsyncSessionLocal() as db:
        today = date.today()

        subs_result = await db.execute(
            select(Subscription).where(Subscription.status == SubscriptionStatus.ACTIVE)
        )
        subs = subs_result.scalars().all()

        for sub in subs:
            # Load alert recipients for this subscription
            recip_result = await db.execute(
                select(AlertRecipient, User)
                .join(User, User.id == AlertRecipient.recipient_user_id)
                .where(AlertRecipient.subscription_id == sub.id, AlertRecipient.status == "ACTIVE")
            )
            recipients = recip_result.all()

            if not recipients:
                # Default: alert the farmer themselves
                farmer = (await db.execute(select(User).where(User.id == sub.farmer_user_id))).scalar_one_or_none()
                if farmer:
                    recipients = [(type("AR", (), {"recipient_type": "FARMER"}), farmer)]

            pkg = (await db.execute(select(Package).where(Package.id == sub.package_id))).scalar_one_or_none()
            if not pkg:
                continue

            # ── START_DATE alert ──────────────────────────────────────────────
            if not sub.crop_start_date:
                already_sent_today = (await db.execute(
                    select(Alert).where(
                        Alert.subscription_id == sub.id,
                        Alert.alert_type == AlertType.START_DATE,
                        Alert.sent_at >= datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc),
                    )
                )).scalar_one_or_none()

                if not already_sent_today:
                    for _, user in recipients:
                        if user.phone:
                            msg = SUBSCRIPTION_ALERT_SMS.format(
                                name=user.name or "Farmer",
                                package=pkg.name,
                            )
                            try:
                                await send_otp_sms(user.phone, msg)
                            except Exception as e:
                                logger.error(f"SMS failed to {user.phone}: {e}")

                        db.add(Alert(
                            subscription_id=sub.id,
                            alert_type=AlertType.START_DATE,
                            recipient_user_id=user.id,
                        ))
                continue  # Skip input alerts if no start date

            # ── INPUT_DUE alert ───────────────────────────────────────────────
            crop_start = sub.crop_start_date.date() if hasattr(sub.crop_start_date, 'date') else sub.crop_start_date
            day_offset = (today - crop_start).days

            tl_result = await db.execute(
                select(Timeline).where(Timeline.package_id == sub.package_id)
            )
            timelines = tl_result.scalars().all()

            input_due = False
            for tl in timelines:
                from_type = tl.from_type.value if hasattr(tl.from_type, 'value') else str(tl.from_type)
                if from_type == "DAS":
                    if tl.from_value <= day_offset <= tl.to_value:
                        p_result = await db.execute(
                            select(Practice).where(
                                Practice.timeline_id == tl.id,
                                Practice.l0_type == PracticeL0.INPUT,
                            )
                        )
                        if p_result.scalars().first():
                            input_due = True
                            break
                elif from_type == "DBS":
                    if -tl.to_value <= day_offset <= -tl.from_value:
                        p_result = await db.execute(
                            select(Practice).where(
                                Practice.timeline_id == tl.id,
                                Practice.l0_type == PracticeL0.INPUT,
                            )
                        )
                        if p_result.scalars().first():
                            input_due = True
                            break

            if input_due:
                already_sent_today = (await db.execute(
                    select(Alert).where(
                        Alert.subscription_id == sub.id,
                        Alert.alert_type == AlertType.INPUT,
                        Alert.sent_at >= datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc),
                    )
                )).scalar_one_or_none()

                if not already_sent_today:
                    for _, user in recipients:
                        if user.phone:
                            msg = INPUT_ALERT_SMS.format(
                                name=user.name or "Farmer",
                                package=pkg.name,
                            )
                            try:
                                await send_otp_sms(user.phone, msg)
                            except Exception as e:
                                logger.error(f"SMS failed to {user.phone}: {e}")

                        db.add(Alert(
                            subscription_id=sub.id,
                            alert_type=AlertType.INPUT,
                            recipient_user_id=user.id,
                        ))

        await db.commit()
        logger.info(f"Daily alerts processed for {len(subs)} subscriptions")


@celery_app.task(name="app.tasks.alerts.send_daily_alerts")
def send_daily_alerts():
    """BL-09: Triggered daily at 06:00 UTC. Sends start-date and input-due SMS alerts."""
    asyncio.get_event_loop().run_until_complete(_run_daily_alerts())
