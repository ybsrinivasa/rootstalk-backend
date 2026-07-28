"""
Per-Subscription Content Versioning — Snapshot library.

This module serialises timeline content (Practices + Elements + Relations + Conditionals)
into immutable JSONB snapshots stored in `locked_timeline_snapshots`. Once a timeline
is locked for a subscription, the snapshot is the source of truth for that
(subscription, timeline, source) triple forever — even if the SE later modifies the
master tables.

Per spec rules confirmed 2026-05-03:
  - Lock stays forever — no release on order completion or cancellation (Rules 1 & 2)
  - Snapshot dates stored relative to crop_start (CCA) or triggered_at (CHA),
    NOT absolute (Rule 3 — the renderer applies the offset at read time)
  - SE is unaware of snapshots — they edit master tables freely (Rule 4)
  - Dealer also reads from snapshot for orders (Rule 5)

Phase 1 scope: library only. NO callers wired. Nothing in the existing codebase
imports or invokes any function below. That is intentional. Phase 2 will wire
order placement and the today-render path.

See:
  /Users/ybsrinivasa/.claude/projects/-Users-ybsrinivasa-cosh-backend/memory/per_subscription_versioning.md
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import (
    Timeline, Practice, Element, Relation,
    ConditionalQuestion, PracticeConditional, RelationConditional,
)
from app.modules.subscriptions.snapshot_models import LockedTimelineSnapshot


# ── Constants ────────────────────────────────────────────────────────────────

VALID_LOCK_TRIGGERS = {"PURCHASE_ORDER", "VIEWED", "BACKFILL"}
VALID_SOURCES = {"CCA", "PG", "SP"}
SCHEMA_VERSION = 1


# ── Helpers ──────────────────────────────────────────────────────────────────

def _enum_value(v):
    """Return the enum's `.value` if it is an enum, else str(v) (or None)."""
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)


def _int_or(v, default=0):
    if v is None:
        return default
    return int(v)


def _int_or_none(v):
    if v is None:
        return None
    return int(v)


# ── Serialisation: CCA Timeline ──────────────────────────────────────────────

async def serialise_timeline(db: AsyncSession, timeline_id: str) -> dict:
    """Serialise a CCA Timeline + all its children into a JSON-safe dict.

    Captures: timeline metadata, all practices, all elements per practice,
    all referenced relations, all conditional questions on the timeline, and
    all practice-conditional links. Dates are stored as `from_value`/`to_value`
    (DBS/DAS/CALENDAR offsets), not absolute — per Rule 3.

    Raises ValueError if the timeline is missing.
    """
    tl = (
        await db.execute(select(Timeline).where(Timeline.id == timeline_id))
    ).scalar_one_or_none()
    if tl is None:
        raise ValueError(f"Timeline not found: {timeline_id}")

    practices = (
        await db.execute(
            select(Practice)
            .where(Practice.timeline_id == timeline_id)
            .order_by(Practice.display_order)
        )
    ).scalars().all()

    practice_ids = [p.id for p in practices]

    elements = []
    if practice_ids:
        elements = (
            await db.execute(
                select(Element)
                .where(Element.practice_id.in_(practice_ids))
                .order_by(Element.display_order)
            )
        ).scalars().all()
    elements_by_practice: dict[str, list[Element]] = {}
    for e in elements:
        elements_by_practice.setdefault(e.practice_id, []).append(e)

    relation_ids = list({p.relation_id for p in practices if p.relation_id})
    relations = []
    if relation_ids:
        relations = (
            await db.execute(
                select(Relation).where(Relation.id.in_(relation_ids))
            )
        ).scalars().all()

    conditional_qs = (
        await db.execute(
            select(ConditionalQuestion)
            .where(ConditionalQuestion.timeline_id == timeline_id)
            .order_by(ConditionalQuestion.display_order)
        )
    ).scalars().all()

    conditional_links = []
    if practice_ids:
        conditional_links = (
            await db.execute(
                select(PracticeConditional).where(
                    PracticeConditional.practice_id.in_(practice_ids)
                )
            )
        ).scalars().all()

    # 2026-06-26 — Capture RelationConditional bindings (BL-19 §6.4).
    # When the SE binds an entire Relation to a CQ answer (rather than
    # binding each member Practice individually via PracticeConditional),
    # this is where the binding lives. The renderer expands each row
    # into per-member virtual PracticeConditional links so the existing
    # BL-02 filter gates the whole Relation uniformly.
    relation_conditional_links = []
    if relation_ids:
        relation_conditional_links = (
            await db.execute(
                select(RelationConditional).where(
                    RelationConditional.relation_id.in_(relation_ids)
                )
            )
        ).scalars().all()

    return {
        "schema_version": SCHEMA_VERSION,
        "source": "CCA",
        "timeline": {
            "id": tl.id,
            "package_id": tl.package_id,
            "name": tl.name,
            "from_type": _enum_value(tl.from_type),
            "from_value": _int_or(tl.from_value),
            "to_value": _int_or(tl.to_value),
            "display_order": _int_or(tl.display_order),
        },
        "practices": [
            {
                "id": p.id,
                "l0_type": _enum_value(p.l0_type),
                "l1_type": p.l1_type,
                "l2_type": p.l2_type,
                "display_order": _int_or(p.display_order),
                "relation_id": p.relation_id,
                "relation_role": p.relation_role,
                "is_special_input": bool(p.is_special_input),
                "common_name_cosh_id": p.common_name_cosh_id,
                "frequency_days": _int_or_none(p.frequency_days),
                # 2026-07-28 — capture brand-lock intent for
                # historically-honest reporting. See
                # reference_brand_authoring_states memory + the
                # orders_brand_mix report. Snapshots written before
                # this line are missing the field; the reader
                # tolerates that (falls back to live Practice).
                "is_brand_locked": bool(getattr(p, "is_brand_locked", False)),
                "elements": [
                    {
                        "id": e.id,
                        "element_type": e.element_type,
                        "cosh_ref": e.cosh_ref,
                        "value": e.value,
                        "unit_cosh_id": e.unit_cosh_id,
                        "display_order": _int_or(e.display_order),
                    }
                    for e in elements_by_practice.get(p.id, [])
                ],
            }
            for p in practices
        ],
        "relations": [
            {
                "id": r.id,
                "relation_type": _enum_value(r.relation_type),
                "expression": r.expression,
            }
            for r in relations
        ],
        "conditional_questions": [
            {
                "id": q.id,
                "question_text": q.question_text,
                "display_order": _int_or(q.display_order),
            }
            for q in conditional_qs
        ],
        "conditional_links": [
            {
                "practice_id": pc.practice_id,
                "question_id": pc.question_id,
                "answer": _enum_value(pc.answer),
            }
            for pc in conditional_links
        ],
        # 2026-06-26 — Per-Relation CQ bindings (BL-19 §6.4 + audit 3).
        "relation_conditional_links": [
            {
                "relation_id": rc.relation_id,
                "question_id": rc.question_id,
                "answer": _enum_value(rc.answer),
            }
            for rc in relation_conditional_links
        ],
    }


# ── Serialisation: CHA (PG / SP) Timeline ────────────────────────────────────

async def serialise_cha_timeline(
    db: AsyncSession, timeline_id: str, source: str
) -> dict:
    """Serialise a CHA / Q&A timeline + its children. `source` must be
    'PG', 'SP', or 'QA'.

    CHA / QA snapshot dates anchor to triggered_at (stored on the
    TriggeredCHAEntry, not in the snapshot) and do NOT shift with
    crop_start (Rule 3, second clause).

    'QA' uses the same physical tables as 'PG' (UCAT pipe-3 reuse, see
    project_rootstalk_ucat.md). The string remains 'QA' so the
    LockedTimelineSnapshot's `source` discriminator distinguishes
    Q&A-rooted from PG-rooted snapshots even though they're stored
    in the same tables.

    2026-06-26 (Phase 3 audit 1): CHA / QA timelines DO use Practice
    Relations — the authoring side mounts `<RelationsSection>` on the
    CHA editor pages. Earlier this serialiser stripped relation_id /
    relation_role and shipped an empty relations array, so any
    authored Relation on a CHA / QA timeline was invisible to the
    farmer at render time (BL-19 violation). Now mirrored against the
    CCA path: capture per-practice role + serialise the Relation rows
    themselves. PracticeConditional bindings are still excluded for
    CHA / QA — those pipes don't surface in-flow conditional questions
    (CCA-only).

    Raises ValueError on bad source or missing timeline.
    """
    # Batch 39O UCAT unification (2026-05-16): PG / SP / QA all live in
    # the shared Timeline / Practice / Element tables. The `source`
    # discriminator no longer dispatches to different models — it's
    # kept on the `LockedTimelineSnapshot` row purely to label the
    # snapshot origin for downstream renderers.
    if source not in ("PG", "SP", "QA"):
        raise ValueError(f"source must be 'PG', 'SP', or 'QA', got {source!r}")
    TLModel, PracticeModel, ElementModel = Timeline, Practice, Element

    tl = (
        await db.execute(select(TLModel).where(TLModel.id == timeline_id))
    ).scalar_one_or_none()
    if tl is None:
        raise ValueError(f"{source}Timeline not found: {timeline_id}")

    practices = (
        await db.execute(
            select(PracticeModel)
            .where(PracticeModel.timeline_id == timeline_id)
            .order_by(PracticeModel.display_order)
        )
    ).scalars().all()

    practice_ids = [p.id for p in practices]

    elements = []
    if practice_ids:
        elements = (
            await db.execute(
                select(ElementModel)
                .where(ElementModel.practice_id.in_(practice_ids))
                .order_by(ElementModel.display_order)
            )
        ).scalars().all()
    elements_by_practice: dict[str, list] = {}
    for e in elements:
        elements_by_practice.setdefault(e.practice_id, []).append(e)

    # 2026-06-26 — Capture Practice Relations on CHA / QA timelines.
    # Mirrors `serialise_timeline` (CCA). PracticeConditional links are
    # intentionally NOT captured here — CHA / QA pipes don't run the
    # in-flow conditional-question flow.
    relation_ids = list({p.relation_id for p in practices if p.relation_id})
    relations: list[Relation] = []
    if relation_ids:
        relations = (
            await db.execute(
                select(Relation).where(Relation.id.in_(relation_ids))
            )
        ).scalars().all()

    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,  # "PG" or "SP"
        "timeline": {
            "id": tl.id,
            "name": getattr(tl, "name", None),
            "from_type": _enum_value(getattr(tl, "from_type", None)),
            "from_value": _int_or(getattr(tl, "from_value", 0)),
            "to_value": _int_or(getattr(tl, "to_value", 0)),
            "display_order": _int_or(getattr(tl, "display_order", 0)),
        },
        "practices": [
            {
                "id": p.id,
                "l0_type": _enum_value(p.l0_type),
                "l1_type": p.l1_type,
                "l2_type": p.l2_type,
                "display_order": _int_or(p.display_order),
                "is_special_input": bool(getattr(p, "is_special_input", False)),
                "frequency_days": _int_or_none(getattr(p, "frequency_days", None)),
                # 2026-07-28 — capture brand-lock intent (see the
                # matching field in serialise_timeline above).
                "is_brand_locked": bool(getattr(p, "is_brand_locked", False)),
                # 2026-06-26 — relation membership preserved
                "relation_id": p.relation_id,
                "relation_role": p.relation_role,
                "elements": [
                    {
                        "id": e.id,
                        "element_type": e.element_type,
                        "cosh_ref": e.cosh_ref,
                        "value": e.value,
                        "unit_cosh_id": e.unit_cosh_id,
                        "display_order": _int_or(getattr(e, "display_order", 0)),
                    }
                    for e in elements_by_practice.get(p.id, [])
                ],
            }
            for p in practices
        ],
        # 2026-06-26 — Relations on CHA / QA timelines are now serialised.
        # Conditional questions stay empty: CHA / QA pipes don't surface
        # in-flow CQs (CCA-only).
        "relations": [
            {
                "id": r.id,
                "relation_type": _enum_value(r.relation_type),
                "expression": r.expression,
            }
            for r in relations
        ],
        "conditional_questions": [],
        "conditional_links": [],
    }


# ── Snapshot CRUD ────────────────────────────────────────────────────────────

async def take_snapshot(
    db: AsyncSession,
    subscription_id: str,
    timeline_id: str,
    lock_trigger: str,
    source: str = "CCA",
) -> LockedTimelineSnapshot:
    """Serialise + store. If a snapshot for (sub, lineage_id, source)
    already exists, return it without overwriting — snapshots are
    immutable per Rules 1 & 2.

    `timeline_id` is the physical Timeline row whose content we
    serialise; the snapshot's uniqueness key is the Timeline's
    `lineage_id` (added 2026-06-19) so the snapshot survives package
    republishes that clone the Timeline row with a new id.

    Raises ValueError on invalid lock_trigger or source.
    """
    if lock_trigger not in VALID_LOCK_TRIGGERS:
        raise ValueError(
            f"lock_trigger must be one of {sorted(VALID_LOCK_TRIGGERS)}, "
            f"got {lock_trigger!r}"
        )
    if source not in VALID_SOURCES:
        raise ValueError(
            f"source must be one of {sorted(VALID_SOURCES)}, got {source!r}"
        )

    # Look up the timeline's lineage_id once. The render-path callers
    # already hold a Timeline row; we accept timeline_id for ergonomic
    # parity with the pre-2026-06-19 signature and resolve lineage_id
    # here.
    from app.modules.advisory.models import Timeline as _TL
    tl_row = (await db.execute(
        select(_TL.lineage_id).where(_TL.id == timeline_id)
    )).scalar_one_or_none()
    if tl_row is None:
        raise ValueError(f"Timeline not found: {timeline_id}")
    lineage_id = tl_row

    existing = await get_snapshot_by_lineage(
        db, subscription_id, lineage_id, source,
    )
    if existing is not None:
        return existing

    if source == "CCA":
        content = await serialise_timeline(db, timeline_id)
    else:
        content = await serialise_cha_timeline(db, timeline_id, source)

    snapshot = LockedTimelineSnapshot(
        subscription_id=subscription_id,
        timeline_id=timeline_id,
        lineage_id=lineage_id,
        source=source,
        content=content,
        locked_at=datetime.now(timezone.utc),
        lock_trigger=lock_trigger,
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


async def get_snapshot(
    db: AsyncSession,
    subscription_id: str,
    timeline_id: str,
    source: str = "CCA",
) -> Optional[LockedTimelineSnapshot]:
    """Return existing snapshot for the lineage of `timeline_id`,
    scoped to this subscription + source. Accepts the physical
    timeline_id for caller convenience; resolves the lineage_id
    internally so callers don't have to thread it through.

    Returns None when no snapshot exists OR when the timeline_id
    has no matching row (defensive — caller's responsibility to
    surface this if they care)."""
    from app.modules.advisory.models import Timeline as _TL
    lineage_id = (await db.execute(
        select(_TL.lineage_id).where(_TL.id == timeline_id)
    )).scalar_one_or_none()
    if lineage_id is None:
        return None
    return await get_snapshot_by_lineage(
        db, subscription_id, lineage_id, source,
    )


async def get_snapshot_by_lineage(
    db: AsyncSession,
    subscription_id: str,
    lineage_id: str,
    source: str = "CCA",
) -> Optional[LockedTimelineSnapshot]:
    """Lineage-keyed lookup. Use this when the caller already has the
    lineage_id (e.g. it just queried a Timeline row) to avoid the
    extra round-trip in `get_snapshot`."""
    return (
        await db.execute(
            select(LockedTimelineSnapshot).where(
                LockedTimelineSnapshot.subscription_id == subscription_id,
                LockedTimelineSnapshot.lineage_id == lineage_id,
                LockedTimelineSnapshot.source == source,
            )
        )
    ).scalar_one_or_none()


async def has_snapshot(
    db: AsyncSession,
    subscription_id: str,
    timeline_id: str,
    source: str = "CCA",
) -> bool:
    """Quick existence check for (sub, timeline, source)."""
    return (await get_snapshot(db, subscription_id, timeline_id, source)) is not None


# ── Deserialisation ──────────────────────────────────────────────────────────

def deserialise_timeline(content: dict) -> dict:
    """Return a normalised in-memory representation that the renderer can consume.

    For Phase 1 this is essentially a pass-through — the content dict is already
    in the right shape — but it adds convenience indexes for O(1) lookups during
    rendering:
      - 'practices_by_id'  : practice_id -> practice dict
      - 'relations_by_id'  : relation_id -> relation dict
      - 'questions_by_id'  : question_id -> question dict
      - 'links_by_practice': practice_id -> list of conditional_link dicts

    Raises ValueError if `content` is not a dict.
    """
    if not isinstance(content, dict):
        raise ValueError("Snapshot content must be a dict")

    practices = content.get("practices", []) or []
    relations = content.get("relations", []) or []
    questions = content.get("conditional_questions", []) or []
    links = content.get("conditional_links", []) or []

    links_by_practice: dict[str, list] = {}
    for link in links:
        pid = link.get("practice_id")
        if pid is None:
            continue
        links_by_practice.setdefault(pid, []).append(link)

    return {
        **content,
        "practices_by_id": {p["id"]: p for p in practices if "id" in p},
        "relations_by_id": {r["id"]: r for r in relations if "id" in r},
        "questions_by_id": {q["id"]: q for q in questions if "id" in q},
        "links_by_practice": links_by_practice,
    }
