"""BL-17 — Timeline Date Boundary Rules (pure functions, no DB).

Spec:
- DBS closes at 23:59:59 of (start - to_value).
  DAS opens at 00:00:00 of (start + from_value).
- Consecutive timelines: no gaps, no overlaps — validated at save
  but not hard-blocked.

The day-granularity arithmetic in `snapshot_render.cca_window_active`
already implements the spec's intent for in-window/out-of-window
decisions (a window that closes on day X is in-window for all of
day X). What was missing:

1. Explicit `(opens_at, closes_at)` datetimes with 00:00:00 / 23:59:59
   precision for callers that want time-of-day awareness later (PWA
   countdown timer, scheduled jobs running near midnight).
2. Gap/overlap detection across consecutive timelines. Pre-audit the
   live `_validate_timeline` only checked the SHAPE of one timeline
   (DBS: from > to; DAS/CALENDAR: to > from); nothing compared two
   timelines against each other, so a Package could ship with silent
   coverage gaps or duplicated coverage.

Two helpers:
- `compute_window(...)` — concrete datetime boundaries given a
  specific crop_start. For PWA / response payloads.
- `find_timeline_conflicts(...)` — works on day-offset ranges
  alone. Independent of any crop_start because the gap/overlap
  property is structural — it must hold for every farmer's
  subscription, not just one. Used at Package save time to surface
  warnings to the CA.

CALENDAR timelines are deferred — they have no anchor day-offset
relative to crop_start, so neither helper handles them. This
matches the existing convention in `cca_window_active` /
`cca_calendar_dates` (BL-04).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Optional


# ── Datatypes ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TimelineWindow:
    """Concrete window for a specific crop_start."""
    timeline_id: str
    opens_at: datetime
    closes_at: datetime


@dataclass(frozen=True)
class TimelineSpec:
    """Just the shape of a timeline — no crop_start anchor.
    Sufficient for gap/overlap detection across a Package."""
    timeline_id: str
    from_type: str       # "DAS" | "DBS" | "CALENDAR"
    from_value: int
    to_value: int


@dataclass(frozen=True)
class Conflict:
    timeline_a_id: str
    timeline_b_id: str
    kind: str            # "OVERLAP" | "GAP"
    detail: str


# ── compute_window: concrete dates ───────────────────────────────────────────

_DAY_OPEN = time(0, 0, 0)
_DAY_CLOSE = time(23, 59, 59)


def compute_window(
    from_type: str, from_value: int, to_value: int, crop_start: date,
    *, timeline_id: str = "",
) -> Optional[TimelineWindow]:
    """Return (opens_at, closes_at) for a DAS or DBS timeline given a
    specific crop_start. Returns None for CALENDAR (no anchor).

    DAS: opens_at = (crop_start + from_value) at 00:00:00 UTC,
         closes_at = (crop_start + to_value) at 23:59:59 UTC.
    DBS: opens_at = (crop_start - from_value) at 00:00:00 UTC,
         closes_at = (crop_start - to_value) at 23:59:59 UTC.

    Production DBS convention is from > to (e.g. from=15, to=8 means
    "active 15 → 8 days before sowing"). With that convention,
    `crop_start - from_value < crop_start - to_value`, so opens_at is
    correctly before closes_at.

    UTC timezone is used everywhere — same convention as the rest of
    the codebase (BL-09 alerts day-boundary fix, etc.).
    """
    from datetime import timedelta
    if from_type == "DAS":
        open_date = crop_start + timedelta(days=from_value)
        close_date = crop_start + timedelta(days=to_value)
    elif from_type == "DBS":
        open_date = crop_start - timedelta(days=from_value)
        close_date = crop_start - timedelta(days=to_value)
    else:
        return None
    return TimelineWindow(
        timeline_id=timeline_id,
        opens_at=datetime.combine(open_date, _DAY_OPEN, tzinfo=timezone.utc),
        closes_at=datetime.combine(close_date, _DAY_CLOSE, tzinfo=timezone.utc),
    )


# ── to_day_offset_range: structural, crop-start-independent ──────────────────

def to_day_offset_range(
    from_type: str, from_value: int, to_value: int,
) -> Optional[tuple[int, int]]:
    """Convert a timeline's (from, to) into a (start, end) day-offset
    range relative to crop_start. Returns None for CALENDAR.

    DAS: returns (from_value, to_value) — positive offsets, increasing.
    DBS: returns (-from_value, -to_value) — negative offsets. Production
         convention from > to means -from < -to, so the tuple is still
         (smaller, larger) and stays comparable across timeline types
         on the same numeric line.

    Used by `find_timeline_conflicts` because gap/overlap is a
    structural property of the timeline configuration — it must hold
    for every farmer's crop_start, so we don't need a specific
    crop_start to detect it.
    """
    if from_type == "DAS":
        return (from_value, to_value)
    if from_type == "DBS":
        return (-from_value, -to_value)
    return None


# ── find_timeline_conflicts ──────────────────────────────────────────────────

def find_timeline_conflicts(timelines: list[TimelineSpec]) -> list[Conflict]:
    """Detect GAP and OVERLAP conflicts across a Package's timelines.

    Treats each (DAS, DBS) timeline as a closed integer interval on
    the day-offset number line (DAS: positive, DBS: negative).
    CALENDAR timelines are skipped — no anchor.

    Walks the timelines in order of opens_at (start-offset ascending)
    and compares each adjacent pair. Two timelines are:
    - OVERLAP if the second starts at or before the first ends:
      `b_start <= a_end`. Captures both partial overlap (b_start ==
      a_end) and full enclosure.
    - GAP if the second starts more than one day after the first ends:
      `b_start > a_end + 1`. Adjacent (`b_start == a_end + 1`) is
      considered "no gap" — the spec wants no day uncovered, not
      day-fractions.
    - Otherwise no conflict.

    Returns a list of `Conflict` records — empty if the Package's
    timelines are clean. Used as soft validation at Package save
    time: the CA sees warnings but isn't hard-blocked from saving.
    """
    rangeable: list[tuple[TimelineSpec, tuple[int, int]]] = []
    for spec in timelines:
        rng = to_day_offset_range(spec.from_type, spec.from_value, spec.to_value)
        if rng is None:
            continue
        rangeable.append((spec, rng))

    rangeable.sort(key=lambda pair: pair[1][0])

    conflicts: list[Conflict] = []
    for i in range(len(rangeable) - 1):
        a_spec, (a_start, a_end) = rangeable[i]
        b_spec, (b_start, b_end) = rangeable[i + 1]
        if b_start <= a_end:
            conflicts.append(Conflict(
                timeline_a_id=a_spec.timeline_id,
                timeline_b_id=b_spec.timeline_id,
                kind="OVERLAP",
                detail=(
                    f"timelines overlap on day-offsets "
                    f"[{b_start}, {min(a_end, b_end)}]"
                ),
            ))
        elif b_start > a_end + 1:
            gap_days = b_start - a_end - 1
            conflicts.append(Conflict(
                timeline_a_id=a_spec.timeline_id,
                timeline_b_id=b_spec.timeline_id,
                kind="GAP",
                detail=(
                    f"{gap_days}-day gap between day-offset {a_end} "
                    f"and day-offset {b_start}"
                ),
            ))
    return conflicts
