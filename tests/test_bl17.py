"""BL-17 — pure-function tests for the timeline-boundary service.

Live router wiring is exercised by the integration tests in batch 3.
This file is hermetic.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.services.bl17_timeline_boundary import (
    Conflict, TimelineSpec,
    compute_window, find_timeline_conflicts, to_day_offset_range,
)


# ── compute_window: time-of-day boundaries ────────────────────────────────────

def test_das_window_opens_at_midnight_and_closes_at_end_of_day():
    """The headline boundary spec — DAS opens at 00:00:00 of
    (start + from_value), closes at 23:59:59 of (start + to_value)."""
    crop_start = date(2026, 5, 1)
    out = compute_window(
        from_type="DAS", from_value=10, to_value=20,
        crop_start=crop_start, timeline_id="t1",
    )
    assert out is not None
    assert out.opens_at == datetime(2026, 5, 11, 0, 0, 0, tzinfo=timezone.utc)
    assert out.closes_at == datetime(2026, 5, 21, 23, 59, 59, tzinfo=timezone.utc)


def test_dbs_window_uses_production_from_greater_than_to_convention():
    """DBS production convention: from=15, to=8 means 'active 15 → 8
    days before sowing'. opens_at is the EARLIER date (start - from);
    closes_at is the LATER date (start - to). Verified by the BL-04
    audit; pinned here too."""
    crop_start = date(2026, 5, 1)
    out = compute_window(
        from_type="DBS", from_value=15, to_value=8,
        crop_start=crop_start, timeline_id="t1",
    )
    assert out is not None
    # 15 days before May 1 = April 16; 8 days before = April 23.
    assert out.opens_at == datetime(2026, 4, 16, 0, 0, 0, tzinfo=timezone.utc)
    assert out.closes_at == datetime(2026, 4, 23, 23, 59, 59, tzinfo=timezone.utc)
    assert out.opens_at < out.closes_at


def test_calendar_window_returns_none():
    """CALENDAR has no anchor against crop_start. The helper returns
    None — matches the existing convention in
    snapshot_render.cca_window_active and the BL-04 today route."""
    out = compute_window(
        from_type="CALENDAR", from_value=6, to_value=8,
        crop_start=date(2026, 5, 1),
    )
    assert out is None


def test_window_is_timezone_aware_utc():
    """UTC convention everywhere — matches the BL-09 alerts day
    boundary fix that swapped to `datetime.now(timezone.utc).date()`.
    A naive datetime would silently coerce to host TZ at compare time
    and break boundary correctness near midnight."""
    out = compute_window(
        from_type="DAS", from_value=0, to_value=5,
        crop_start=date(2026, 5, 1),
    )
    assert out is not None
    assert out.opens_at.tzinfo == timezone.utc
    assert out.closes_at.tzinfo == timezone.utc


# ── to_day_offset_range: structural ──────────────────────────────────────────

def test_das_offset_range_is_positive_and_increasing():
    assert to_day_offset_range("DAS", 10, 20) == (10, 20)


def test_dbs_offset_range_is_negative_with_from_being_more_negative():
    """Production convention: from=15, to=8 → range (-15, -8). The
    tuple stays (smaller, larger) so it can be compared with DAS
    ranges on the same number line for gap/overlap detection."""
    rng = to_day_offset_range("DBS", 15, 8)
    assert rng == (-15, -8)
    assert rng[0] < rng[1]


def test_calendar_offset_range_returns_none():
    assert to_day_offset_range("CALENDAR", 0, 0) is None


# ── find_timeline_conflicts: clean cases ─────────────────────────────────────

def test_single_timeline_has_no_conflicts():
    """A package with one timeline can't conflict with itself."""
    out = find_timeline_conflicts([
        TimelineSpec("t1", "DAS", 0, 30),
    ])
    assert out == []


def test_adjacent_das_timelines_have_no_gap_no_overlap():
    """Spec rule: (5-10) and (11-20) are adjacent — day 11 starts
    where day 10 ended, no day uncovered, no day double-covered."""
    out = find_timeline_conflicts([
        TimelineSpec("t1", "DAS", 5, 10),
        TimelineSpec("t2", "DAS", 11, 20),
    ])
    assert out == []


# ── find_timeline_conflicts: gap detection ───────────────────────────────────

def test_das_gap_is_detected():
    """(0-10) and (15-30) — days 11/12/13/14 have no coverage. Spec
    says 'no gaps' — pre-audit the live router didn't enforce this
    at all."""
    out = find_timeline_conflicts([
        TimelineSpec("t1", "DAS", 0, 10),
        TimelineSpec("t2", "DAS", 15, 30),
    ])
    assert len(out) == 1
    assert out[0].kind == "GAP"
    assert out[0].timeline_a_id == "t1"
    assert out[0].timeline_b_id == "t2"
    assert "4-day gap" in out[0].detail


def test_dbs_to_das_gap_is_detected_across_sowing_day():
    """Mixed DBS + DAS timelines on the same package. DBS covers
    -15..-8 (8 days before to 1 day before — wait, -15..-8 includes
    8 days; check). DAS covers 0..30. Day -7..-1 (week before
    sowing) and day 0 itself fall through. Day 0 is actually day
    offset 0, which IS in the DAS range (0..30). So the gap is
    -7..-1 — 7 days uncovered."""
    out = find_timeline_conflicts([
        TimelineSpec("dbs", "DBS", 15, 8),     # covers -15 to -8
        TimelineSpec("das", "DAS", 0, 30),     # covers 0 to 30
    ])
    assert len(out) == 1
    assert out[0].kind == "GAP"
    # Gap from -8 to 0 → 7 days uncovered (-7, -6, -5, -4, -3, -2, -1).
    assert "7-day gap" in out[0].detail


# ── find_timeline_conflicts: overlap detection ───────────────────────────────

def test_partial_das_overlap_is_detected():
    """(5-10) and (8-15) overlap on days 8/9/10."""
    out = find_timeline_conflicts([
        TimelineSpec("t1", "DAS", 5, 10),
        TimelineSpec("t2", "DAS", 8, 15),
    ])
    assert len(out) == 1
    assert out[0].kind == "OVERLAP"
    assert "[8, 10]" in out[0].detail


def test_full_das_enclosure_is_detected_as_overlap():
    """One timeline fully inside another — every day of the inner is
    a duplicate-coverage day. Detected as overlap."""
    out = find_timeline_conflicts([
        TimelineSpec("outer", "DAS", 0, 30),
        TimelineSpec("inner", "DAS", 10, 20),
    ])
    assert len(out) == 1
    assert out[0].kind == "OVERLAP"


def test_identical_das_timelines_are_overlap():
    out = find_timeline_conflicts([
        TimelineSpec("t1", "DAS", 5, 10),
        TimelineSpec("t2", "DAS", 5, 10),
    ])
    assert len(out) == 1
    assert out[0].kind == "OVERLAP"


def test_zero_day_overlap_at_the_boundary_is_overlap_not_gap():
    """(5-10) and (10-15) share day 10 — that's a 1-day overlap, not
    adjacency. Adjacency requires the second to start at end+1."""
    out = find_timeline_conflicts([
        TimelineSpec("t1", "DAS", 5, 10),
        TimelineSpec("t2", "DAS", 10, 15),
    ])
    assert len(out) == 1
    assert out[0].kind == "OVERLAP"


# ── find_timeline_conflicts: CALENDAR is skipped ─────────────────────────────

def test_calendar_timelines_are_skipped_not_reported():
    """CALENDAR has no day-offset anchor, so it can't gap or overlap
    with DAS/DBS timelines. Skipped from the conflict walk; the
    package may still validate as clean if all DAS/DBS timelines
    fit. CALENDAR conflict detection is a separate concern (deferred
    elsewhere too — cca_window_active also defers CALENDAR)."""
    out = find_timeline_conflicts([
        TimelineSpec("das", "DAS", 0, 10),
        TimelineSpec("cal", "CALENDAR", 5, 8),
        TimelineSpec("das2", "DAS", 11, 20),
    ])
    # DAS+DAS are adjacent (no conflict). CALENDAR skipped.
    assert out == []


# ── find_timeline_conflicts: many timelines ──────────────────────────────────

def test_multiple_conflicts_all_reported():
    """Three timelines: two adjacent (clean), then a gap, then an
    overlap — both conflicts surfaced in the output."""
    out = find_timeline_conflicts([
        TimelineSpec("a", "DAS", 0, 10),
        TimelineSpec("b", "DAS", 11, 20),    # adjacent to a
        TimelineSpec("c", "DAS", 25, 30),    # 4-day gap from b
        TimelineSpec("d", "DAS", 28, 40),    # overlaps c on 28..30
    ])
    kinds = sorted(c.kind for c in out)
    assert kinds == ["GAP", "OVERLAP"]
