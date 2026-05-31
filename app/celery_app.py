"""Celery application instance and beat schedule."""
from celery import Celery
from celery.schedules import crontab
from app.config import settings

celery_app = Celery(
    "rootstalk",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.alerts",
        "app.tasks.query_expiry",
        "app.tasks.order_expiry",
        "app.tasks.account_deletion",
        "app.tasks.snapshot_sweep",
        "app.tasks.payment_request_expiry",
        "app.tasks.share_link_reconciler",
        "app.tasks.assignment_expiry",
        "app.tasks.enterprise_license_lifecycle",
        "app.tasks.postpone_expiry",
        "app.tasks.timeline_archive",
    ],
)


# asyncpg + asyncio.run() per-task pool race is handled at the engine
# level — `app.database.engine` uses `NullPool` so every task gets a
# fresh connection. No celery signal handlers needed; see
# `app/database.py` for the rationale.

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
    # 30-day grace deletion — permanently anonymise expired soft-deletes
    "anonymise-deleted-users": {
        "task": "app.tasks.account_deletion.anonymise_deleted_users",
        "schedule": crontab(hour=3, minute=0),  # 03:00 UTC daily
    },
    # Per-subscription versioning — defensive snapshot sweep at 02:00 UTC.
    # Catches any synchronous PO/VIEWED trigger misses.
    "snapshot-sweep": {
        "task": "app.tasks.snapshot_sweep.take_missing_snapshots",
        "schedule": crontab(hour=2, minute=0),
    },
    # 2026-05-29: hourly auto-cancel of payment requests past their
    # 24-hour expires_at. Notifies the farmer via FCM.
    "payment-request-expiry-check": {
        "task": "app.tasks.payment_request_expiry.expire_payment_requests",
        "schedule": crontab(minute=0),   # every hour on the hour
    },
    # 2026-05-29: share-link reconciliation safety net. Razorpay's
    # webhook is the primary path; this catches misses. Scheduled at
    # :15 so it runs AFTER the expire sweep at :00 — that way the
    # reconciler can resurrect any SHARE_LINK row the expire sweep
    # just cancelled while Razorpay's payment_link.paid was still in
    # transit. See app/tasks/share_link_reconciler.py.
    "share-link-reconcile-check": {
        "task": "app.tasks.share_link_reconciler.reconcile_share_link_payments",
        "schedule": crontab(minute=15),
    },
    # F-P B3 (2026-05-29): auto-expire PromoterAssignments older than
    # 72h. Scheduled at :30 to keep the per-minute event log readable
    # (payment-request expiry at :00, share-link reconciler at :15).
    # See app/tasks/assignment_expiry.py.
    "assignment-expiry-check": {
        "task": "app.tasks.assignment_expiry.expire_assignments",
        "schedule": crontab(minute=30),
    },
    # EL module (2026-05-30): daily sweep that fires expiry reminders
    # (30/23/16/9/2 days out) and flips ACTIVE → EXPIRED + Client
    # status → INACTIVE on the closure day. Scheduled at 01:30 UTC
    # (07:00 IST) so the closure email lands at the start of the
    # working day. See app/tasks/enterprise_license_lifecycle.py.
    "enterprise-license-lifecycle": {
        "task": "app.tasks.enterprise_license_lifecycle.sweep",
        "schedule": crontab(hour=1, minute=30),
    },
    # Orders V2 Batch 7: hourly auto-flip of POSTPONED items past
    # their postponed_until → NOT_AVAILABLE, so the farmer sees them
    # as Returned-needs-rerouting promptly. :45 keeps the per-minute
    # event log readable alongside the other hourly sweeps.
    "postpone-expiry-check": {
        "task": "app.tasks.postpone_expiry.sweep_expired_postpones",
        "schedule": crontab(minute=45),
    },
    # Orders V2 Batch 8: hourly tandem-archive of OrderItems whose
    # timeline window has closed. Mirrors the advisory's hidden-past
    # behaviour on the order side. :50 keeps it adjacent to the
    # postpone sweep without colliding.
    "timeline-archive-check": {
        "task": "app.tasks.timeline_archive.archive_expired_timeline_items",
        "schedule": crontab(minute=50),
    },
}

celery_app.conf.timezone = "UTC"
