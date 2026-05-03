"""
BL-05 — Lock Detection and Start Date Modification
All 7 test cases from RootsTalk_Dev_TestCases.pdf §BL-05.
"""
import pytest
from datetime import date, timedelta
from app.services.bl05_lock_detection import (
    detect_lock, compute_date_shifts,
    TimelineDateRange, OrderItemStub, LockType,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

D = date

def tl_range(id: str, from_d: D, to_d: D) -> TimelineDateRange:
    return TimelineDateRange(id=id, from_date=from_d, to_date=to_d)

def order_item(timeline_id: str, from_d: D, to_d: D, status: str = "AVAILABLE") -> OrderItemStub:
    return OrderItemStub(timeline_id=timeline_id, order_from_date=from_d, order_to_date=to_d, status=status)


# ── TC-BL05-01: Viewed lock when today is within timeline ─────────────────────

def test_bl05_01_viewed_lock_applied_when_today_in_window():
    """TC-BL05-01: Timeline Apr 1–15, today=Apr 8 → VIEWED lock detected."""
    tl = tl_range("TL_1", D(2026, 4, 1), D(2026, 4, 15))
    today = D(2026, 4, 8)

    result = detect_lock(tl, today, [])

    assert result.locked is True
    assert result.lock_type == LockType.VIEWED
    assert result.viewed_locked is True
    assert result.po_locked is False


# ── TC-BL05-02: PO lock when order covers timeline dates ──────────────────────

def test_bl05_02_po_lock_applied_with_active_order():
    """TC-BL05-02: Order placed covering Apr 10–20. Timeline Apr 15–25. PO lock detected."""
    tl = tl_range("TL_1", D(2026, 4, 15), D(2026, 4, 25))
    today = D(2026, 4, 5)  # today before timeline (no viewed lock)
    items = [order_item("TL_1", D(2026, 4, 10), D(2026, 4, 20), "AVAILABLE")]

    result = detect_lock(tl, today, items)

    assert result.locked is True
    assert result.lock_type == LockType.PURCHASE_ORDER
    assert result.po_locked is True
    assert result.viewed_locked is False


# ── TC-BL05-03: Lock is per-farmer ────────────────────────────────────────────

def test_bl05_03_lock_is_per_farmer():
    """TC-BL05-03: Farmer A's order locks TL. Farmer B has no order → not locked."""
    tl = tl_range("TL_1", D(2026, 4, 15), D(2026, 4, 25))
    today = D(2026, 4, 5)  # before timeline window

    # Farmer A has an order covering TL_1
    farmer_a_items = [order_item("TL_1", D(2026, 4, 10), D(2026, 4, 20))]
    # Farmer B has no orders
    farmer_b_items: list = []

    result_a = detect_lock(tl, today, farmer_a_items)
    result_b = detect_lock(tl, today, farmer_b_items)

    assert result_a.locked is True
    assert result_b.locked is False


# ── TC-BL05-04: Start date modification — all timelines shift by delta ─────────

def test_bl05_04_all_timelines_shift_by_delta():
    """TC-BL05-04: Start date moves +10 days. All 4 timelines shift by +10."""
    old_start = D(2026, 4, 10)
    new_start = D(2026, 4, 20)
    today = D(2026, 4, 5)  # before any timeline

    timelines = [
        tl_range("TL_1", D(2026, 4,  1), D(2026, 4, 15)),
        tl_range("TL_2", D(2026, 4, 15), D(2026, 4, 30)),
        tl_range("TL_3", D(2026, 5,  1), D(2026, 5, 20)),
        tl_range("TL_4", D(2026, 5, 20), D(2026, 6, 10)),
    ]

    results, delta = compute_date_shifts(timelines, old_start, new_start, today, [])

    assert delta == 10
    for r in results:
        original = next(t for t in timelines if t.id == r.timeline_id)
        assert r.new_from_date == original.from_date + timedelta(days=10)
        assert r.new_to_date == original.to_date + timedelta(days=10)


# ── TC-BL05-05: Locked timeline content unchanged ─────────────────────────────

def test_bl05_05_locked_timeline_content_unchanged():
    """TC-BL05-05: TL_A is PO-locked. Start date changed. Dates shift, but content_updated=False."""
    old_start = D(2026, 4, 10)
    new_start = D(2026, 4, 20)
    today = D(2026, 4, 12)  # within TL_A → viewed lock

    tl_a = tl_range("TL_A", D(2026, 4, 8), D(2026, 4, 20))  # today=Apr 12 is within → VIEWED

    results, _ = compute_date_shifts([tl_a], old_start, new_start, today, [])

    r = results[0]
    assert r.was_locked is True
    assert r.content_updated is False  # Locked: content must NOT be updated
    assert r.new_from_date == tl_a.from_date + timedelta(days=10)  # But dates shift


# ── TC-BL05-06: Unlocked timeline content updates ─────────────────────────────

def test_bl05_06_unlocked_timeline_content_updates():
    """TC-BL05-06: TL_B is unlocked. Dates shift AND content_updated=True."""
    old_start = D(2026, 4, 10)
    new_start = D(2026, 4, 20)
    today = D(2026, 4, 5)  # before any timeline → no lock

    tl_b = tl_range("TL_B", D(2026, 5, 1), D(2026, 5, 20))  # far future → no lock

    results, _ = compute_date_shifts([tl_b], old_start, new_start, today, [])

    r = results[0]
    assert r.was_locked is False
    assert r.content_updated is True  # Unlocked: content SHOULD update
    assert r.new_from_date == tl_b.from_date + timedelta(days=10)


# ── TC-BL05-07: Order dates shift with start date change ──────────────────────

def test_bl05_07_delta_is_correct_for_order_date_update():
    """TC-BL05-07: Active order Apr 10–20. Start date moves +5 days. Caller uses delta to shift order dates."""
    old_start = D(2026, 4, 10)
    new_start = D(2026, 4, 15)
    today = D(2026, 4, 5)

    tl = tl_range("TL_1", D(2026, 4, 10), D(2026, 4, 25))
    order_date_from = D(2026, 4, 10)
    order_date_to = D(2026, 4, 20)

    _, delta = compute_date_shifts([tl], old_start, new_start, today, [])

    assert delta == 5
    # Caller should apply: order.date_from += delta, order.date_to += delta
    assert order_date_from + timedelta(days=delta) == D(2026, 4, 15)
    assert order_date_to + timedelta(days=delta) == D(2026, 4, 25)


# ── TC-BL05-EXTRA-01: No lock before timeline starts ────────────────────────

def test_bl05_extra_no_lock_before_timeline():
    """Timeline starts tomorrow. Today is before it. No viewed lock, no PO lock."""
    today = date.today()
    tl = tl_range("TL_1", today + timedelta(days=1), today + timedelta(days=15))

    result = detect_lock(tl, today, [])

    assert result.locked is False
    assert result.lock_type == LockType.NONE


# ── TC-BL05-EXTRA-02: No lock after timeline ends ────────────────────────────

def test_bl05_extra_no_lock_after_timeline():
    """Timeline ended yesterday. Today is after it. No viewed lock (unless PO lock)."""
    today = date.today()
    tl = tl_range("TL_1", today - timedelta(days=15), today - timedelta(days=1))

    result = detect_lock(tl, today, [])

    assert result.locked is False
    assert result.viewed_locked is False


# ── TC-BL05-EXTRA-03: Cancelled order does not cause PO lock ─────────────────

def test_bl05_extra_cancelled_order_no_lock():
    """CANCELLED order_item does not trigger PO lock."""
    today = date.today()
    tl = tl_range("TL_1", today + timedelta(days=5), today + timedelta(days=20))

    items = [order_item("TL_1", today, today + timedelta(days=15), status="CANCELLED")]

    result = detect_lock(tl, today, items)

    assert result.po_locked is False
    assert result.locked is False


# ── TC-BL05-EXTRA-04: Delta can be negative (start moved earlier) ────────────

def test_bl05_extra_negative_delta():
    """Start date moved to earlier. Delta is negative. All timelines shift backwards."""
    old_start = D(2026, 5, 20)
    new_start = D(2026, 5, 15)  # moved 5 days earlier
    today = D(2026, 5, 1)

    timelines = [
        tl_range("TL_1", D(2026, 5, 10), D(2026, 5, 25)),
        tl_range("TL_2", D(2026, 5, 25), D(2026, 6, 10)),
    ]

    results, delta = compute_date_shifts(timelines, old_start, new_start, today, [])

    assert delta == -5
    for r in results:
        original = next(t for t in timelines if t.id == r.timeline_id)
        assert r.new_from_date == original.from_date - timedelta(days=5)


# ── Gap 1 — date-range overlap PO lock (spec §6.5) ────────────────────────────

def test_bl05_06_po_lock_overlapping_timeline_without_direct_match():
    """Order's date range covers timeline T2 even though items reference T1.
    T2 should still be PO-locked per spec §6.5."""
    from datetime import date
    from app.services.bl05_lock_detection import detect_lock, LockType, TimelineDateRange, OrderItemStub

    t2 = TimelineDateRange(id="T2", from_date=date(2026, 5, 10), to_date=date(2026, 5, 15))
    today = date(2026, 5, 1)

    items = [OrderItemStub(
        timeline_id="T1",
        order_from_date=date(2026, 5, 5),
        order_to_date=date(2026, 5, 20),
        status="AVAILABLE",
    )]

    result = detect_lock(t2, today, items)
    assert result.po_locked
    assert result.locked
    assert result.lock_type == LockType.PURCHASE_ORDER


def test_bl05_07_po_lock_no_overlap_no_lock():
    from datetime import date
    from app.services.bl05_lock_detection import detect_lock, TimelineDateRange, OrderItemStub

    timeline = TimelineDateRange(id="T1", from_date=date(2026, 5, 20), to_date=date(2026, 5, 25))
    today = date(2026, 5, 1)

    items = [OrderItemStub(
        timeline_id="X",
        order_from_date=date(2026, 5, 1),
        order_to_date=date(2026, 5, 10),
        status="AVAILABLE",
    )]

    assert not detect_lock(timeline, today, items).po_locked


def test_bl05_08_po_lock_partial_overlap_locks():
    from datetime import date
    from app.services.bl05_lock_detection import detect_lock, TimelineDateRange, OrderItemStub

    timeline = TimelineDateRange(id="T1", from_date=date(2026, 5, 10), to_date=date(2026, 5, 20))
    today = date(2026, 5, 1)

    items = [OrderItemStub(
        timeline_id="X",
        order_from_date=date(2026, 5, 18),
        order_to_date=date(2026, 5, 25),
        status="AVAILABLE",
    )]

    assert detect_lock(timeline, today, items).po_locked


def test_bl05_09_inactive_order_does_not_lock():
    from datetime import date
    from app.services.bl05_lock_detection import detect_lock, TimelineDateRange, OrderItemStub

    timeline = TimelineDateRange(id="T1", from_date=date(2026, 5, 10), to_date=date(2026, 5, 15))
    today = date(2026, 5, 1)

    items = [OrderItemStub(
        timeline_id="T1",
        order_from_date=date(2026, 5, 8),
        order_to_date=date(2026, 5, 18),
        status="CANCELLED",
    )]

    assert not detect_lock(timeline, today, items).po_locked


# ── Gap 2 — CHA timelines don't shift but are locked normally ─────────────────

def test_bl05_10_cha_timeline_does_not_shift():
    from datetime import date
    from app.services.bl05_lock_detection import compute_date_shifts, TimelineDateRange

    cca = TimelineDateRange(id="CCA1", from_date=date(2026, 5, 10), to_date=date(2026, 5, 15))
    cha = TimelineDateRange(id="CHA1", from_date=date(2026, 5, 12), to_date=date(2026, 5, 18), is_cha=True)

    old_start = date(2026, 5, 1)
    new_start = date(2026, 5, 6)  # +5 days
    today = date(2026, 5, 1)

    results, delta = compute_date_shifts([cca, cha], old_start, new_start, today, [])
    assert delta == 5

    cca_result = next(r for r in results if r.timeline_id == "CCA1")
    cha_result = next(r for r in results if r.timeline_id == "CHA1")

    assert cca_result.new_from_date == date(2026, 5, 15)
    assert cca_result.new_to_date == date(2026, 5, 20)

    assert cha_result.new_from_date == date(2026, 5, 12)
    assert cha_result.new_to_date == date(2026, 5, 18)
    assert not cha_result.content_updated


def test_bl05_11_cha_timeline_locked_when_today_inside():
    from datetime import date
    from app.services.bl05_lock_detection import detect_lock, TimelineDateRange, LockType

    cha = TimelineDateRange(id="CHA1", from_date=date(2026, 5, 1), to_date=date(2026, 5, 10), is_cha=True)
    today = date(2026, 5, 5)

    result = detect_lock(cha, today, [])
    assert result.viewed_locked
    assert result.locked
    assert result.lock_type == LockType.VIEWED
