"""Snapshot render helpers — Phase 3.1.

Source-agnostic builders that turn a deserialised snapshot content dict
(from `app.services.snapshot.deserialise_timeline`) into the data shapes
the today-render path consumes (PracticeStub list, BL-02 inputs, calendar
dates).

The today route uses these helpers so the same downstream pipeline runs
regardless of whether content came from a frozen snapshot or a fresh
master-table serialisation.

See: per_subscription_versioning.md (Phase 3 — read path integration).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.bl02_conditional import (
    ConditionalQuestion as CQ,
    PracticeConditionalLink as PCL,
    filter_practices_by_conditionals,
)
from app.services.bl03_deduplication import (
    PracticeElement as PEl,
    PracticeStub as PStub,
)
from app.services.snapshot import (
    deserialise_timeline,
    get_snapshot,
    serialise_cha_timeline,
    serialise_timeline,
    take_snapshot,
)

logger = logging.getLogger(__name__)


# ── Timeline window metadata ────────────────────────────────────────────────

@dataclass
class TimelineMetadata:
    """The four fields BL-04 + calendar-date computation need for one timeline."""
    from_type: str       # "DAS" | "DBS" | "CALENDAR"
    from_value: int
    to_value: int


def metadata_from_content(content: dict) -> TimelineMetadata:
    """Read window metadata out of a snapshot's deserialised content dict."""
    tl = content.get("timeline") or {}
    return TimelineMetadata(
        from_type=str(tl.get("from_type") or ""),
        from_value=int(tl.get("from_value") or 0),
        to_value=int(tl.get("to_value") or 0),
    )


def metadata_from_master_cca(tl) -> TimelineMetadata:
    """Read window metadata off a master CCA Timeline row."""
    from_type = tl.from_type.value if hasattr(tl.from_type, "value") else str(tl.from_type)
    return TimelineMetadata(
        from_type=from_type,
        from_value=int(tl.from_value),
        to_value=int(tl.to_value),
    )


def cca_window_active(
    meta: TimelineMetadata, day_offset: int,
    today_date: Optional[date] = None,
) -> bool:
    """Mirrors the BL-04 check in `/farmer/advisory/today` for CCA timelines.

    DAS: from_value <= day_offset <= to_value (positive offsets, from < to).
    DBS: -from_value <= day_offset <= -max(to_value, 1).
         Production convention has `from_value > to_value` for DBS rows
         (e.g. from=15, to=8 means "active 15 to 8 days before sowing").
         day_offset is negative pre-sowing.
         BL-17 boundary rule (2026-06-02): DBS closes at midnight of
         (crop_start - 1) — i.e. DBS never covers crop_start itself even
         when to_value == 0. Without `max(to_value, 1)`, to=0 would let
         day_offset=0 pass through and overlap a DAS 0→N timeline on
         the sowing day. Fixed across all four call sites
         (cca_window_active here + in snapshot_sweep,
         cca_calendar_dates below, bl17.compute_window, bl17.to_day_offset_range).
    CALENDAR (Alerts D, 2026-05-29): PERENNIAL packages anchor to the
         calendar year — `from_value` / `to_value` are day-of-year
         bounds (1-365/366). Active when today's day-of-year falls in
         [from_value, to_value]. Requires `today_date`; callers
         without a calendar context (e.g. snapshot resolution) get
         False, matching the pre-2026-05-29 defer-CALENDAR behaviour
         so the snapshot reads continue to no-op CALENDAR rows.
    Unknown: False.
    """
    if meta.from_type == "DAS":
        return meta.from_value <= day_offset <= meta.to_value
    if meta.from_type == "DBS":
        return -meta.from_value <= day_offset <= -max(meta.to_value, 1)
    if meta.from_type == "CALENDAR":
        if today_date is None:
            return False
        doy = today_date.timetuple().tm_yday
        return meta.from_value <= doy <= meta.to_value
    return False


def cca_calendar_dates(
    meta: TimelineMetadata, crop_start: date,
    today: Optional[date] = None,
) -> tuple[date, date]:
    """Compute (from_date, to_date) used by BL-03 + the response payload.

    CALENDAR (Perennial): `from_value` / `to_value` are day-of-year
    (1..365/366). Window resolves to `today.year`. Bug fix
    2026-06-18 — this branch previously fell through to
    `(crop_start, crop_start)`, which made the date range shown on
    the farmer's advisory card collapse to a single day at the
    sowing date instead of the actual calendar window the timeline
    covers; BL-03 dedup also lost its overlap signal because every
    CALENDAR timeline reported the same one-day window. Now mirrors
    the equivalent `_timeline_window` logic in
    `app/services/order_bundle.py`.
    """
    if meta.from_type == "DAS":
        return (
            crop_start + timedelta(days=meta.from_value),
            crop_start + timedelta(days=meta.to_value),
        )
    if meta.from_type == "DBS":
        # BL-17 boundary rule: DBS closes the day BEFORE crop_start
        # when to_value == 0. Otherwise closes to_value days before.
        return (
            crop_start - timedelta(days=meta.from_value),
            crop_start - timedelta(days=max(meta.to_value, 1)),
        )
    if meta.from_type == "CALENDAR":
        if today is None:
            today = date.today()
        year_start = date(today.year, 1, 1)
        return (
            year_start + timedelta(days=int(meta.from_value) - 1),
            year_start + timedelta(days=int(meta.to_value) - 1),
        )
    return (crop_start, crop_start)


def cha_calendar_dates(
    meta: TimelineMetadata, triggered_at: date,
) -> tuple[date, date]:
    """CHA windows anchor to triggered_at (Rule 3, second clause)."""
    return (
        triggered_at + timedelta(days=meta.from_value),
        triggered_at + timedelta(days=meta.to_value),
    )


# ── CCA render output ────────────────────────────────────────────────────────

@dataclass
class RenderedCCATimeline:
    practice_stubs: list[PStub]
    pending_question: Optional[dict]   # {question_id, question_text, display_order}
    blank_paths: list[dict]            # [{question_id, question_text, farmer_answer}]


def render_cca_from_content(
    content: dict, today_answers: dict[str, str],
) -> RenderedCCATimeline:
    """Apply BL-02 filtering against snapshot content + build PStubs."""
    practices = content.get("practices") or []
    all_practice_ids = [p["id"] for p in practices if "id" in p]

    questions = [
        CQ(q["id"], q.get("question_text", ""), int(q.get("display_order", 0)))
        for q in (content.get("conditional_questions") or [])
        if "id" in q
    ]
    practice_links = [
        PCL(l["practice_id"], l["question_id"], str(l.get("answer", "")))
        for l in (content.get("conditional_links") or [])
        if l.get("practice_id") and l.get("question_id")
    ]

    bl02_result = filter_practices_by_conditionals(
        all_practice_ids=all_practice_ids,
        questions=questions,
        practice_links=practice_links,
        today_answers=today_answers,
    )

    pending_question = None
    if not bl02_result.all_questions_answered and bl02_result.pending_question:
        pending_question = {
            "question_id": bl02_result.pending_question.id,
            "question_text": bl02_result.pending_question.question_text,
            "display_order": bl02_result.pending_question.display_order,
        }

    blank_paths: list[dict] = []
    if bl02_result.blank_path_questions:
        cq_text_map = {
            q["id"]: q.get("question_text", "")
            for q in (content.get("conditional_questions") or [])
            if "id" in q
        }
        for qid in bl02_result.blank_path_questions:
            if qid in cq_text_map and qid in today_answers:
                blank_paths.append({
                    "question_id": qid,
                    "question_text": cq_text_map[qid],
                    "farmer_answer": today_answers[qid],
                })

    visible_ids = set(bl02_result.visible_practices)
    visible_practices = [p for p in practices if p.get("id") in visible_ids]

    rel_type_map = {
        r["id"]: r.get("relation_type")
        for r in (content.get("relations") or [])
        if "id" in r
    }

    practice_stubs = [
        PStub(
            id=p["id"],
            l0_type=str(p.get("l0_type", "INPUT")),
            l1_type=p.get("l1_type"),
            l2_type=p.get("l2_type"),
            display_order=int(p.get("display_order", 0)),
            is_special_input=bool(p.get("is_special_input", False)),
            relation_id=p.get("relation_id"),
            relation_role=p.get("relation_role"),
            relation_type=(
                rel_type_map.get(p["relation_id"])
                if p.get("relation_id") else None
            ),
            elements=[
                PEl(
                    element_type=str(e.get("element_type", "")),
                    cosh_ref=e.get("cosh_ref"),
                    value=e.get("value"),
                    unit_cosh_id=e.get("unit_cosh_id"),
                )
                for e in (p.get("elements") or [])
            ],
            frequency_days=p.get("frequency_days"),
        )
        for p in visible_practices
    ]

    return RenderedCCATimeline(
        practice_stubs=practice_stubs,
        pending_question=pending_question,
        blank_paths=blank_paths,
    )


def render_cha_from_content(content: dict) -> list[PStub]:
    """CHA practices have no relations / conditional questions in the schema —
    just convert each practice + its elements directly into a PStub."""
    practices = content.get("practices") or []
    return [
        PStub(
            id=p["id"],
            l0_type=str(p.get("l0_type", "INPUT")),
            l1_type=p.get("l1_type"),
            l2_type=p.get("l2_type"),
            display_order=int(p.get("display_order", 0)),
            is_special_input=bool(p.get("is_special_input", False)),
            relation_id=None,
            elements=[
                PEl(
                    element_type=str(e.get("element_type", "")),
                    cosh_ref=e.get("cosh_ref"),
                    value=e.get("value"),
                    unit_cosh_id=e.get("unit_cosh_id"),
                )
                for e in (p.get("elements") or [])
            ],
            frequency_days=p.get("frequency_days"),
        )
        for p in practices
    ]


# ── Snapshot-or-master content resolution ───────────────────────────────────

async def resolve_cca_content(
    db: AsyncSession, subscription_id: str, timeline_id: str,
    lock_trigger: str = "VIEWED",
) -> tuple[dict, bool]:
    """Return (deserialised_content, locked) for a CCA timeline.

    locked=True   → content came from an existing or just-taken snapshot
                    (the farmer's frozen reality).
    locked=False  → snapshot capture failed; we are returning a fresh master
                    serialisation as a degraded fallback. The defensive
                    sweep retries on its next run.

    `lock_trigger` labels the snapshot when one is taken inline. Default
    "VIEWED" — caller is rendering an in-window timeline. Pass
    "PURCHASE_ORDER" when the caller is the BL-03 context-only path
    (timeline out of window but referenced by approved order items)
    so the snapshot's audit trail reflects the true reason for capture.
    Trigger labels are observability-only; render behaviour does not
    depend on them.
    """
    snap = await get_snapshot(db, subscription_id, timeline_id, "CCA")
    if snap is not None:
        return deserialise_timeline(snap.content), True

    try:
        snap = await take_snapshot(
            db, subscription_id, timeline_id, lock_trigger, source="CCA",
        )
        return deserialise_timeline(snap.content), True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "snapshot capture failed (CCA) sub=%s tl=%s: %s — falling back to master",
            subscription_id, timeline_id, exc,
        )
        try:
            return deserialise_timeline(await serialise_timeline(db, timeline_id)), False
        except Exception as exc2:  # noqa: BLE001
            logger.error(
                "master serialisation also failed sub=%s tl=%s: %s",
                subscription_id, timeline_id, exc2,
            )
            return _empty_content(timeline_id, "CCA"), False


async def resolve_cha_content(
    db: AsyncSession, subscription_id: str, timeline_id: str, source: str,
    lock_trigger: str = "VIEWED",
) -> tuple[dict, bool]:
    """CHA equivalent of resolve_cca_content. `source` must be 'PG' or 'SP'.

    `lock_trigger` semantics match resolve_cca_content — label only.
    """
    snap = await get_snapshot(db, subscription_id, timeline_id, source)
    if snap is not None:
        return deserialise_timeline(snap.content), True

    try:
        snap = await take_snapshot(
            db, subscription_id, timeline_id, lock_trigger, source=source,
        )
        return deserialise_timeline(snap.content), True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "snapshot capture failed (%s) sub=%s tl=%s: %s — falling back to master",
            source, subscription_id, timeline_id, exc,
        )
        try:
            return (
                deserialise_timeline(await serialise_cha_timeline(db, timeline_id, source)),
                False,
            )
        except Exception as exc2:  # noqa: BLE001
            logger.error(
                "master serialisation also failed sub=%s tl=%s src=%s: %s",
                subscription_id, timeline_id, source, exc2,
            )
            return _empty_content(timeline_id, source), False


def _empty_content(timeline_id: str, source: str) -> dict:
    """Last-resort empty content shape so the renderer never crashes."""
    return {
        "schema_version": 1, "source": source,
        "timeline": {
            "id": timeline_id, "from_type": "DAS",
            "from_value": 0, "to_value": 0,
        },
        "practices": [], "relations": [],
        "conditional_questions": [], "conditional_links": [],
        "practices_by_id": {}, "relations_by_id": {},
        "questions_by_id": {}, "links_by_practice": {},
    }
