"""Subscription Touchpoints firing engine — daily sweep.

Runs once a day (06:00 UTC = 11:30 IST — paired with the existing
BL-09 alerts beat so farmers get a single "morning dose" of new
content). For each active TouchpointAssignment × active Subscription
pair where the assignment's milestone has been reached and we haven't
fired yet, this task:

1. Computes the milestone timestamp via the existing
   `bl17_timeline_boundary.compute_window` helper (DAS/DBS supported;
   CALENDAR skipped with a log — same convention used by the alerts
   engine).
2. Non-retroactive gate: `milestone_ts >= assignment.activated_at`.
   Farmers already past the milestone when the CA activated the
   assignment DO NOT receive. Per spec §4.2.
3. Fire gate: `milestone_ts <= now`.
4. Dedupe gate: UNIQUE `(assignment_id, subscription_id)` on
   touchpoint_recipients means re-runs on the same (sub, assignment)
   are no-ops. If the sweep crashes mid-batch and retries, already-
   fired rows are skipped by the UNIQUE conflict.
5. Writes the recipient row with denormalised context columns
   (crop_cosh_id, timeline_id, timeline_name, fire_at,
   recipient_name/phone/language/state/district) so analytics and
   audit don't need live joins post-firing.
6. Enqueues `touchpoint_dispatch` to push FCM to the newly-fired
   recipients in batches.

Design notes:
- Per-assignment work wrapped in try/except so one bad
  assignment doesn't strand the whole sweep.
- Touchpoint-level gates: parent Touchpoint must be ACTIVE (not
  PAUSED/ARCHIVED/DRAFT) AND assignment must be ACTIVE.
- Subscription gate: ACTIVE status AND `crop_start_date` set AND
  not soft-deleted.
- CALENDAR timelines are logged + skipped — they have no crop_start
  anchor so can't be fired. If a CA accidentally picks a CALENDAR
  timeline during composer (shouldn't happen — picker filters by
  `status == ACTIVE` but doesn't filter from_type), nothing bad
  happens; the sweep just skips.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.advisory.models import Package, Timeline
from app.modules.platform.models import User
from app.modules.subscriptions.models import Subscription, SubscriptionStatus
from app.modules.touchpoints.models import (
    Touchpoint, TouchpointAssignment, TouchpointAssignmentStatus,
    TouchpointFireAt, TouchpointRecipient, TouchpointStatus,
)
from app.services.bl17_timeline_boundary import compute_window


logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.touchpoint_firing.sweep_touchpoint_firings")
def sweep_touchpoint_firings() -> dict:
    """Celery entrypoint. Thin wrapper around the async sweep."""
    return asyncio.run(_run())


async def _run() -> dict:
    now = datetime.now(timezone.utc)
    assignments_scanned = 0
    firings_written = 0
    errored: list[str] = []
    skipped_calendar = 0

    async with AsyncSessionLocal() as db:
        # 1. Fetch every ACTIVE assignment whose parent Touchpoint is
        #    also ACTIVE. Join to Package + Timeline for the firing
        #    math and to the parent Touchpoint for the per_firing_
        #    expiry_days + content gates.
        rows = (await db.execute(
            select(
                TouchpointAssignment, Touchpoint, Package, Timeline,
            )
            .join(Touchpoint, Touchpoint.id == TouchpointAssignment.touchpoint_id)
            .join(Package, Package.id == TouchpointAssignment.package_id)
            .join(Timeline, Timeline.id == TouchpointAssignment.timeline_id)
            .where(
                TouchpointAssignment.status == TouchpointAssignmentStatus.ACTIVE.value,
                Touchpoint.status == TouchpointStatus.ACTIVE.value,
            )
        )).all()
        if not rows:
            return {
                "assignments_scanned": 0, "firings_written": 0,
                "skipped_calendar": 0, "swept_at": now.isoformat(),
            }

        # 2. For each (assignment, timeline, subscription) triple we
        #    need to decide whether to fire. We do this per assignment
        #    so per-assignment errors stay contained.
        for assignment, touchpoint, package, timeline in rows:
            assignments_scanned += 1
            try:
                # CALENDAR timelines have no crop_start anchor — skip
                # with a log. The composer should prevent this but
                # the backend is defensive.
                if timeline.from_type.value == "CALENDAR" if hasattr(timeline.from_type, "value") else timeline.from_type == "CALENDAR":
                    skipped_calendar += 1
                    logger.info(
                        "touchpoint_firing: skip CALENDAR assignment=%s "
                        "timeline=%s", assignment.id, timeline.id,
                    )
                    continue

                # 3. Candidate subscriptions: ACTIVE on this package
                #    with a crop_start_date set and not soft-deleted.
                #    Soft-delete is auto-filtered by the session-level
                #    listener (installed in main.py); explicit check
                #    on crop_start_date here.
                sub_rows = (await db.execute(
                    select(Subscription).where(
                        Subscription.package_id == assignment.package_id,
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                        Subscription.crop_start_date.isnot(None),
                    )
                )).scalars().all()
                if not sub_rows:
                    continue

                # 4. Per-sub milestone math. compute_window returns
                #    None for CALENDAR (already handled above).
                #    `opens_at` = Beginning milestone, `closes_at` = End.
                from_type_str = (
                    timeline.from_type.value
                    if hasattr(timeline.from_type, "value")
                    else str(timeline.from_type)
                )
                for sub in sub_rows:
                    crop_start_date = sub.crop_start_date.date()
                    window = compute_window(
                        from_type=from_type_str,
                        from_value=timeline.from_value,
                        to_value=timeline.to_value,
                        crop_start=crop_start_date,
                        timeline_id=timeline.id,
                    )
                    if window is None:
                        continue
                    milestone_ts = (
                        window.opens_at
                        if assignment.fire_at == TouchpointFireAt.BEGINNING.value
                        else window.closes_at
                    )

                    # Non-retroactive gate: milestone must be at or
                    # after the assignment's activation moment.
                    if milestone_ts < assignment.activated_at:
                        continue

                    # Fire gate: milestone must have been reached.
                    if milestone_ts > now:
                        continue

                    # 5. Write the recipient row. UNIQUE (assignment_id,
                    #    subscription_id) catches duplicate firing if
                    #    the sweep somehow re-runs the same pair in
                    #    the same tick. Catch IntegrityError + skip.
                    farmer = (await db.execute(
                        select(User).where(User.id == sub.farmer_user_id)
                    )).scalar_one_or_none()
                    if farmer is None:
                        # Farmer anonymised — skip firing. The audit
                        # trail still has the subscription linkage.
                        continue

                    expires_at = now + timedelta(
                        days=max(1, int(touchpoint.per_firing_expiry_days or 7))
                    )
                    recipient = TouchpointRecipient(
                        touchpoint_id=touchpoint.id,
                        assignment_id=assignment.id,
                        subscription_id=sub.id,
                        user_id=farmer.id,
                        crop_cosh_id=package.crop_cosh_id,
                        timeline_id=timeline.id,
                        timeline_name=timeline.name,
                        fire_at=assignment.fire_at,
                        recipient_name=(farmer.name or "")[:200],
                        recipient_phone=(farmer.phone or "")[:20],
                        recipient_language_code=farmer.language_code,
                        recipient_state_cosh_id=farmer.state_cosh_id,
                        recipient_district_cosh_id=farmer.district_cosh_id,
                        fired_at=now,
                        expires_at=expires_at,
                    )
                    db.add(recipient)
                    try:
                        await db.flush()
                    except IntegrityError:
                        # Another concurrent sweep or earlier run
                        # already wrote this pair. Roll back the
                        # single row and move on.
                        await db.rollback()
                        continue
                    firings_written += 1

                # Commit per-assignment so a mid-sweep crash still
                # preserves progress for the assignments already
                # processed.
                await db.commit()

            except Exception as exc:
                await db.rollback()
                errored.append(assignment.id)
                logger.exception(
                    "touchpoint_firing: assignment=%s failed: %s",
                    assignment.id, exc,
                )

    # 6. Enqueue dispatch — single job walks every newly-pushed
    #    recipient (those with pushed_at IS NULL). Same idempotent
    #    pattern as campaign_dispatch: re-runs skip already-pushed.
    if firings_written > 0:
        from app.tasks.touchpoint_dispatch import dispatch_touchpoint_firings
        dispatch_touchpoint_firings.delay()

    return {
        "assignments_scanned": assignments_scanned,
        "firings_written": firings_written,
        "skipped_calendar": skipped_calendar,
        "errored": errored,
        "swept_at": now.isoformat(),
    }
