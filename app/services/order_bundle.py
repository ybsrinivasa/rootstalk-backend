"""Date-range bundling rules for farmer purchase orders.

The user-facing model (locked 2026-05-21):

  - Each order has ONE `category`: PESTICIDE or FERTILIZER.
    Adjuvants (L1=SPECIAL_INPUT) ride with pesticides.
  - The farmer picks a date range [today, chosen_to_date]; the
    server bundles every eligible practice whose timeline window
    overlaps that range by even one day.
  - One practice can be in AT MOST ONE order over the
    subscription's lifetime. The bundle excludes any practice
    already ordered (in any non-CANCELLED order).
  - NPK-dosage L2s are calculation-only — no trade names exist
    so dealers can't fulfil them. Excluded from bundles.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import Practice, Timeline
from app.modules.orders.models import Order, OrderItem, OrderStatus
from app.modules.subscriptions.models import Subscription


# L1 (practice taxonomy parent groups, singular) → bundle categories.
PESTICIDE_L1S = {"PESTICIDE", "SPECIAL_INPUT"}
FERTILIZER_L1S = {"FERTILIZER"}

# L2s under L1=FERTILIZER that are calculation aids only (no trade
# names → no dealer products). Excluded from bundles per
# cosh_options_view.L2_TYPES_WITHOUT_TRADE_NAMES.
FERTILIZER_L2_BUNDLE_EXCLUDE = {
    "CHEMICAL_FERTILIZERS_NPK_DOSAGES",
    "FERTIGATION_NPK_DOSAGES",
}

CATEGORY_PESTICIDE = "PESTICIDE"
CATEGORY_FERTILIZER = "FERTILIZER"
ALL_CATEGORIES = {CATEGORY_PESTICIDE, CATEGORY_FERTILIZER}


def l1_set_for_category(category: str) -> set[str]:
    cat = category.upper()
    if cat == CATEGORY_PESTICIDE:
        return PESTICIDE_L1S
    if cat == CATEGORY_FERTILIZER:
        return FERTILIZER_L1S
    return set()


def l2_exclude_for_category(category: str) -> set[str]:
    cat = category.upper()
    if cat == CATEGORY_FERTILIZER:
        return FERTILIZER_L2_BUNDLE_EXCLUDE
    return set()


def _timeline_window(
    tl: Timeline,
    crop_start: date | None,
    today: date | None = None,
) -> tuple[date, date] | None:
    """Convert a Timeline's offsets into absolute calendar dates.

    DAS / DBS: anchor to `crop_start` (required for both; returns
    None if absent).
    CALENDAR (Perennial, Batch 21): `from_value` / `to_value` are
    day-of-year (1..365/366). Window resolves to the current
    subscription year (`today.year`). When `today` is omitted we
    default to `date.today()`. Wrap-around ranges
    (from_value > to_value, e.g. 350 → 10) aren't supported in V1
    — the bundle returns None for those rows, matching the
    advisory walk's current behaviour.

    Returns None for:
      - DAYS_AFTER_DETECTION / DAYS_AFTER_RESPONSE (handled by the
        diagnosis flow, not the order bundle).
      - NULL offsets — defensive.
    """
    if tl.from_value is None or tl.to_value is None:
        return None
    ftype = tl.from_type.value if hasattr(tl.from_type, "value") else str(tl.from_type)
    if ftype == "DAS":
        if crop_start is None:
            return None
        return (
            crop_start + timedelta(days=int(tl.from_value)),
            crop_start + timedelta(days=int(tl.to_value)),
        )
    if ftype == "DBS":
        if crop_start is None:
            return None
        # BL-17 boundary rule: DBS closes the day BEFORE crop_start
        # when to_value == 0. `max(to_value, 1)` clamp keeps DBS
        # strictly pre-sowing so it doesn't bleed into a DAS-only
        # date-range order and trip the timing-type mix guard in
        # POST /farmer/orders. Mirrors the same clamp in
        # snapshot_render, snapshot_sweep, and bl17_timeline_boundary.
        return (
            crop_start - timedelta(days=int(tl.from_value)),
            crop_start - timedelta(days=max(int(tl.to_value), 1)),
        )
    if ftype == "CALENDAR":
        if today is None:
            today = date.today()
        if tl.from_value > tl.to_value:
            return None  # wrap-around unsupported in V1
        year_start = date(today.year, 1, 1)
        return (
            year_start + timedelta(days=int(tl.from_value) - 1),
            year_start + timedelta(days=int(tl.to_value) - 1),
        )
    return None


def windows_overlap(
    a_from: date, a_to: date, b_from: date, b_to: date,
) -> bool:
    """Inclusive overlap — one shared day is enough. Order-of-
    endpoints agnostic so DBS-shaped (from > to in date terms)
    timelines compose correctly."""
    a_lo, a_hi = (a_from, a_to) if a_from <= a_to else (a_to, a_from)
    b_lo, b_hi = (b_from, b_to) if b_from <= b_to else (b_to, b_from)
    return a_lo <= b_hi and b_lo <= a_hi


async def _build_timeline_windows_for_dedup(
    db: AsyncSession, *,
    subscription: Subscription,
    today: date,
    to_date: date,
) -> list:
    """Builds the TimelineWindow list BL-03 expects, covering the
    bundle's contribution from all four advisory sources:
      - Package CCA timelines (DAS + DBS) whose window overlaps
        [today, to_date]
      - Active triggered CHA timelines (PG, SP, QA via the
        TriggeredCHAEntry table) whose window overlaps [today, to_date]
    Each window has its INPUT practices loaded with the elements the
    dedup engine needs for `primary_identity_ref()` (COMMON_NAME /
    BRAND / ACTIVE_INGREDIENT cosh_refs).

    Returns `[]` if `crop_start_date` is unset (no anchor to compute
    DAS/DBS windows).
    """
    from app.services.bl03_deduplication import (
        TimelineWindow as TLWindow, PracticeStub, PracticeElement,
    )
    from app.modules.advisory.models import (
        Practice, Element, Timeline, Relation,
    )
    from app.modules.subscriptions.models import TriggeredCHAEntry

    # Batch 21: Perennial allowed — `crop_start_date` may be None.
    # CALENDAR timelines resolve via today.year; DAS/DBS rows just
    # get skipped without an anchor.
    crop_start = (
        subscription.crop_start_date.date()
        if hasattr(subscription.crop_start_date, "date")
        else subscription.crop_start_date
    ) if subscription.crop_start_date is not None else None

    candidate_tls: dict[str, tuple[date, date, str]] = {}
    """tl_id -> (from_date, to_date, source). source: CCA | CHA"""

    # ── CCA (package) timelines ──────────────────────────────────
    pkg_tls = (await db.execute(
        select(Timeline).where(Timeline.package_id == subscription.package_id)
    )).scalars().all()
    pkg_tl_by_id: dict[str, Timeline] = {tl.id: tl for tl in pkg_tls}
    for tl in pkg_tls:
        w = _timeline_window(tl, crop_start, today)
        if w and windows_overlap(w[0], w[1], today, to_date):
            candidate_tls[tl.id] = (w[0], w[1], "CCA")

    # ── CHA-pipe triggered timelines (PG / SP / QA) ──────────────
    # Each TriggeredCHAEntry points at one of three timeline tables
    # (sp_, pg_, qa via standard_response_id). The triggered date
    # anchors the window — CHA does NOT shift with crop_start_date.
    cha_entries = (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.subscription_id == subscription.id,
            TriggeredCHAEntry.status == "ACTIVE",
        )
    )).scalars().all()
    cha_tl_by_id: dict[str, Timeline] = {}
    for cha in cha_entries:
        triggered_d = cha.triggered_at.date() if hasattr(cha.triggered_at, "date") else cha.triggered_at
        cha_tls = []
        if cha.recommendation_type == "SP":
            cha_tls = (await db.execute(
                select(Timeline).where(Timeline.sp_recommendation_id == cha.recommendation_id)
            )).scalars().all()
        elif cha.recommendation_type == "PG":
            cha_tls = (await db.execute(
                select(Timeline).where(Timeline.pg_recommendation_id == cha.recommendation_id)
            )).scalars().all()
        elif cha.recommendation_type == "QA":
            cha_tls = (await db.execute(
                select(Timeline).where(Timeline.standard_response_id == cha.recommendation_id)
            )).scalars().all()
        for cha_tl in cha_tls:
            if cha_tl.from_value is None or cha_tl.to_value is None:
                continue
            from_d = triggered_d + timedelta(days=int(cha_tl.from_value))
            to_d = triggered_d + timedelta(days=int(cha_tl.to_value))
            if windows_overlap(from_d, to_d, today, to_date):
                candidate_tls[cha_tl.id] = (from_d, to_d, "CHA")
                cha_tl_by_id[cha_tl.id] = cha_tl

    # ── Context timelines: past CCA/CHA timelines that own an
    # APPROVED practice on this subscription. BL-03 step 12 (the
    # purchased-input rule) requires them to participate in dedup
    # so a later timeline's identical practice stays suppressed
    # even after the governing timeline closed. Mirrors the
    # advisory walk's `context_tl_ids` pass at /farmer/advisory/today.
    approved_practice_to_tl = (await db.execute(
        select(OrderItem.practice_id, OrderItem.timeline_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.subscription_id == subscription.id,
            OrderItem.status == "APPROVED",
        )
    )).all()
    context_tl_ids = {
        tl_id for _pid, tl_id in approved_practice_to_tl
        if tl_id and tl_id not in candidate_tls
    }
    if context_tl_ids:
        # Most context timelines live in `timelines` (CCA / PG / SP / QA
        # all share the same table). Load + add with their dates.
        ctx_rows = (await db.execute(
            select(Timeline).where(Timeline.id.in_(context_tl_ids))
        )).scalars().all()
        for ctx_tl in ctx_rows:
            # Use the shared `_timeline_window` so CALENDAR context
            # timelines also resolve correctly. Anchor for DAS/DBS is
            # crop_start; CALENDAR uses today.year (Batch 21).
            cw = _timeline_window(ctx_tl, crop_start, today)
            if cw is None:
                continue
            candidate_tls[ctx_tl.id] = (cw[0], cw[1], "CCA")
            pkg_tl_by_id[ctx_tl.id] = ctx_tl

    if not candidate_tls:
        return []

    # ── Load practices + elements in a single round ──────────────
    practices = (await db.execute(
        select(Practice).where(Practice.timeline_id.in_(candidate_tls.keys()))
    )).scalars().all()
    practice_ids = [p.id for p in practices]
    elements_by_practice: dict[str, list[Element]] = {}
    if practice_ids:
        elements = (await db.execute(
            select(Element).where(Element.practice_id.in_(practice_ids))
        )).scalars().all()
        for el in elements:
            elements_by_practice.setdefault(el.practice_id, []).append(el)

    # Relation type lookup — needed by PracticeStub for completeness.
    relation_ids = {p.relation_id for p in practices if p.relation_id}
    relations_by_id: dict[str, Relation] = {}
    if relation_ids:
        rels = (await db.execute(
            select(Relation).where(Relation.id.in_(relation_ids))
        )).scalars().all()
        relations_by_id = {r.id: r for r in rels}

    # ── Build TimelineWindow stubs grouped by tl_id ──────────────
    practices_by_tl: dict[str, list[PracticeStub]] = {}
    for p in practices:
        stub_els = [
            PracticeElement(
                element_type=el.element_type,
                cosh_ref=el.cosh_ref,
                value=el.value,
                unit_cosh_id=el.unit_cosh_id,
            )
            for el in elements_by_practice.get(p.id, [])
        ]
        rel_type = None
        if p.relation_id and p.relation_id in relations_by_id:
            rel = relations_by_id[p.relation_id]
            rel_type = rel.relation_type.value if hasattr(rel.relation_type, "value") else str(rel.relation_type)
        stub = PracticeStub(
            id=p.id,
            l0_type=p.l0_type.value if hasattr(p.l0_type, "value") else str(p.l0_type),
            l1_type=p.l1_type, l2_type=p.l2_type,
            display_order=p.display_order or 0,
            is_special_input=bool(p.is_special_input),
            relation_id=p.relation_id,
            relation_role=p.relation_role,
            relation_type=rel_type,
            frequency_days=p.frequency_days,
            elements=stub_els,
        )
        practices_by_tl.setdefault(p.timeline_id, []).append(stub)

    out: list[TLWindow] = []
    for tl_id, (from_d, to_d, source) in candidate_tls.items():
        tl_row = pkg_tl_by_id.get(tl_id) or cha_tl_by_id.get(tl_id)
        if not tl_row:
            continue
        created_d = tl_row.created_at.date() if hasattr(tl_row.created_at, "date") else today
        out.append(TLWindow(
            id=tl_id,
            name=tl_row.name or "",
            from_date=from_d, to_date=to_d,
            created_at=created_d,
            practices=practices_by_tl.get(tl_id, []),
            source=source,
        ))
    return out


async def filter_by_conditional_answers(
    db: AsyncSession, *,
    subscription: Subscription,
    today: date,
    candidate_practice_ids: set[str],
) -> set[str]:
    """BL-02 step 9: filter candidate practices by the farmer's
    conditional answers for `today`.

    A practice passes if:
      (a) it has no `PracticeConditional` row (always included), OR
      (b) the farmer's answer to the linked question matches the
          conditional's `answer` value, OR
      (c) the conditional's `answer` is `BOTH` (always included).

    A practice is filtered OUT when its conditional question was
    not yet answered (blank path — BL-02 step 12) or the answer
    doesn't match.

    Used by `compute_bundle` and `resolve_dbs_practices_for_category`
    so the order side matches what the farmer sees on
    `/farmer/advisory/today` — without this, practices the farmer
    answered "NO" to would still flow into the bundle.
    """
    if not candidate_practice_ids:
        return set()

    from app.modules.advisory.models import PracticeConditional
    from app.modules.subscriptions.models import (
        ConditionalAnswer as _ConditionalAnswer,
    )

    pc_rows = (await db.execute(
        select(PracticeConditional).where(
            PracticeConditional.practice_id.in_(list(candidate_practice_ids))
        )
    )).scalars().all()
    if not pc_rows:
        return set(candidate_practice_ids)  # nothing to filter

    pc_by_practice: dict[str, PracticeConditional] = {pc.practice_id: pc for pc in pc_rows}

    cond_rows = (await db.execute(
        select(_ConditionalAnswer).where(
            _ConditionalAnswer.subscription_id == subscription.id,
            _ConditionalAnswer.answer_date == today,
        )
    )).scalars().all()
    today_answers: dict[str, str] = {
        r.question_id: (r.answer.value if hasattr(r.answer, "value") else str(r.answer))
        for r in cond_rows
    }

    survivors: set[str] = set()
    for pid in candidate_practice_ids:
        pc = pc_by_practice.get(pid)
        if pc is None:
            survivors.add(pid)
            continue
        required = pc.answer.value if hasattr(pc.answer, "value") else str(pc.answer)
        if required == "BOTH":
            survivors.add(pid)
            continue
        farmer_answer = today_answers.get(pc.question_id)
        if farmer_answer is not None and farmer_answer == required:
            survivors.add(pid)
    return survivors


async def dedup_filter_practice_ids(
    db: AsyncSession, *,
    subscription: Subscription,
    today: date,
    to_date: date,
    candidate_practice_ids: set[str],
) -> set[str]:
    """Return the subset of `candidate_practice_ids` that survive
    BL-03 deduplication across all advisory sources active for this
    subscription (CCA package + triggered CHA PG / SP / QA).

    Used by `compute_bundle` (DAS) and
    `resolve_dbs_practices_for_category` (DBS) so the order side
    matches what the farmer sees on `/farmer/advisory/today`.
    Without this, duplicate recommendations across timelines would
    all land in the order — the farmer would buy the same input
    twice.
    """
    from app.services.bl03_deduplication import deduplicate_advisory

    if not candidate_practice_ids:
        return set()

    tl_windows = await _build_timeline_windows_for_dedup(
        db, subscription=subscription, today=today, to_date=to_date,
    )
    if not tl_windows:
        return set(candidate_practice_ids)

    # Approved set — practices the farmer has already purchased.
    # BL-03 step 12 says purchased inputs stay suppressed in
    # overlapping timelines.
    approved_rows = (await db.execute(
        select(OrderItem.practice_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.subscription_id == subscription.id,
            OrderItem.status == "APPROVED",
        )
    )).all()
    approved_ids: set[str] = {r[0] for r in approved_rows if r[0]}

    deduped = deduplicate_advisory(tl_windows, approved_practice_ids=approved_ids)

    surviving_ids: set[str] = set()
    for dt in deduped:
        for p in dt.visible_practices:
            surviving_ids.add(p.id)

    return {pid for pid in candidate_practice_ids if pid in surviving_ids}


async def resolve_dbs_practices_for_category(
    db: AsyncSession, *, subscription: Subscription, category: str,
) -> list[str]:
    """Return all DBS practice IDs in the subscription's package that
    match the category's L1 set, survive the L2 exclude list, and
    survive BL-03 dedup across CCA + CHA sources.

    BL-04a context: DBS practices live on timelines with
    `from_type == DBS`. We don't filter by date window here — the
    bulk order takes EVERY DBS practice of this category that
    isn't already in another order. The caller layers the
    "already-ordered" filter on top.

    Subscription is required (not just package_id) because BL-03 is
    per-subscription: the active CHA-pipe timelines come from this
    farmer's `triggered_cha_entries`, which differ between
    subscribers of the same package.
    """
    from app.modules.advisory.models import Practice, Timeline

    l1_allowed = l1_set_for_category(category)
    l2_excluded = l2_exclude_for_category(category)
    if not l1_allowed:
        return []

    rows = (await db.execute(
        select(Practice.id)
        .join(Timeline, Timeline.id == Practice.timeline_id)
        .where(
            Timeline.package_id == subscription.package_id,
            Timeline.from_type == "DBS",
            Practice.l0_type == "INPUT",
            Practice.l1_type.in_(list(l1_allowed)),
        )
    )).all()

    practice_ids = [r[0] for r in rows]
    if l2_excluded and practice_ids:
        filt = (await db.execute(
            select(Practice.id).where(
                Practice.id.in_(practice_ids),
                Practice.l2_type.notin_(list(l2_excluded)),
            )
        )).all()
        practice_ids = [r[0] for r in filt]

    if not practice_ids:
        return []

    # BL-02 step 9: drop practices the farmer answered "NO" to (or
    # hasn't answered yet). Runs unconditionally — applies even
    # pre-start-date.
    today = date.today()
    cond_survivors = await filter_by_conditional_answers(
        db, subscription=subscription, today=today,
        candidate_practice_ids=set(practice_ids),
    )
    practice_ids = [pid for pid in practice_ids if pid in cond_survivors]
    if not practice_ids:
        return []

    # BL-03 dedup needs an anchor to compute windows. Pre-start-date
    # carve-out: no crop_start → skip dedup, return as-is. Matches the
    # advisory walk's deferral of DBS rendering pre-start.
    if subscription.crop_start_date is None:
        return practice_ids

    crop_start = (
        subscription.crop_start_date.date()
        if hasattr(subscription.crop_start_date, "date")
        else subscription.crop_start_date
    )
    to_date_for_dedup = crop_start - timedelta(days=1) if crop_start > today else today
    survivors = await dedup_filter_practice_ids(
        db, subscription=subscription, today=today, to_date=to_date_for_dedup,
        candidate_practice_ids=set(practice_ids),
    )
    return [pid for pid in practice_ids if pid in survivors]


async def already_ordered_practice_ids(
    db: AsyncSession, subscription_id: str,
) -> set[str]:
    """Every practice_id appearing in any non-CANCELLED order for
    this subscription. Cancelled orders release their practices
    back into the pool — same rule as BL-10's "Cancel returns
    items to Purchase required".
    """
    rows = (await db.execute(
        select(OrderItem.practice_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.subscription_id == subscription_id,
            Order.status != OrderStatus.CANCELLED,
        )
    )).all()
    return {r[0] for r in rows if r[0]}


async def compute_bundle(
    db: AsyncSession,
    *,
    subscription: Subscription,
    category: str,
    to_date: date,
    today: date,
) -> dict:
    """Return the bundle preview / actual set for an order.

    Shape:
        {
          "practices": [{id, l1_type, l2_type, timeline_id,
                         timeline_from_date, timeline_to_date,
                         display_order}],
          "excluded_already_ordered": int,
        }

    The caller decides whether to use this as a preview
    (GET /order-preview) or to bind these practice_ids onto a new
    Order (POST /farmer/orders).
    """
    # Batch 21: Perennial packages may have no `crop_start_date` —
    # their timelines are CALENDAR only. We still resolve the bundle;
    # DAS/DBS rows return None from `_timeline_window` without an
    # anchor and get skipped naturally.
    crop_start = (
        subscription.crop_start_date.date()
        if hasattr(subscription.crop_start_date, "date")
        else subscription.crop_start_date
    ) if subscription.crop_start_date is not None else None

    l1_set = l1_set_for_category(category)
    l2_exclude = l2_exclude_for_category(category)
    if not l1_set:
        return {"practices": [], "excluded_already_ordered": 0}

    # Batch 23: load CCA (package) timelines + active triggered CHA
    # (PG / SP / QA) timelines for this subscription. CHA-derived
    # practices recommended by diagnosis pipes must be orderable
    # alongside CCA practices — without this, the farmer's "Order"
    # button on a CHA recommendation card silently does nothing
    # because the preview bundle excludes the practice.
    timelines = (await db.execute(
        select(Timeline).where(Timeline.package_id == subscription.package_id)
    )).scalars().all()

    eligible_tl_windows: dict[str, tuple[date, date]] = {}
    for tl in timelines:
        w = _timeline_window(tl, crop_start, today)
        if w is None:
            continue
        if windows_overlap(w[0], w[1], today, to_date):
            eligible_tl_windows[tl.id] = w

    # CHA-pipe triggered timelines (anchored to `triggered_at`, not
    # crop_start). Same Timeline model — only the anchor logic differs.
    from app.modules.subscriptions.models import TriggeredCHAEntry
    cha_entries = (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.subscription_id == subscription.id,
            TriggeredCHAEntry.status == "ACTIVE",
        )
    )).scalars().all()
    for cha in cha_entries:
        triggered_d = cha.triggered_at.date() if hasattr(cha.triggered_at, "date") else cha.triggered_at
        cha_tls = []
        if cha.recommendation_type == "SP":
            cha_tls = (await db.execute(
                select(Timeline).where(Timeline.sp_recommendation_id == cha.recommendation_id)
            )).scalars().all()
        elif cha.recommendation_type == "PG":
            cha_tls = (await db.execute(
                select(Timeline).where(Timeline.pg_recommendation_id == cha.recommendation_id)
            )).scalars().all()
        elif cha.recommendation_type == "QA":
            cha_tls = (await db.execute(
                select(Timeline).where(Timeline.standard_response_id == cha.recommendation_id)
            )).scalars().all()
        for cha_tl in cha_tls:
            if cha_tl.from_value is None or cha_tl.to_value is None:
                continue
            cha_from = triggered_d + timedelta(days=int(cha_tl.from_value))
            cha_to = triggered_d + timedelta(days=int(cha_tl.to_value))
            if windows_overlap(cha_from, cha_to, today, to_date):
                eligible_tl_windows[cha_tl.id] = (cha_from, cha_to)

    if not eligible_tl_windows:
        return {"practices": [], "excluded_already_ordered": 0}

    practices = (await db.execute(
        select(Practice).where(
            Practice.timeline_id.in_(eligible_tl_windows.keys()),
        )
    )).scalars().all()

    practices = [
        p for p in practices
        if (p.l1_type or "").upper() in l1_set
        and (p.l2_type or "").upper() not in l2_exclude
    ]

    # BL-02 step 9: drop practices whose conditional question was
    # answered "NO" (or not answered yet — blank path) by the farmer.
    # The advisory walk runs this filter; the bundle needs to match
    # so practices the farmer rejected don't slip into the order.
    if practices:
        cond_survivors = await filter_by_conditional_answers(
            db, subscription=subscription, today=today,
            candidate_practice_ids={p.id for p in practices},
        )
        practices = [p for p in practices if p.id in cond_survivors]

    # BL-03: drop practices suppressed by deduplication across CCA +
    # CHA sources. The farmer sees the deduped list in /farmer/
    # advisory/today; without this, the bundle would include
    # suppressed siblings and the farmer would buy the same input
    # twice. Fix added 2026-05-31 in response to BL audit conversation.
    if practices:
        survivors = await dedup_filter_practice_ids(
            db, subscription=subscription, today=today, to_date=to_date,
            candidate_practice_ids={p.id for p in practices},
        )
        practices = [p for p in practices if p.id in survivors]

    already_ordered = await already_ordered_practice_ids(db, subscription.id)
    excluded = sum(1 for p in practices if p.id in already_ordered)
    practices = [p for p in practices if p.id not in already_ordered]

    practices.sort(key=lambda p: (p.display_order or 0, p.id))

    out_rows = []
    for p in practices:
        w = eligible_tl_windows[p.timeline_id]
        out_rows.append({
            "id": p.id,
            "l0_type": p.l0_type.value if hasattr(p.l0_type, "value") else str(p.l0_type),
            "l1_type": p.l1_type,
            "l2_type": p.l2_type,
            "timeline_id": p.timeline_id,
            "timeline_from_date": w[0].isoformat(),
            "timeline_to_date": w[1].isoformat(),
            "display_order": p.display_order,
        })
    return {
        "practices": out_rows,
        "excluded_already_ordered": excluded,
    }


def package_end_date(subscription: Subscription, duration_days: int) -> date | None:
    """Computed cap for the date picker — `crop_start + duration_days`.
    The PWA clamps the user's chosen `to_date` to this. Returns None
    when crop_start_date isn't set yet (the order flow is gated upstream)."""
    if subscription.crop_start_date is None:
        return None
    cs = (
        subscription.crop_start_date.date()
        if hasattr(subscription.crop_start_date, "date")
        else subscription.crop_start_date
    )
    return cs + timedelta(days=duration_days)


def conflicts_with_existing_orders(
    requested_ids: Iterable[str], already_ordered: set[str],
) -> list[str]:
    """Practice IDs in the request that are already bound to a
    prior non-CANCELLED order — the "one practice per order" rule
    violation. Caller decides whether to silently exclude or 409."""
    return sorted(set(requested_ids) & already_ordered)
