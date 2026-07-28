"""Pure query functions for the Client Reports dashboard (Phase 1).

This module holds every SQL query used by the reports endpoints, kept
strictly separate from FastAPI so the router stays a thin adapter.

Two disciplines baked in from Phase 1 that make future migration
painless (see `feedback_reporting_architecture_escalation_ladder.md`):

1. **Pure functions, one per report.** No FastAPI, no request state, no
   response models — just `(db, client_id, filters) -> data`. When
   Stage 2 (rollup tables) or Stage 3 (read replica) arrives, we swap
   the function body (or the session) without touching endpoints,
   tests, or the frontend.

2. **P95 latency log line on every query.** The ``@timed_query`` wrapper
   emits ``reports.query.timing`` at INFO with the query name, elapsed
   ms, and client_id. When a specific report starts consistently
   crossing ~500 ms P95 in prod logs, that's the data-driven trigger
   to escalate to a precomputed rollup for THAT report — no vibes.

Absolute filter contract, every query (see companion memory):

- ``client_id = :cid`` — nothing cross-client, ever.
- ``Client.is_training = false`` — explicit WHERE.
- ``Subscription.deleted_at IS NULL`` — auto via the session listener
  in ``app/modules/subscriptions/soft_delete.py``. **Every query MUST
  join Subscription** so the cascade tables (Order / OrderItem /
  Query) inherit the filter; a shortcut like
  ``select(Order).where(Order.client_id == cid)`` would leak
  cleaned-up rows.

The base scope helper ``_subscription_scope`` returns the join skeleton
so no query re-derives the filter contract.

Skeleton pass — function signatures + docstrings, bodies raise
``NotImplementedError``. Fill one metric at a time so each can be
code-reviewed and unit-tested before the next lands.
"""
from __future__ import annotations

import functools
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import Element, Package, Practice
from app.modules.subscriptions.snapshot_models import LockedTimelineSnapshot
from app.modules.clients.models import Client
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus, PackingList,
)
from app.modules.platform.models import User
from app.modules.subscriptions.models import Subscription, SubscriptionStatus


# Order statuses that count as a "real" order for headline metrics —
# reached at least SENT. Excludes DRAFT (unshared with the dealer),
# CANCELLED, and EXPIRED (aborted). Confirmed with the user 2026-07-27.
ORDER_COUNTED_STATUSES = [
    OrderStatus.SENT,
    OrderStatus.ACCEPTED,
    OrderStatus.PROCESSING,
    OrderStatus.SENT_FOR_APPROVAL,
    OrderStatus.PARTIALLY_APPROVED,
    OrderStatus.COMPLETED,
]


logger = logging.getLogger(__name__)


# ── P95 latency wrapper ───────────────────────────────────────────────────────

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def timed_query(name: str) -> Callable[[F], F]:
    """Wrap an async query function with a structured timing log.

    Emits at INFO on every call:

        reports.query.timing name=<name> ms=<elapsed> client_id=<cid>

    ``client_id`` is looked up positionally (arg 1, after ``db``) or by
    kwarg — the wrapper stays generic so every report query gets the
    log without a per-call boilerplate.
    """

    def _decorate(fn: F) -> F:
        @functools.wraps(fn)
        async def _wrapped(*args, **kwargs):
            started = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                cid = kwargs.get("client_id")
                if cid is None and len(args) >= 2:
                    cid = args[1]
                logger.info(
                    "reports.query.timing name=%s ms=%.1f client_id=%s",
                    name, elapsed_ms, cid,
                )

        return _wrapped  # type: ignore[return-value]

    return _decorate


# ── Base filter scope ─────────────────────────────────────────────────────────

def _subscription_scope(client_id: str) -> Select:
    """Return the base Subscription select every report query joins from.

    Applies both absolute filters:

    - ``Subscription.client_id == client_id`` (scope).
    - ``Client.is_training == False`` (training exclusion), via join.

    ``Subscription.deleted_at IS NULL`` is added automatically by the
    session-level listener installed in
    ``app/modules/subscriptions/soft_delete.py`` — do NOT re-add it
    here or you'll get a redundant clause.

    Every report query should build on top of this rather than starting
    from ``select(Order)`` or ``select(OrderItem)`` directly, so the
    filter contract is enforced at exactly one place.
    """
    return (
        select(Subscription)
        .join(Client, Client.id == Subscription.client_id)
        .where(
            Subscription.client_id == client_id,
            Client.is_training.is_(False),
        )
    )


def _apply_filters(stmt: Select, filters: "ReportFilters") -> Select:
    """Apply the optional Report chip filters to a subscription-scoped stmt.

    Joins Package / User only when a filter demands it — the ``subs_active``
    "no chips selected" path stays a two-table query. Ordering matches the
    frontend chip row: Crop · State · District · Package.

    Time filters (``period_from`` / ``period_to``) are NOT applied here —
    each metric applies them against its own date column (``subscription_date``
    for subs_new, ``created_at`` for orders_count, etc.). Left to the
    per-metric body to keep the semantics honest.
    """
    if filters.crop_cosh_id:
        stmt = stmt.join(Package, Package.id == Subscription.package_id).where(
            Package.crop_cosh_id == filters.crop_cosh_id,
        )
    if filters.state_cosh_id or filters.district_cosh_id:
        stmt = stmt.join(User, User.id == Subscription.farmer_user_id)
        if filters.state_cosh_id:
            stmt = stmt.where(User.state_cosh_id == filters.state_cosh_id)
        if filters.district_cosh_id:
            stmt = stmt.where(User.district_cosh_id == filters.district_cosh_id)
    if filters.package_id:
        stmt = stmt.where(Subscription.package_id == filters.package_id)
    return stmt


# ── Common filter bundle ──────────────────────────────────────────────────────

class ReportFilters:
    """Filter bundle passed into every report query.

    Kept as a plain container (not a Pydantic model) because these
    functions run pre-request too — from CSV export, from the Overview
    composer, and from unit tests that don't build request payloads.

    All fields optional; each query applies only the ones it cares
    about. period_from / period_to are UTC datetimes; the router
    normalises calendar-month presets before constructing this.
    """

    __slots__ = (
        "period_from", "period_to",
        "crop_cosh_id", "state_cosh_id", "district_cosh_id",
        "package_id",
    )

    def __init__(
        self,
        period_from: Optional[datetime] = None,
        period_to: Optional[datetime] = None,
        crop_cosh_id: Optional[str] = None,
        state_cosh_id: Optional[str] = None,
        district_cosh_id: Optional[str] = None,
        package_id: Optional[str] = None,
    ) -> None:
        self.period_from = period_from
        self.period_to = period_to
        self.crop_cosh_id = crop_cosh_id
        self.state_cosh_id = state_cosh_id
        self.district_cosh_id = district_cosh_id
        self.package_id = package_id


# ── Subscriptions subject area ────────────────────────────────────────────────

@timed_query("subs_new")
async def subs_new(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """New subscriptions in ``[period_from, period_to)``.

    Returns::

        {
          "relationships": <int>,   # subscription events in period
          "farmers": <int>,         # DISTINCT farmers whose FIRST-ever
                                    # sub within the filtered scope fell
                                    # in period
        }

    Both numbers on the same card per the design decision — "412 new
    subscriptions · 247 first-time farmers" reads as one story.

    Period semantics: subscription_date >= period_from AND < period_to.
    NULL subscription_date rows are excluded from both counts (can't
    date them; usually legacy pre-feature rows).

    "First-ever" scope: the MIN(subscription_date) subquery runs
    inside ``_apply_filters`` too — so under a Crop=Tomato filter,
    a farmer's first-ever *Tomato* sub in July counts even if they
    had Maize since 2020. That's the useful interpretation: "new is
    relative to what you're looking at."

    Two round-trips (one per aggregation) — cheap at Stage 1 scale
    and each keeps its own P95 log line, which is exactly the data
    we want if we ever escalate. Fold into a single SQL only when
    subs_new specifically shows up as a Stage-2 candidate.
    """
    scope = _apply_filters(_subscription_scope(client_id), filters)

    period_from = filters.period_from
    period_to = filters.period_to

    # ── relationships: sub events in period ──────────────────────────
    rel_stmt = scope.with_only_columns(
        func.count(Subscription.id).label("n"),
    ).where(Subscription.subscription_date.is_not(None))
    if period_from is not None:
        rel_stmt = rel_stmt.where(Subscription.subscription_date >= period_from)
    if period_to is not None:
        rel_stmt = rel_stmt.where(Subscription.subscription_date < period_to)
    relationships = int((await db.execute(rel_stmt)).scalar_one())

    # ── farmers: distinct first-time farmers whose MIN(sub_date) in period ──
    # Grouped subquery over the SAME filtered scope, then filter by
    # the MIN date. Postgres handles this well; the GROUP BY collapses
    # to farmer_user_id and HAVING scopes the min-date to the window.
    min_date_col = func.min(Subscription.subscription_date).label("first_date")
    grouped_scope = (
        scope.with_only_columns(
            Subscription.farmer_user_id.label("farmer_user_id"),
            min_date_col,
        )
        .where(Subscription.subscription_date.is_not(None))
        .group_by(Subscription.farmer_user_id)
    )
    if period_from is not None:
        grouped_scope = grouped_scope.having(min_date_col >= period_from)
    if period_to is not None:
        grouped_scope = grouped_scope.having(min_date_col < period_to)
    farmers_subq = grouped_scope.subquery()
    farmers_stmt = select(func.count()).select_from(farmers_subq)
    farmers = int((await db.execute(farmers_stmt)).scalar_one())

    return {"relationships": relationships, "farmers": farmers}


@timed_query("subs_active")
async def subs_active(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """Active subscriptions right now.

    Returns::

        {
          "subscriptions": <int>,   # status == ACTIVE
          "farmers": <int>,         # DISTINCT farmers with >=1 active
        }

    ``period_to`` is currently ignored — ``Subscription.status`` has no
    history table so we can only answer "active NOW". Historical
    "active as of date X" needs a status audit trail and is deferred
    to Phase 2+.
    """
    stmt = _apply_filters(
        _subscription_scope(client_id).where(
            Subscription.status == SubscriptionStatus.ACTIVE,
        ),
        filters,
    ).with_only_columns(
        func.count(Subscription.id).label("subscriptions"),
        func.count(func.distinct(Subscription.farmer_user_id)).label("farmers"),
    )
    row = (await db.execute(stmt)).one()
    return {"subscriptions": int(row.subscriptions), "farmers": int(row.farmers)}


@timed_query("subs_total")
async def subs_total(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """All subscriptions ever created on this client.

    Returns::

        {
          "subscriptions": <int>,   # every subscription row (any status)
          "farmers": <int>,         # DISTINCT farmers who ever subscribed
        }

    Same filter contract as ``subs_active`` minus the status carve-out
    — includes LAPSED / CANCELLED / SUSPENDED / UNSUBSCRIBED. Training
    and soft-deleted rows still don't count (auto via the base scope).

    Period is not applied — "Total" is cumulative-to-now by design.
    When we ship a Period-aware "Total as of date X", it becomes a
    separate metric so the semantics stay obvious.
    """
    stmt = _apply_filters(
        _subscription_scope(client_id), filters,
    ).with_only_columns(
        func.count(Subscription.id).label("subscriptions"),
        func.count(func.distinct(Subscription.farmer_user_id)).label("farmers"),
    )
    row = (await db.execute(stmt)).one()
    return {"subscriptions": int(row.subscriptions), "farmers": int(row.farmers)}


@timed_query("subs_active_by_dimension")
async def subs_active_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``subs_active`` by CROP / SPACE / PACKAGE.

    TIME dimension is unsupported (subs_active is point-in-time —
    there's nothing to bucket). Frontend hides the TIME tab for
    metrics with ``supportsTime=false``; if it slips through, this
    raises ValueError.
    """
    if dimension.upper() == "TIME":
        raise ValueError("subs_active does not support TIME dimension")
    base = _dimension_base(client_id, filters).where(
        Subscription.status == SubscriptionStatus.ACTIVE,
    )
    subs_expr = func.count(Subscription.id)
    farmers_expr = func.count(func.distinct(Subscription.farmer_user_id))
    stmt = _group_by_dimension(
        base, dimension, Subscription.subscription_date,
        filters.period_from, filters.period_to,
        extra_select=[
            subs_expr.label("subscriptions"),
            farmers_expr.label("farmers"),
        ],
        primary_order_expr=subs_expr,
    )
    rows = (await db.execute(stmt)).all()
    return [dict(r._mapping) for r in rows]


@timed_query("subs_total_by_dimension")
async def subs_total_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``subs_total`` by CROP / SPACE / PACKAGE / TIME.

    TIME buckets by ``Subscription.subscription_date`` — subs with
    NULL subscription_date drop out of the TIME bucket (nothing to
    bucket into) but still count in the other dimensions.
    """
    base = _dimension_base(client_id, filters)
    subs_expr = func.count(Subscription.id)
    farmers_expr = func.count(func.distinct(Subscription.farmer_user_id))
    stmt = _group_by_dimension(
        base, dimension, Subscription.subscription_date,
        filters.period_from, filters.period_to,
        extra_select=[
            subs_expr.label("subscriptions"),
            farmers_expr.label("farmers"),
        ],
        primary_order_expr=subs_expr,
    )
    rows = (await db.execute(stmt)).all()
    return [dict(r._mapping) for r in rows]


@timed_query("subs_new_by_dimension")
async def subs_new_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``subs_new`` by CROP / SPACE / PACKAGE / TIME.

    Returns ``{key, relationships, farmers, ...}`` per group.
    - **relationships** — subs whose subscription_date is in period,
      counted per group.
    - **farmers**       — DISTINCT farmers whose MIN(subscription_date)
      within (filtered scope × group) falls in period. "First-time in
      the current chip scope AND in this group" — under a Crop filter
      + CROP dimension, this is trivially "farmers whose first-ever
      Tomato sub is this month" grouped by crop.

    Two queries under the hood — one for relationships, one for
    grouped-MIN farmers — merged Python-side by key. Same pattern as
    the ``subs_new`` headline, extended to a GROUP BY.
    """
    base = _dimension_base(client_id, filters).where(
        Subscription.subscription_date.is_not(None),
    )

    # Query 1: relationships per group (subs with subscription_date in period)
    rel_base = base
    if filters.period_from is not None:
        rel_base = rel_base.where(Subscription.subscription_date >= filters.period_from)
    if filters.period_to is not None:
        rel_base = rel_base.where(Subscription.subscription_date < filters.period_to)
    rel_expr = func.count(Subscription.id)
    rel_stmt = _group_by_dimension(
        rel_base, dimension, Subscription.subscription_date,
        filters.period_from, filters.period_to,
        extra_select=[rel_expr.label("relationships")],
        primary_order_expr=rel_expr,
    )
    rel_rows = (await db.execute(rel_stmt)).all()

    # Query 2: first-time farmers per group. Group by (dim_key, farmer),
    # compute MIN(subscription_date), then HAVING min in period.
    dim = dimension.upper()
    min_date = func.min(Subscription.subscription_date).label("first_date")

    if dim == "CROP":
        grouped = base.with_only_columns(
            Package.crop_cosh_id.label("k"),
            Subscription.farmer_user_id.label("f"),
            min_date,
        ).group_by(Package.crop_cosh_id, Subscription.farmer_user_id)
    elif dim == "SPACE":
        grouped = base.with_only_columns(
            User.state_cosh_id.label("k"),
            Subscription.farmer_user_id.label("f"),
            min_date,
        ).group_by(User.state_cosh_id, Subscription.farmer_user_id)
    elif dim == "PACKAGE":
        grouped = base.with_only_columns(
            Subscription.package_id.label("k"),
            Subscription.farmer_user_id.label("f"),
            min_date,
        ).group_by(Subscription.package_id, Subscription.farmer_user_id)
    elif dim == "TIME":
        bucket = _pick_time_bucket(filters.period_from, filters.period_to)
        bucket_col = func.date_trunc(bucket, Subscription.subscription_date)
        grouped = base.with_only_columns(
            bucket_col.label("k"),
            Subscription.farmer_user_id.label("f"),
            min_date,
        ).group_by(bucket_col, Subscription.farmer_user_id)
    else:
        raise ValueError(f"Unknown dimension: {dimension!r}")

    if filters.period_from is not None:
        grouped = grouped.having(min_date >= filters.period_from)
    if filters.period_to is not None:
        grouped = grouped.having(min_date < filters.period_to)
    subq = grouped.subquery()
    farmers_by_group = (await db.execute(
        select(subq.c.k, func.count()).group_by(subq.c.k)
    )).all()
    farmer_map = {k: n for k, n in farmers_by_group}

    # Merge relationships + farmers by key. Include groups with 0
    # first-time farmers but nonzero relationships (returning-only).
    merged: list[dict] = []
    for r in rel_rows:
        row = dict(r._mapping)
        key = row.get("key")
        row["farmers"] = int(farmer_map.get(key, 0))
        merged.append(row)
    return merged


# ── Orders subject area ───────────────────────────────────────────────────────

@timed_query("orders_count")
async def orders_count(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """Order count in period.

    Returns::

        {"orders": <int>, "farmers": <int>}

    ``orders`` counts orders with status in ``ORDER_COUNTED_STATUSES``
    (see module top). DRAFT + CANCELLED + EXPIRED excluded.

    Period filters on ``Order.created_at`` (not subscription_date —
    they can be years apart on perennial subs). ``farmers`` = distinct
    farmers who placed a counted order.

    Every Order is reached via a JOIN through Subscription so the
    filter contract cascades — no separate Order-side check for
    training / soft-delete / client scoping.
    """
    scope = _apply_filters(_subscription_scope(client_id), filters)
    stmt = (
        scope
        .join(Order, Order.subscription_id == Subscription.id)
        .with_only_columns(
            func.count(func.distinct(Order.id)).label("orders"),
            func.count(func.distinct(Order.farmer_user_id)).label("farmers"),
        )
        .where(Order.status.in_(ORDER_COUNTED_STATUSES))
    )
    if filters.period_from is not None:
        stmt = stmt.where(Order.created_at >= filters.period_from)
    if filters.period_to is not None:
        stmt = stmt.where(Order.created_at < filters.period_to)
    row = (await db.execute(stmt)).one()
    return {"orders": int(row.orders), "farmers": int(row.farmers)}


@timed_query("orders_items")
async def orders_items(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """Item-level counts on counted-status orders in period.

    Returns::

        {
          "items_total":    <int>,   # every real item
          "items_approved": <int>,   # OrderItemStatus.APPROVED
          "items_rejected": <int>,   # OrderItemStatus.REJECTED
        }

    Real items exclude ``REMOVED`` (dropped from the order after it
    was drafted) and ``REROUTED`` (bookkeeping husk after Orders V2
    reroute — the actual item lives on a new order). Everything else
    — PENDING, AVAILABLE, POSTPONED, NOT_AVAILABLE, SENT_FOR_APPROVAL,
    APPROVED, REJECTED, NOT_NEEDED, SKIPPED — counts as a real item.

    Parent Order must be counted-status + in period, same rules as
    ``orders_count``. Period is on ``Order.created_at`` because
    OrderItem has no separate created_at we can rely on.

    All three counts in one round-trip via conditional COUNT FILTER.
    """
    scope = _apply_filters(_subscription_scope(client_id), filters)
    stmt = (
        scope
        .join(Order, Order.subscription_id == Subscription.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .with_only_columns(
            func.count(func.distinct(OrderItem.id))
                .filter(OrderItem.status.notin_([
                    OrderItemStatus.REMOVED, OrderItemStatus.REROUTED,
                ]))
                .label("items_total"),
            func.count(func.distinct(OrderItem.id))
                .filter(OrderItem.status == OrderItemStatus.APPROVED)
                .label("items_approved"),
            func.count(func.distinct(OrderItem.id))
                .filter(OrderItem.status == OrderItemStatus.REJECTED)
                .label("items_rejected"),
        )
        .where(Order.status.in_(ORDER_COUNTED_STATUSES))
    )
    if filters.period_from is not None:
        stmt = stmt.where(Order.created_at >= filters.period_from)
    if filters.period_to is not None:
        stmt = stmt.where(Order.created_at < filters.period_to)
    row = (await db.execute(stmt)).one()
    return {
        "items_total":    int(row.items_total),
        "items_approved": int(row.items_approved),
        "items_rejected": int(row.items_rejected),
    }


def _classify_brand_from_snapshot(
    snapshot_content: dict, practice_id: str,
) -> str | None:
    """Look up the practice inside a LockedTimelineSnapshot's JSONB
    and classify its brand-lock intent. Returns 'LOCKED' /
    'RECOMMENDED' / 'OPEN', or ``None`` if the practice isn't in the
    snapshot (odd — shouldn't happen in practice, but bail rather
    than misclassify).

    Falls back on missing ``is_brand_locked`` (pre-2026-07-28
    snapshots didn't capture the field) by returning None — caller
    reads live Practice instead.
    """
    practices = snapshot_content.get("practices") or []
    for p in practices:
        if p.get("id") != practice_id:
            continue
        if "is_brand_locked" not in p:
            return None
        if p.get("is_brand_locked"):
            return "LOCKED"
        for e in p.get("elements") or []:
            if e.get("element_type") != "BRAND_NAME":
                continue
            cosh_ref = (e.get("cosh_ref") or "").strip()
            if cosh_ref:
                return "RECOMMENDED"
        return "OPEN"
    return None


@timed_query("orders_brand_mix")
async def orders_brand_mix(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """Item-level brand mix — three-way split by SE AUTHORING intent
    at the moment the order landed with the dealer.

    Returns::

        {
          "locked":      <int>,
          "recommended": <int>,
          "open":        <int>,
        }

    Semantics (2-lock principle honoured):
    - **Locked**      — Practice.is_brand_locked = True at the moment
      the timeline was snapshotted. Dealer MUST sell the SE's SKU.
      Direct business the client captured.
    - **Recommended** — not locked, but Practice carries a BRAND_NAME
      element with non-empty cosh_ref. Dealer sees the suggestion,
      can substitute.
    - **Open**        — no brand named. Dealer picks freely.

    Read path:
    - If ``OrderItem.snapshot_id`` is set (the 2-lock frozen state),
      read from ``LockedTimelineSnapshot.content`` JSONB. SE edits
      to the live Practice after order-land do NOT reclassify.
    - Fall back to live Practice + Element when snapshot_id is NULL
      (legacy orders that pre-date the snapshot feature) OR the
      snapshot was written before is_brand_locked was captured in
      the serializer (pre-2026-07-28).

    Python-side classification because the JSONB traversal makes for
    unreadable SQL. Row volume per report call is bounded by
    counted-status orders in the filter scope — modest even at 10x
    current scale. If this becomes a P95 pain point (data will tell
    us via the ``@timed_query`` log), extend the snapshot with a
    denormalised ``brand_lock_state`` column and switch to SQL.

    Item-level not order-level. REMOVED / REROUTED excluded. Parent
    Order gated by ORDER_COUNTED_STATUSES + period on
    Order.created_at.
    """
    scope = _apply_filters(_subscription_scope(client_id), filters)

    # Fetch every real item on counted-status orders in period,
    # along with its Practice (fallback source) and the snapshot
    # content (authoritative when present).
    stmt = (
        scope
        .join(Order, Order.subscription_id == Subscription.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Practice, Practice.id == OrderItem.practice_id)
        .outerjoin(
            LockedTimelineSnapshot,
            LockedTimelineSnapshot.id == OrderItem.snapshot_id,
        )
        .with_only_columns(
            OrderItem.id.label("item_id"),
            OrderItem.practice_id.label("practice_id"),
            Practice.is_brand_locked.label("live_locked"),
            LockedTimelineSnapshot.content.label("snapshot_content"),
        )
        .where(
            Order.status.in_(ORDER_COUNTED_STATUSES),
            OrderItem.status.notin_([
                OrderItemStatus.REMOVED, OrderItemStatus.REROUTED,
            ]),
        )
        .distinct()
    )
    if filters.period_from is not None:
        stmt = stmt.where(Order.created_at >= filters.period_from)
    if filters.period_to is not None:
        stmt = stmt.where(Order.created_at < filters.period_to)

    rows = (await db.execute(stmt)).all()

    # For the live-fallback path we need "does this practice have a
    # BRAND_NAME element with non-empty cosh_ref?". Cache by
    # practice_id since the same practice appears on many items.
    fallback_pids: set[str] = set()
    for r in rows:
        if r.snapshot_content is None:
            fallback_pids.add(r.practice_id)
        else:
            classified = _classify_brand_from_snapshot(
                r.snapshot_content, r.practice_id,
            )
            if classified is None:
                fallback_pids.add(r.practice_id)

    has_brand_by_pid: dict[str, bool] = {}
    if fallback_pids:
        brand_rows = (await db.execute(
            select(Element.practice_id).where(
                Element.practice_id.in_(fallback_pids),
                Element.element_type == "BRAND_NAME",
                Element.cosh_ref.is_not(None),
                func.length(func.trim(Element.cosh_ref)) > 0,
            ).distinct()
        )).all()
        for (pid,) in brand_rows:
            has_brand_by_pid[pid] = True

    counts = {"LOCKED": 0, "RECOMMENDED": 0, "OPEN": 0}
    for r in rows:
        state: str | None = None
        if r.snapshot_content is not None:
            state = _classify_brand_from_snapshot(
                r.snapshot_content, r.practice_id,
            )
        if state is None:
            if r.live_locked:
                state = "LOCKED"
            elif has_brand_by_pid.get(r.practice_id):
                state = "RECOMMENDED"
            else:
                state = "OPEN"
        counts[state] += 1

    return {
        "locked":      counts["LOCKED"],
        "recommended": counts["RECOMMENDED"],
        "open":        counts["OPEN"],
    }


@timed_query("orders_routing")
async def orders_routing(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """Order routing — Direct vs Via Facilitator.

    Returns::

        {"direct": <int>, "via_facilitator": <int>}

    Direct = ``facilitator_user_id IS NULL`` (farmer ordered directly
    from a dealer). Via Facilitator = NOT NULL (facilitator forwarded
    the order to a dealer).

    One query, two conditional counts — cheaper than two separate
    fetches for a metric that always renders both together.

    Same status + period filters as ``orders_count``. Frontend-side
    labels; server keys stay stable.
    """
    scope = _apply_filters(_subscription_scope(client_id), filters)
    stmt = (
        scope
        .join(Order, Order.subscription_id == Subscription.id)
        .with_only_columns(
            func.count(func.distinct(Order.id))
                .filter(Order.facilitator_user_id.is_(None))
                .label("direct"),
            func.count(func.distinct(Order.id))
                .filter(Order.facilitator_user_id.is_not(None))
                .label("via_facilitator"),
        )
        .where(Order.status.in_(ORDER_COUNTED_STATUSES))
    )
    if filters.period_from is not None:
        stmt = stmt.where(Order.created_at >= filters.period_from)
    if filters.period_to is not None:
        stmt = stmt.where(Order.created_at < filters.period_to)
    row = (await db.execute(stmt)).one()
    return {
        "direct": int(row.direct),
        "via_facilitator": int(row.via_facilitator),
    }


@timed_query("orders_conversion")
async def orders_conversion(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """Order → sale conversion.

    Returns::

        {
          "ordered":   <int>,   # counted-status orders in period
          "approved":  <int>,   # order has any APPROVED item
          "picked_up": <int>,   # order has any PackingList row with
                                # farmer_received_at NOT NULL
        }

    Frontend renders three ratios from these:
      - Sale conversion = picked_up / ordered  (headline)
      - Approval        = approved  / ordered  (secondary)
      - Fulfilment      = picked_up / approved (diagnostic — how many
                                                approvals actually
                                                translated to pickup)

    Semantics:
    - **ordered** — same denominator as ``orders_count``. Every order
      that reached the dealer (SENT and beyond) counts.
    - **approved** — the order has AT LEAST ONE item in APPROVED
      status. A partially-approved order still counts here.
    - **picked_up** — the sale marker per the plan: at least one
      PackingList row on the order has ``farmer_received_at`` set.
      Farmer receipt is the moment the sale is genuinely closed.

    All three counts in one round-trip via conditional COUNT FILTER
    + correlated EXISTS.
    """
    scope = _apply_filters(_subscription_scope(client_id), filters)

    approved_exists = (
        select(OrderItem.id).where(
            OrderItem.order_id == Order.id,
            OrderItem.status == OrderItemStatus.APPROVED,
        ).exists()
    )
    picked_up_exists = (
        select(PackingList.id).where(
            PackingList.order_id == Order.id,
            PackingList.farmer_received_at.is_not(None),
        ).exists()
    )

    stmt = (
        scope
        .join(Order, Order.subscription_id == Subscription.id)
        .with_only_columns(
            func.count(func.distinct(Order.id)).label("ordered"),
            func.count(func.distinct(Order.id))
                .filter(approved_exists)
                .label("approved"),
            func.count(func.distinct(Order.id))
                .filter(picked_up_exists)
                .label("picked_up"),
        )
        .where(Order.status.in_(ORDER_COUNTED_STATUSES))
    )
    if filters.period_from is not None:
        stmt = stmt.where(Order.created_at >= filters.period_from)
    if filters.period_to is not None:
        stmt = stmt.where(Order.created_at < filters.period_to)
    row = (await db.execute(stmt)).one()
    return {
        "ordered":   int(row.ordered),
        "approved":  int(row.approved),
        "picked_up": int(row.picked_up),
    }


# ── Dimension-drill shared helpers ────────────────────────────────────────────
#
# All *_by_dimension functions share the same base-query shape:
# subscription scope + Package + User pre-joined (so GROUP BY on
# crop_cosh_id / state_cosh_id / package_id doesn't need conditional
# joins). Filter WHERE clauses are inlined here rather than routing
# through _apply_filters — the latter conditionally-joins based on
# filter presence, which conflicts with the deterministic-join
# requirement of dimension drills.

def _dimension_base(client_id: str, filters: ReportFilters) -> Select:
    """Subscription scope + Package + User joins + chip filter WHEREs.
    Callers add the metric-specific joins (Order, OrderItem) and any
    metric-specific period filter on top."""
    base = (
        _subscription_scope(client_id)
        .join(Package, Package.id == Subscription.package_id)
        .join(User, User.id == Subscription.farmer_user_id)
    )
    if filters.crop_cosh_id:
        base = base.where(Package.crop_cosh_id == filters.crop_cosh_id)
    if filters.state_cosh_id:
        base = base.where(User.state_cosh_id == filters.state_cosh_id)
    if filters.district_cosh_id:
        base = base.where(User.district_cosh_id == filters.district_cosh_id)
    if filters.package_id:
        base = base.where(Subscription.package_id == filters.package_id)
    return base


def _orders_dimension_base(client_id: str, filters: ReportFilters) -> Select:
    """Dimension base extended with Order join + status + period on
    Order.created_at. Used by every orders_*_by_dimension query."""
    base = _dimension_base(client_id, filters).join(
        Order, Order.subscription_id == Subscription.id,
    ).where(Order.status.in_(ORDER_COUNTED_STATUSES))
    if filters.period_from is not None:
        base = base.where(Order.created_at >= filters.period_from)
    if filters.period_to is not None:
        base = base.where(Order.created_at < filters.period_to)
    return base


def _group_by_dimension(
    base: Select,
    dimension: str,
    date_col,
    period_from,
    period_to,
    *,
    extra_select: list,
    primary_order_expr,
) -> Select:
    """Attach the dimension GROUP BY + ORDER BY.

    ``date_col`` — the column used for TIME bucketing (e.g.
    ``Order.created_at`` or ``Subscription.subscription_date``).
    ``extra_select`` — metric-specific SELECT columns (COUNT FILTERs).
    ``primary_order_expr`` — the expression used to order desc for
    non-TIME dimensions (usually the primary count).

    Returns a Select shaped like::

        SELECT <dim_key>[, <dim_extras>], <metric_columns...>
        FROM <base>
        GROUP BY <dim_key>[, <dim_extras>]
        ORDER BY <primary_order_expr DESC>   -- or bucket ASC for TIME
    """
    dim = dimension.upper()
    if dim == "CROP":
        stmt = base.with_only_columns(
            Package.crop_cosh_id.label("key"), *extra_select,
        ).group_by(Package.crop_cosh_id).order_by(primary_order_expr.desc())
    elif dim == "SPACE":
        stmt = base.with_only_columns(
            User.state_cosh_id.label("key"), *extra_select,
        ).group_by(User.state_cosh_id).order_by(primary_order_expr.desc())
    elif dim == "PACKAGE":
        stmt = base.with_only_columns(
            Subscription.package_id.label("key"),
            Package.name.label("package_name"),
            *extra_select,
        ).group_by(Subscription.package_id, Package.name).order_by(
            primary_order_expr.desc(),
        )
    elif dim == "TIME":
        bucket = _pick_time_bucket(period_from, period_to)
        bucket_col = func.date_trunc(bucket, date_col).label("key")
        stmt = base.with_only_columns(
            bucket_col, *extra_select,
        ).where(date_col.is_not(None)).group_by(bucket_col).order_by(bucket_col.asc())
    else:
        raise ValueError(f"Unknown dimension: {dimension!r}")
    return stmt


def _pick_time_bucket(period_from, period_to) -> str:
    """Auto-pick date_trunc bucket size based on the period window.

    Rule confirmed with user on 2026-07-27:
      ≤ 14 days  → daily
      ≤ 120 days → weekly (Postgres week starts Monday)
      else       → monthly

    When no period bounds are given, default to monthly — a long
    all-time trend is only readable as months.
    """
    if period_from is None or period_to is None:
        return "month"
    days = (period_to - period_from).days
    if days <= 14:
        return "day"
    if days <= 120:
        return "week"
    return "month"


@timed_query("orders_count_by_dimension")
async def orders_count_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``orders_count`` by CROP / SPACE / PACKAGE / TIME.

    Returns a list of rows shaped for the frontend drill table::

        [{"key": <id-or-iso-date>, "label_needs_lookup": <bool>,
          "orders": <int>, "farmers": <int>, ...}, ...]

    - **CROP**    — group by ``Package.crop_cosh_id``. Row ``key`` is
      the cosh_id (resolved to English name by the router).
    - **SPACE**   — group by farmer's ``User.state_cosh_id``. Same.
      District drill deferred per plan (state-level is enough for v1).
    - **PACKAGE** — group by ``Subscription.package_id``. Row ``key``
      is the package_id; the label is ``Package.name`` (no cosh
      lookup needed).
    - **TIME**    — ``date_trunc(bucket, Order.created_at)`` with
      auto bucket size from ``_pick_time_bucket``. Row ``key`` is
      the bucket-start datetime (ISO). Ordered ascending.

    Same status filter (ORDER_COUNTED_STATUSES) + period filter as
    ``orders_count``. Dimension queries pre-join Package + User
    directly instead of routing through ``_apply_filters`` because
    the join needs to exist regardless of filter presence (else
    grouping by an un-joined column blows up).
    """
    base = _orders_dimension_base(client_id, filters)
    orders_count_expr = func.count(func.distinct(Order.id))
    farmers_count_expr = func.count(func.distinct(Order.farmer_user_id))
    stmt = _group_by_dimension(
        base, dimension, Order.created_at,
        filters.period_from, filters.period_to,
        extra_select=[
            orders_count_expr.label("orders"),
            farmers_count_expr.label("farmers"),
        ],
        primary_order_expr=orders_count_expr,
    )
    rows = (await db.execute(stmt)).all()
    return [dict(r._mapping) for r in rows]


@timed_query("orders_routing_by_dimension")
async def orders_routing_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``orders_routing`` by dimension.

    Row shape: ``{key, direct, via_facilitator, ...}`` per group.
    Primary sort by (direct + via_facilitator) desc so the busiest
    group leads.
    """
    base = _orders_dimension_base(client_id, filters)
    direct_expr = func.count(func.distinct(Order.id)).filter(
        Order.facilitator_user_id.is_(None),
    )
    via_expr = func.count(func.distinct(Order.id)).filter(
        Order.facilitator_user_id.is_not(None),
    )
    stmt = _group_by_dimension(
        base, dimension, Order.created_at,
        filters.period_from, filters.period_to,
        extra_select=[
            direct_expr.label("direct"),
            via_expr.label("via_facilitator"),
        ],
        primary_order_expr=func.count(func.distinct(Order.id)),
    )
    rows = (await db.execute(stmt)).all()
    return [dict(r._mapping) for r in rows]


@timed_query("orders_items_by_dimension")
async def orders_items_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``orders_items`` by dimension.

    Row shape: ``{key, items_total, items_approved, items_rejected,
    ...}`` per group. Real items only (REMOVED / REROUTED excluded).
    Primary sort by items_total desc.
    """
    base = _orders_dimension_base(client_id, filters).join(
        OrderItem, OrderItem.order_id == Order.id,
    )
    total_expr = func.count(func.distinct(OrderItem.id)).filter(
        OrderItem.status.notin_([
            OrderItemStatus.REMOVED, OrderItemStatus.REROUTED,
        ]),
    )
    approved_expr = func.count(func.distinct(OrderItem.id)).filter(
        OrderItem.status == OrderItemStatus.APPROVED,
    )
    rejected_expr = func.count(func.distinct(OrderItem.id)).filter(
        OrderItem.status == OrderItemStatus.REJECTED,
    )
    stmt = _group_by_dimension(
        base, dimension, Order.created_at,
        filters.period_from, filters.period_to,
        extra_select=[
            total_expr.label("items_total"),
            approved_expr.label("items_approved"),
            rejected_expr.label("items_rejected"),
        ],
        primary_order_expr=total_expr,
    )
    rows = (await db.execute(stmt)).all()
    return [dict(r._mapping) for r in rows]


@timed_query("orders_conversion_by_dimension")
async def orders_conversion_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``orders_conversion`` by dimension.

    Row shape: ``{key, ordered, approved, picked_up, ...}`` per
    group. Same EXISTS-based conditional counts as the headline.
    Primary sort by ordered desc.
    """
    base = _orders_dimension_base(client_id, filters)
    approved_exists = (
        select(OrderItem.id).where(
            OrderItem.order_id == Order.id,
            OrderItem.status == OrderItemStatus.APPROVED,
        ).exists()
    )
    picked_up_exists = (
        select(PackingList.id).where(
            PackingList.order_id == Order.id,
            PackingList.farmer_received_at.is_not(None),
        ).exists()
    )
    ordered_expr  = func.count(func.distinct(Order.id))
    approved_expr = func.count(func.distinct(Order.id)).filter(approved_exists)
    picked_expr   = func.count(func.distinct(Order.id)).filter(picked_up_exists)
    stmt = _group_by_dimension(
        base, dimension, Order.created_at,
        filters.period_from, filters.period_to,
        extra_select=[
            ordered_expr.label("ordered"),
            approved_expr.label("approved"),
            picked_expr.label("picked_up"),
        ],
        primary_order_expr=ordered_expr,
    )
    rows = (await db.execute(stmt)).all()
    return [dict(r._mapping) for r in rows]


@timed_query("orders_brand_mix_by_dimension")
async def orders_brand_mix_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``orders_brand_mix`` by dimension. Snapshot-aware.

    Row shape: ``{key, locked, recommended, open, ...}`` per group.

    Reads brand-lock intent from ``LockedTimelineSnapshot.content``
    when ``OrderItem.snapshot_id`` is set (2-lock guarantee — see
    ``reference_brand_authoring_states``). Falls back to live
    Practice when the snapshot is absent or pre-dates the
    is_brand_locked serializer addition.

    Python-side classification because the JSONB traversal on top
    of a GROUP BY makes for gnarly SQL. Row volume per report call
    is bounded by items on counted-status orders in scope — modest
    at current scale. If this hits a P95 pain point, denormalise
    the brand_lock_state onto the OrderItem or an aux table.
    """
    dim = dimension.upper()
    base = _orders_dimension_base(client_id, filters).join(
        OrderItem, OrderItem.order_id == Order.id,
    ).outerjoin(
        LockedTimelineSnapshot,
        LockedTimelineSnapshot.id == OrderItem.snapshot_id,
    ).join(
        Practice, Practice.id == OrderItem.practice_id,
    ).where(
        OrderItem.status.notin_([
            OrderItemStatus.REMOVED, OrderItemStatus.REROUTED,
        ]),
    )

    # Select dimension key + per-item classification inputs.
    if dim == "CROP":
        key_col = Package.crop_cosh_id.label("key")
        extra_cols = [key_col]
    elif dim == "SPACE":
        key_col = User.state_cosh_id.label("key")
        extra_cols = [key_col]
    elif dim == "PACKAGE":
        key_col = Subscription.package_id.label("key")
        extra_cols = [key_col, Package.name.label("package_name")]
    elif dim == "TIME":
        bucket = _pick_time_bucket(filters.period_from, filters.period_to)
        key_col = func.date_trunc(bucket, Order.created_at).label("key")
        extra_cols = [key_col]
    else:
        raise ValueError(f"Unknown dimension: {dimension!r}")

    stmt = base.with_only_columns(
        *extra_cols,
        OrderItem.id.label("item_id"),
        OrderItem.practice_id.label("practice_id"),
        Practice.is_brand_locked.label("live_locked"),
        LockedTimelineSnapshot.content.label("snapshot_content"),
    ).distinct()

    rows = (await db.execute(stmt)).all()

    # Fallback lookup: for practices whose snapshot doesn't carry
    # is_brand_locked (or has no snapshot at all), read the BRAND_NAME
    # element existence from the live Practice.
    fallback_pids: set[str] = set()
    for r in rows:
        if r.snapshot_content is None:
            fallback_pids.add(r.practice_id)
        else:
            if _classify_brand_from_snapshot(
                r.snapshot_content, r.practice_id,
            ) is None:
                fallback_pids.add(r.practice_id)
    has_brand_by_pid: dict[str, bool] = {}
    if fallback_pids:
        brand_rows = (await db.execute(
            select(Element.practice_id).where(
                Element.practice_id.in_(fallback_pids),
                Element.element_type == "BRAND_NAME",
                Element.cosh_ref.is_not(None),
                func.length(func.trim(Element.cosh_ref)) > 0,
            ).distinct()
        )).all()
        for (pid,) in brand_rows:
            has_brand_by_pid[pid] = True

    # Bucket per-item classification by dimension key.
    buckets: dict[Any, dict] = {}
    for r in rows:
        state: Optional[str] = None
        if r.snapshot_content is not None:
            state = _classify_brand_from_snapshot(
                r.snapshot_content, r.practice_id,
            )
        if state is None:
            if r.live_locked:
                state = "LOCKED"
            elif has_brand_by_pid.get(r.practice_id):
                state = "RECOMMENDED"
            else:
                state = "OPEN"

        key = r.key
        if key not in buckets:
            bucket_entry: dict = {
                "key": key, "locked": 0, "recommended": 0, "open": 0,
            }
            if dim == "PACKAGE":
                bucket_entry["package_name"] = r.package_name
            buckets[key] = bucket_entry
        buckets[key][state.lower()] += 1

    out = list(buckets.values())
    if dim == "TIME":
        out.sort(key=lambda x: x["key"] or datetime.min)
    else:
        out.sort(
            key=lambda x: (x["locked"] + x["recommended"] + x["open"]),
            reverse=True,
        )
    return out


# ── Overview page composer ────────────────────────────────────────────────────

async def overview_bundle(
    db: AsyncSession,
    client_id: str,
    filters: ReportFilters,
) -> dict:
    """Compose the Overview page payload in one HTTP round-trip.

    Calls four headline metrics sequentially with the same filter
    set. Each metric keeps its own P95 log line via
    ``@timed_query`` — when Overview becomes slow, the logs tell us
    WHICH sub-query is heavy, not just "Overview is slow." See
    ``feedback_reporting_architecture_escalation_ladder.md``.

    Returns::

        {
          "subs_new":          {"relationships": <int>, "farmers": <int>},
          "subs_active":       {"subscriptions": <int>, "farmers": <int>},
          "orders_count":      {"orders":        <int>, "farmers": <int>},
          "orders_conversion": {"ordered":       <int>, "approved": <int>,
                                "picked_up":     <int>},
        }

    Deferred to Phase 1 polish:
    - Prev-period deltas (doubles the query count; add a second
      ``prev_filters`` parameter and shape the response as
      ``{current, prev, delta}`` per card).
    - Hero chart series (needs the *_by_dimension queries to land).
    """
    subs_new_res      = await subs_new(db, client_id, filters)
    subs_active_res   = await subs_active(db, client_id, filters)
    orders_count_res  = await orders_count(db, client_id, filters)
    orders_conv_res   = await orders_conversion(db, client_id, filters)
    return {
        "subs_new":          subs_new_res,
        "subs_active":       subs_active_res,
        "orders_count":      orders_count_res,
        "orders_conversion": orders_conv_res,
    }


# ── CSV row queries ───────────────────────────────────────────────────────────
#
# Row-per-entity fetchers for the CSV export endpoints. Unlike the
# headline metrics which aggregate, these return raw rows so a
# manager can slice further in Excel. Same filter contract applies
# (client scoping + is_training + soft-delete cascade) via
# _subscription_scope + _apply_filters.

@timed_query("subscriptions_rows")
async def subscriptions_rows(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> list[dict]:
    """Row-per-Subscription for CSV export.

    Applies chip filters (crop/state/district/package). If
    ``period_from`` / ``period_to`` are set, further narrows to
    subscriptions whose ``subscription_date`` falls in the window
    (matching ``subs_new`` semantics). Rows lacking
    ``subscription_date`` are included only when Period is unset.

    Cosh id fields (crop_cosh_id, state, district) are returned as
    ids; the endpoint resolves them to English names in a single
    batch lookup before writing the CSV.
    """
    stmt = (
        _apply_filters(_subscription_scope(client_id), filters)
        .join(Package, Package.id == Subscription.package_id)
        .join(User, User.id == Subscription.farmer_user_id)
        .with_only_columns(
            Subscription.reference_number.label("subscription_ref"),
            User.name.label("farmer_name"),
            User.phone.label("farmer_phone"),
            Package.name.label("package_name"),
            Package.crop_cosh_id.label("crop_cosh_id"),
            User.state_cosh_id.label("state_cosh_id"),
            User.district_cosh_id.label("district_cosh_id"),
            Subscription.subscription_date.label("subscription_date"),
            Subscription.status.label("status"),
            Subscription.subscription_type.label("subscription_type"),
        )
        .order_by(Subscription.subscription_date.desc().nulls_last())
    )
    if filters.period_from is not None:
        stmt = stmt.where(Subscription.subscription_date >= filters.period_from)
    if filters.period_to is not None:
        stmt = stmt.where(Subscription.subscription_date < filters.period_to)
    rows = (await db.execute(stmt)).all()
    return [dict(r._mapping) for r in rows]


@timed_query("orders_rows")
async def orders_rows(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> list[dict]:
    """Row-per-Order for CSV export.

    Only counted-status orders (SENT and beyond) in period. Includes
    aggregated item counts (total, approved, rejected) and pickup
    timestamp (max farmer_received_at across the order's
    PackingLists — an order can have multiple).

    Facilitator / dealer names looked up via correlated subqueries
    to keep this to one round-trip; if that becomes a P95 pain
    point, replace with a bulk-hydrate pass in Python.
    """
    dealer_alias = User.__table__.alias("dealer_u")
    facilitator_alias = User.__table__.alias("facilitator_u")
    farmer_alias = User.__table__.alias("farmer_u")

    approved_count = (
        select(func.count(OrderItem.id))
        .where(
            OrderItem.order_id == Order.id,
            OrderItem.status == OrderItemStatus.APPROVED,
        )
        .correlate(Order)
        .scalar_subquery()
    )
    rejected_count = (
        select(func.count(OrderItem.id))
        .where(
            OrderItem.order_id == Order.id,
            OrderItem.status == OrderItemStatus.REJECTED,
        )
        .correlate(Order)
        .scalar_subquery()
    )
    real_count = (
        select(func.count(OrderItem.id))
        .where(
            OrderItem.order_id == Order.id,
            OrderItem.status.notin_([
                OrderItemStatus.REMOVED, OrderItemStatus.REROUTED,
            ]),
        )
        .correlate(Order)
        .scalar_subquery()
    )
    picked_up_at = (
        select(func.max(PackingList.farmer_received_at))
        .where(PackingList.order_id == Order.id)
        .correlate(Order)
        .scalar_subquery()
    )

    stmt = (
        _apply_filters(_subscription_scope(client_id), filters)
        .join(Order, Order.subscription_id == Subscription.id)
        .join(Package, Package.id == Subscription.package_id)
        .join(farmer_alias, farmer_alias.c.id == Order.farmer_user_id)
        .outerjoin(dealer_alias, dealer_alias.c.id == Order.dealer_user_id)
        .outerjoin(
            facilitator_alias,
            facilitator_alias.c.id == Order.facilitator_user_id,
        )
        .with_only_columns(
            Order.reference_number.label("order_ref"),
            Order.created_at.label("order_date"),
            Order.status.label("status"),
            farmer_alias.c.name.label("farmer_name"),
            farmer_alias.c.phone.label("farmer_phone"),
            dealer_alias.c.name.label("dealer_name"),
            facilitator_alias.c.name.label("facilitator_name"),
            Order.facilitator_user_id.label("facilitator_user_id"),
            Package.name.label("package_name"),
            Package.crop_cosh_id.label("crop_cosh_id"),
            real_count.label("items_total"),
            approved_count.label("items_approved"),
            rejected_count.label("items_rejected"),
            picked_up_at.label("picked_up_at"),
        )
        .where(Order.status.in_(ORDER_COUNTED_STATUSES))
        .order_by(Order.created_at.desc())
    )
    if filters.period_from is not None:
        stmt = stmt.where(Order.created_at >= filters.period_from)
    if filters.period_to is not None:
        stmt = stmt.where(Order.created_at < filters.period_to)
    rows = (await db.execute(stmt)).all()
    return [dict(r._mapping) for r in rows]
