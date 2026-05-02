"""Celery application instance and beat schedule."""
from celery import Celery
from celery.schedules import crontab
from app.config import settings

celery_app = Celery(
    "rootstalk",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.alerts", "app.tasks.query_expiry", "app.tasks.order_expiry"],
)

celery_app.conf.beat_schedule = {
    # BL-09: Daily advisory alerts at 06:00 UTC (11:30 IST)
    "daily-advisory-alerts": {
        "task": "app.tasks.alerts.send_daily_alerts",
        "schedule": crontab(hour=6, minute=0),
    },
    # BL-12b: Hourly query expiry check
    "query-expiry-check": {
        "task": "app.tasks.query_expiry.expire_queries",
        "schedule": crontab(minute=0),   # every hour on the hour
    },
    # BL-10: Daily order expiry — mark stale orders EXPIRED
    "order-expiry-check": {
        "task": "app.tasks.order_expiry.expire_stale_orders",
        "schedule": crontab(hour=1, minute=0),  # 01:00 UTC daily
    },
}

celery_app.conf.timezone = "UTC"
