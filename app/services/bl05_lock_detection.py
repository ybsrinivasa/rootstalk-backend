"""
BL-05 — Lock Detection and Start Date Modification
Pure function service. No database access.
Spec: RootsTalk_Dev_BusinessLogic.pdf §BL-05
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional
from enum import Enum


class LockType(str, Enum):
    NONE = "NONE"
    VIEWED = "VIEWED"
    PURCHASE_ORDER = "PURCHASE_ORDER"


@dataclass
class TimelineDateRange:
    id: str
    from_date: date
    to_date: date
    is_cha: bool = False  # True for triggered CHA timelines (PG/SP) — they don't shift with crop start date


@dataclass
class OrderItemStub:
    timeline_id: str
    order_from_date: date
    order_to_date: date
    status: str  # AVAILABLE, POSTPONED, SENT_FOR_APPROVAL, APPROVED, PENDING


@dataclass
class LockResult:
    locked: bool
    lock_type: LockType
    # Lock details for UI display
    viewed_locked: bool = False
    po_locked: bool = False


ACTIVE_ORDER_STATUSES = {"AVAILABLE", "POSTPONED", "SENT_FOR_APPROVAL", "APPROVED"}


def detect_lock(
    timeline: TimelineDateRange,
    today: date,
    active_order_items: list[OrderItemStub],
) -> LockResult:
    """
    BL-05a: Detect whether a timeline is locked for a specific farmer.

    Lock types (either triggers a lock):
    1. VIEWED LOCK: today falls within the timeline window.
    2. PURCHASE ORDER LOCK: any active order item directly references this timeline
       (item.timeline_id == timeline.id). The lock is PER TIMELINE, NOT per order
       date-range. A new timeline inserted later whose dates fall within a previous
       order's date range is NOT locked — only timelines whose practices were
       actually ordered are locked. (Confirmed by user 2026-05-03, supersedes the
       date-range-overlap interpretation of spec §6.5 prose.)

    Returns LockResult with lock type details.
    """
    viewed_locked = timeline.from_date <= today <= timeline.to_date

    po_locked = any(
        item.timeline_id == timeline.id and item.status in ACTIVE_ORDER_STATUSES
        for item in active_order_items
    )

    locked = viewed_locked or po_locked
    lock_type = LockType.NONE
    if viewed_locked and po_locked:
        lock_type = LockType.PURCHASE_ORDER  # PO lock takes precedence for display
    elif po_locked:
        lock_type = LockType.PURCHASE_ORDER
    elif viewed_locked:
        lock_type = LockType.VIEWED

    return LockResult(
        locked=locked,
        lock_type=lock_type,
        viewed_locked=viewed_locked,
        po_locked=po_locked,
    )


@dataclass
class TimelineShiftResult:
    timeline_id: str
    new_from_date: date
    new_to_date: date
    was_locked: bool
    content_updated: bool   # True only for unlocked timelines


def compute_date_shifts(
    timelines: list[TimelineDateRange],
    old_start_date: date,
    new_start_date: date,
    today: date,
    active_order_items: list[OrderItemStub],
) -> tuple[list[TimelineShiftResult], int]:
    """
    BL-05b: Compute new dates for all timelines after a start date change.

    Rules:
    - ALL timelines (locked and unlocked) shift by delta_days.
    - Locked timelines: dates shift but content stays frozen (caller handles content).
    - Unlocked timelines: dates shift AND content should update to latest published (caller handles).
    - Returns (shift_results, delta_days).
    """
    delta_days = (new_start_date - old_start_date).days
    results: list[TimelineShiftResult] = []

    for tl in timelines:
        lock = detect_lock(tl, today, active_order_items)
        if tl.is_cha:
            # CHA timelines are anchored to triggered_at (real calendar day), not
            # crop_start_date. They are checked for locks but do NOT shift when the
            # crop start date moves.
            results.append(TimelineShiftResult(
                timeline_id=tl.id,
                new_from_date=tl.from_date,  # unchanged
                new_to_date=tl.to_date,      # unchanged
                was_locked=lock.locked,
                content_updated=False,        # CHA dates frozen on this path
            ))
        else:
            results.append(TimelineShiftResult(
                timeline_id=tl.id,
                new_from_date=tl.from_date + timedelta(days=delta_days),
                new_to_date=tl.to_date + timedelta(days=delta_days),
                was_locked=lock.locked,
                content_updated=not lock.locked,
            ))

    return results, delta_days


def get_all_locked_timeline_ids(
    timelines: list[TimelineDateRange],
    today: date,
    active_order_items: list[OrderItemStub],
) -> set[str]:
    """Convenience function: returns the set of timeline IDs that are locked."""
    return {
        tl.id for tl in timelines
        if detect_lock(tl, today, active_order_items).locked
    }
