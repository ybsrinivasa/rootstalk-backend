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
    # 2026-09-28 — Advisory-Only manual-ack lock. Fires when a
    # PracticeAcknowledgement row on this TL has purchased_at set
    # (farmer recorded an offline purchase via "I've purchased this"
    # → Brands → Save). Distinct from PURCHASE_ORDER (which requires
    # an in-app OrderItem). Closes the gap where a farmer's manual
    # purchase record was silently orphaned by a crop_start shift.
    MANUAL_ACK = "MANUAL_ACK"


@dataclass
class TimelineDateRange:
    id: str
    from_date: date
    to_date: date
    is_cha: bool = False  # True for triggered CHA timelines (PG/SP) — they don't shift with crop start date
    # 2026-09-28 — stable per-lineage identifier for manual-ack lock
    # matching. PracticeAcknowledgement rows key on
    # `timeline_lineage_id`, so lock detection needs the lineage id
    # (not the per-publish `id`) to correlate. Optional for backward
    # compatibility with callers that haven't wired it yet — those
    # simply won't get MANUAL_ACK-lock protection.
    lineage_id: Optional[str] = None


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
    # 2026-09-28 — manual-ack (offline purchase record) lock.
    manual_ack_locked: bool = False


# BL-05a deviation locked 2026-05-31 (user call). Spec says the PO LOCK
# triggers only on AVAILABLE / POSTPONED / SENT_FOR_APPROVAL / APPROVED —
# PENDING items don't lock. We deviate: once an order is SENT (so its
# items become PENDING under a real recipient), neither the advisory nor
# the order's content should change for that farmer. PENDING in the set
# extends the protection to the moment of order send, not the moment of
# dealer first action. The OrderItem.snapshot_id mechanism already
# protects the dealer's fulfilment view; this makes the farmer's
# advisory view match.
ACTIVE_ORDER_STATUSES = {
    "PENDING", "AVAILABLE", "POSTPONED", "SENT_FOR_APPROVAL", "APPROVED",
}


def detect_lock(
    timeline: TimelineDateRange,
    today: date,
    active_order_items: list[OrderItemStub],
    manual_ack_lineage_ids: Optional[set[str]] = None,
) -> LockResult:
    """
    BL-05a: Detect whether a timeline is locked for a specific farmer.

    Lock types (any of which triggers a lock):
    1. VIEWED LOCK: today falls within the timeline window.
    2. PURCHASE ORDER LOCK: any active order item directly references this timeline
       (item.timeline_id == timeline.id). The lock is PER TIMELINE, NOT per order
       date-range. A new timeline inserted later whose dates fall within a previous
       order's date range is NOT locked — only timelines whose practices were
       actually ordered are locked. (Confirmed by user 2026-05-03, supersedes the
       date-range-overlap interpretation of spec §6.5 prose.)
    3. MANUAL_ACK LOCK (2026-09-28): the farmer has recorded an offline purchase
       against a practice on this TL via `PracticeAcknowledgement.purchased_at`.
       Advisory-Only Mode's "I've purchased this" flow — distinct from an in-app
       order — closes the gap where the ack was silently orphaned when the crop
       start date shifted. Caller passes the set of TL `lineage_id`s that carry
       any purchased_at row for this subscription.

    Priority for `lock_type` display when multiple triggers fire:
      PURCHASE_ORDER > MANUAL_ACK > VIEWED > NONE.

    Returns LockResult with lock type details.
    """
    # 2026-09-25 — half-open windows: `to_date` is exclusive, so the
    # VIEWED-lock trigger day is `from_date <= today < to_date`.
    viewed_locked = timeline.from_date <= today < timeline.to_date

    po_locked = any(
        item.timeline_id == timeline.id and item.status in ACTIVE_ORDER_STATUSES
        for item in active_order_items
    )

    manual_ack_locked = bool(
        manual_ack_lineage_ids
        and timeline.lineage_id is not None
        and timeline.lineage_id in manual_ack_lineage_ids
    )

    locked = viewed_locked or po_locked or manual_ack_locked
    if po_locked:
        lock_type = LockType.PURCHASE_ORDER
    elif manual_ack_locked:
        lock_type = LockType.MANUAL_ACK
    elif viewed_locked:
        lock_type = LockType.VIEWED
    else:
        lock_type = LockType.NONE

    return LockResult(
        locked=locked,
        lock_type=lock_type,
        viewed_locked=viewed_locked,
        po_locked=po_locked,
        manual_ack_locked=manual_ack_locked,
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
    manual_ack_lineage_ids: Optional[set[str]] = None,
) -> tuple[list[TimelineShiftResult], int]:
    """
    BL-05b: Compute new dates for all timelines after a start date change.

    Rules:
    - ALL timelines (locked and unlocked) shift by delta_days.
    - Locked timelines: dates shift but content stays frozen (caller handles content).
    - Unlocked timelines: dates shift AND content should update to latest published (caller handles).
    - Returns (shift_results, delta_days).

    `manual_ack_lineage_ids` (2026-09-28): set of TL `lineage_id`s
    for which the farmer has a `PracticeAcknowledgement.purchased_at
    IS NOT NULL` row on this subscription. Forwarded to `detect_lock`
    so Advisory-Only manual acks trigger a lock the same way in-app
    orders do. Optional for backward compatibility.
    """
    delta_days = (new_start_date - old_start_date).days
    results: list[TimelineShiftResult] = []

    for tl in timelines:
        lock = detect_lock(
            tl, today, active_order_items,
            manual_ack_lineage_ids=manual_ack_lineage_ids,
        )
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
    manual_ack_lineage_ids: Optional[set[str]] = None,
) -> set[str]:
    """Convenience function: returns the set of timeline IDs that are locked."""
    return {
        tl.id for tl in timelines
        if detect_lock(
            tl, today, active_order_items,
            manual_ack_lineage_ids=manual_ack_lineage_ids,
        ).locked
    }
