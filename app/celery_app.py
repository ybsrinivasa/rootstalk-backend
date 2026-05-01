"""Celery application instance and beat schedule."""
from celery import Celery
from celery.schedules import crontab
from app.config import settings

celery_app = Celery(
    "rootstalk",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.alerts"],
)

celery_app.conf.beat_schedule = {
    # BL-09: Daily advisory alerts at 06:00 UTC (11:30 IST)
    "daily-advisory-alerts": {
        "task": "app.tasks.alerts.send_daily_alerts",
        "schedule": crontab(hour=6, minute=0),
    },
}

celery_app.conf.timezone = "UTC"
