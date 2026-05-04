"""Per-subscription versioning — nightly defensive snapshot sweep.

Runs once a day at 02:00 UTC. For every ACTIVE subscription with a
crop_start_date set:

  - For every CCA Timeline in the package whose BL-04 window includes
    today, ensure a (subscription_id, timeline_id, 'CCA') snapshot
    exists.
  - For every TriggeredCHAEntry on the subscription whose status is
    ACTIVE, walk its SP / PG timelines whose `triggered_at + offset`
    window includes today, and ensure a snapshot exists for each.

This is the safety net for the synchronous PO + VIEWED triggers in
the route handlers — if either fails for any reason (a transient DB
error, a race) the next sweep brings the snapshot set back into
sync.

`take_snapshot` is itself idempotent on (sub, tl, source), so the
sweep is safe to run as often as needed.

See: per_subscription_versioning.md (lock-trigger table).
"""
import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy import select

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.advisory.models import (
    PGTimeline, SPTimeline, Timeline,
)
from app.modules.subscriptions.models import (
    Subscription, SubscriptionStatus, TriggeredCHAEntry,
)
from app.services.snapshot_triggers import take_snapshots_for_keys

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.snapshot_sweep.take_missing_snapshots")
def take_missing_snapshots():
    asyncio.run(_run())


async def _run() -> dict:
    """Returns a small summary dict for logging/observability."""
    today = date.today()
    summary = {"subscriptions_scanned": 0, "snapshots_attempted": 0}

    async with AsyncSessionLocal() as db:
        subs = (await db.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.crop_start_date.isnot(None),
            )
        )).scalars().all()

        for sub in subs:
            summary["subscriptions_scanned"] += 1
            crop_start = (
                sub.crop_start_date.date()
                if hasattr(sub.crop_start_date, "date")
                else sub.crop_start_date
            )
            day_offset = (today - crop_start).days

            keys = await _collect_active_keys(db, sub, today, day_offset)
            if not keys:
                continue
            n = await take_snapshots_for_keys(
                db, sub.id, keys, lock_trigger="VIEWED",
            )
            summary["snapshots_attempted"] += n

    logger.info(
        "snapshot sweep done: scanned=%(subscriptions_scanned)s "
        "attempted=%(snapshots_attempted)s",
        summary,
    )
    return summary


async def _collect_active_keys(
    db, sub: Subscription, today: date, day_offset: int,
) -> list[tuple[str, str]]:
    """Return (timeline_id, source) pairs whose window contains today
    for this subscription. Mirrors the BL-04 logic in
    GET /farmer/advisory/today (CCA + CHA SP/PG)."""
    keys: list[tuple[str, str]] = []

    # ── CCA timelines ────────────────────────────────────────────────
    timelines = (await db.execute(
        select(Timeline).where(Timeline.package_id == sub.package_id)
    )).scalars().all()
    for tl in timelines:
        from_type = tl.from_type.value if hasattr(tl.from_type, "value") else str(tl.from_type)
        if cca_window_active(from_type, tl.from_value, tl.to_value, day_offset):
            keys.append((tl.id, "CCA"))

    # ── CHA SP / PG timelines ────────────────────────────────────────
    cha_entries = (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.subscription_id == sub.id,
            TriggeredCHAEntry.status == "ACTIVE",
        )
    )).scalars().all()

    for cha in cha_entries:
        triggered_date = (
            cha.triggered_at.date()
            if hasattr(cha.triggered_at, "date")
            else cha.triggered_at
        )
        if cha.recommendation_type == "SP":
            sp_tls = (await db.execute(
                select(SPTimeline).where(
                    SPTimeline.sp_recommendation_id == cha.recommendation_id
                )
            )).scalars().all()
            for sp_tl in sp_tls:
                if cha_window_active(
                    triggered_date, sp_tl.from_value, sp_tl.to_value, today,
                ):
                    keys.append((sp_tl.id, "SP"))
        elif cha.recommendation_type == "PG":
            pg_tls = (await db.execute(
                select(PGTimeline).where(
                    PGTimeline.pg_recommendation_id == cha.recommendation_id
                )
            )).scalars().all()
            for pg_tl in pg_tls:
                if cha_window_active(
                    triggered_date, pg_tl.from_value, pg_tl.to_value, today,
                ):
                    keys.append((pg_tl.id, "PG"))

    return keys


# ── Pure window-check helpers (BL-04 mirror) ─────────────────────────────────

def cca_window_active(
    from_type: str, from_value: int, to_value: int, day_offset: int,
) -> bool:
    """True when today falls inside this CCA timeline's window.

    DAS: from_value <= day_offset <= to_value (positive offsets).
    DBS: -to_value <= day_offset <= -from_value (offsets are negative;
         from_value is the larger # days before sowing).
    CALENDAR: not handled here (the today route also defers it).
    """
    if from_type == "DAS":
        return from_value <= day_offset <= to_value
    if from_type == "DBS":
        return -to_value <= day_offset <= -from_value
    return False


def cha_window_active(
    triggered_date: date, from_value: int, to_value: int, today: date,
) -> bool:
    """True when today falls inside this CHA timeline's `triggered_at + offset`
    window. Inclusive on both ends, matching the today route."""
    from_d = triggered_date + timedelta(days=from_value)
    to_d = triggered_date + timedelta(days=to_value)
    return from_d <= today <= to_d
