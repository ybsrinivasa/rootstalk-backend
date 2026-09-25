"""BL-17 — Timeline Date Boundary Rules (pure functions, no DB).

Spec:
- DBS closes at 23:59:59 of (start - max(to_value, 1)) — i.e. DBS
  never covers crop_start itself, even when to_value == 0.
  DAS opens at 00:00:00 of (start + from_value).
- Consecutive timelines: no gaps, no overlaps — validated at save
  but not hard-blocked.

The `max(to_value, 1)` clamp on DBS upper-bound is the
2026-06-02 fix: pre-fix the math used `start - to_value` directly,
which meant a DBS 10→0 timeline ran up to and INCLUDED the sowing
day, overlapping a DAS 0→8 timeline on day 0. Behaviour for
`to_value >= 1` is unchanged. Same clamp is applied symmetrically in
`snapshot_render.cca_window_active`, `snapshot_render.cca_calendar_dates`,
and `snapshot_sweep.cca_window_active` so the four call sites agree.

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

    2026-09-25 — half-open convention. `to_value` is the FIRST day
    PAST the window (exclusive). `close_date` computed here is the
    LAST INCLUSIVE day, i.e. `to_value - 1`, so `closes_at` stays
    at 23:59:59 of that day (consumers of `closes_at` — PWA
    countdowns, scheduled jobs — expect an inclusive last-second).

    DAS: opens_at = (crop_start + from_value) at 00:00:00 UTC,
         closes_at = (crop_start + to_value - 1) at 23:59:59 UTC.
    DBS: opens_at = (crop_start - from_value) at 00:00:00 UTC,
         closes_at = (crop_start - to_value - 1) at 23:59:59 UTC.
         Old `max(to_value, 1)` clamp dropped — with exclusive
         semantics, to_value=0 gives close_date = crop_start - 1
         (day 1 pre-sowing), which naturally never covers the sowing
         day. Clean.

    Production DBS convention is from > to (e.g. from=15, to=8 means
    "active days 15 down to 9 pre-sowing" under the new semantics).

    UTC timezone is used everywhere — same convention as the rest of
    the codebase (BL-09 alerts day-boundary fix, etc.).
    """
    from datetime import timedelta
    if from_type == "DAS":
        open_date = crop_start + timedelta(days=from_value)
        close_date = crop_start + timedelta(days=to_value - 1)
    elif from_type == "DBS":
        open_date = crop_start - timedelta(days=from_value)
        close_date = crop_start - timedelta(days=to_value + 1)
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
    """Convert a timeline's (from, to) into a (start, end_exclusive)
    day-offset range relative to crop_start. Returns None for CALENDAR.

    2026-09-25 — half-open convention. The returned `end` is the
    FIRST offset PAST the window (exclusive). `find_timeline_conflicts`
    uses this with strict `<` on the overlap check.

    DAS: returns (from_value, to_value) — positive offsets, increasing.
         Under new semantics, `to_value` is exclusive.
    DBS: returns (-from_value, -to_value) — negative offsets.
         Old `max(to_value, 1)` clamp dropped: with exclusive
         semantics, to_value=0 gives end = 0 (crop_start), so DBS
         naturally never overlaps DAS on the sowing day.

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

    2026-09-25 — half-open convention. `to_day_offset_range` returns
    (start, end_exclusive). Two timelines:
    - OVERLAP if the second STARTS STRICTLY BEFORE the first's
      exclusive end: `b_start < a_end`. Adjacent-touching timelines
      (b_start == a_end) share only the boundary point — no
      inclusive day is shared, so NOT an overlap.
    - GAP if the second starts strictly after the first ends:
      `b_start > a_end`. Gap days = `b_start - a_end`.
    - Otherwise (b_start == a_end): adjacent, no conflict.

    CALENDAR timelines are skipped — no anchor.

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
        if b_start < a_end:
            conflicts.append(Conflict(
                timeline_a_id=a_spec.timeline_id,
                timeline_b_id=b_spec.timeline_id,
                kind="OVERLAP",
                detail=(
                    # Report last-inclusive endpoints (a_end - 1,
                    # min(a_end, b_end) - 1) to match the SE's
                    # mental model of the range they authored.
                    f"timelines overlap on day-offsets "
                    f"[{b_start}, {min(a_end, b_end) - 1}]"
                ),
            ))
        elif b_start > a_end:
            gap_days = b_start - a_end
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
