"""Phase 2 — snapshot_sweep window-check tests.

Pure-function coverage for the BL-04-mirror helpers extracted from the
defensive sweep. The full _run() requires DB and is exercised manually
against staging until Phase 3 adds DB fixtures.
"""
from __future__ import annotations

from datetime import date

from app.tasks.snapshot_sweep import cca_window_active, cha_window_active


# ── cca_window_active (DAS) ─────────────────────────────────────────────────

def test_cca_das_inside():
    # day 5 of a DAS 0..30 window
    assert cca_window_active("DAS", 0, 30, 5) is True


def test_cca_das_inclusive_edges():
    assert cca_window_active("DAS", 10, 20, 10) is True
    assert cca_window_active("DAS", 10, 20, 20) is True


def test_cca_das_outside():
    assert cca_window_active("DAS", 10, 20, 9) is False
    assert cca_window_active("DAS", 10, 20, 21) is False


# ── cca_window_active (DBS) ─────────────────────────────────────────────────
# Convention (per today-route BL-04): for DBS rows, from_value is the smaller
# "days before sowing" (latest day) and to_value is the larger (earliest day).
# Window is -to_value <= day_offset <= -from_value.

def test_cca_dbs_pre_sowing():
    # DBS 7..30: active 7 to 30 days before sowing.
    # day_offset = -10 (10 days before sowing) → inside.
    assert cca_window_active("DBS", 7, 30, -10) is True


def test_cca_dbs_inclusive_edges():
    # day_offset = -7 (latest day, 7 days before sowing) — inside.
    assert cca_window_active("DBS", 7, 30, -7) is True
    # day_offset = -30 (earliest day) — inside.
    assert cca_window_active("DBS", 7, 30, -30) is True


def test_cca_dbs_outside():
    # 6 before sowing — too late.
    assert cca_window_active("DBS", 7, 30, -6) is False
    # 31 before sowing — too early.
    assert cca_window_active("DBS", 7, 30, -31) is False
    # 0 = sowing day — well past the DBS window.
    assert cca_window_active("DBS", 7, 30, 0) is False


# ── cca_window_active (CALENDAR / unknown) ──────────────────────────────────

def test_cca_calendar_returns_false():
    """CALENDAR currently not handled — sweep skips like the today route does."""
    assert cca_window_active("CALENDAR", 0, 30, 5) is False
    assert cca_window_active("UNKNOWN_TYPE", 0, 30, 5) is False


# ── cha_window_active ───────────────────────────────────────────────────────

def test_cha_inside():
    triggered = date(2026, 5, 1)
    today = date(2026, 5, 5)
    # offsets 0..7: window is 5/1..5/8 inclusive
    assert cha_window_active(triggered, 0, 7, today) is True


def test_cha_inclusive_edges():
    triggered = date(2026, 5, 1)
    assert cha_window_active(triggered, 0, 7, date(2026, 5, 1)) is True
    assert cha_window_active(triggered, 0, 7, date(2026, 5, 8)) is True


def test_cha_outside():
    triggered = date(2026, 5, 1)
    assert cha_window_active(triggered, 0, 7, date(2026, 4, 30)) is False
    assert cha_window_active(triggered, 0, 7, date(2026, 5, 9)) is False
