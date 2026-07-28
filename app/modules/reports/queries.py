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

from app.modules.advisory.models import Package
from app.modules.clients.models import Client
from app.modules.platform.models import User
from app.modules.subscriptions.models import Subscription, SubscriptionStatus


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
    """New subscriptions in [period_from, period_to).

    Returns::

        {
          "relationships": <int>,   # sub events in period
          "farmers": <int>,         # DISTINCT farmers whose FIRST-ever
                                    # sub with this client fell in period
        }

    Both numbers on the same card per the design decision. ``farmers``
    uses a correlated MIN(subscription_date) subquery scoped to this
    client so a farmer's 2nd crop later doesn't count them again.
    """
    raise NotImplementedError


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


@timed_query("subs_new_by_dimension")
async def subs_new_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``subs_new`` by dimension.

    ``dimension`` one of TIME | SPACE | CROP | PACKAGE. Returns a list
    of ``{label, subscriptions, farmers}`` rows ordered by count desc
    (or by time bucket ascending when dimension=TIME).
    """
    raise NotImplementedError


@timed_query("subs_active_by_dimension")
async def subs_active_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``subs_active`` by dimension."""
    raise NotImplementedError


@timed_query("subs_total_by_dimension")
async def subs_total_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``subs_total`` by dimension."""
    raise NotImplementedError


# ── Orders subject area ───────────────────────────────────────────────────────

@timed_query("orders_count")
async def orders_count(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """Order count in period.

    Includes orders with status in {SENT, ACCEPTED, PROCESSING,
    SENT_FOR_APPROVAL, PARTIALLY_APPROVED, COMPLETED}. Excludes DRAFT
    (unshared) and CANCELLED / EXPIRED (aborted) per open decision.

    Returns::

        {"orders": <int>, "farmers": <int>}
    """
    raise NotImplementedError


@timed_query("orders_items")
async def orders_items(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """Item-level counts in period.

    Returns::

        {
          "items_total": <int>,
          "items_approved": <int>,   # OrderItemStatus.APPROVED
          "items_rejected": <int>,   # OrderItemStatus.REJECTED
        }

    REMOVED / REROUTED excluded — they're bookkeeping, not real items.
    """
    raise NotImplementedError


@timed_query("orders_brand_mix")
async def orders_brand_mix(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """Item-level brand mix — the three-way split.

    Returns::

        {
          "locked": <int>,        # brand_cosh_id IS NOT NULL
          "unlocked": <int>,      # brand_cosh_id NULL, brand_name NOT NULL
          "no_brand": <int>,      # both NULL
        }

    Item-level not order-level per design decision. no_brand kept as
    a first-class bucket — silently dropping it would hide the "12% of
    items have no brand" signal.
    """
    raise NotImplementedError


@timed_query("orders_routing")
async def orders_routing(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """Order routing — Direct vs Via Facilitator.

    Returns::

        {"direct": <int>, "via_facilitator": <int>}

    Direct = ``facilitator_user_id IS NULL``. Via Facilitator = NOT NULL.
    Labels are frontend-side; server returns keys.
    """
    raise NotImplementedError


@timed_query("orders_conversion")
async def orders_conversion(
    db: AsyncSession, client_id: str, filters: ReportFilters,
) -> dict:
    """Order → sale conversion.

    Returns::

        {
          "ordered": <int>,
          "approved": <int>,       # order has any APPROVED item
          "picked_up": <int>,      # PackingList.farmer_received_at NOT NULL
        }

    Frontend renders three ratios from these:
      - Sale conversion = picked_up / ordered  (headline)
      - Approval        = approved  / ordered  (secondary)
      - Fulfilment      = picked_up / approved (diagnostic)
    """
    raise NotImplementedError


@timed_query("orders_count_by_dimension")
async def orders_count_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``orders_count`` by dimension."""
    raise NotImplementedError


@timed_query("orders_items_by_dimension")
async def orders_items_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``orders_items`` by dimension."""
    raise NotImplementedError


@timed_query("orders_brand_mix_by_dimension")
async def orders_brand_mix_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``orders_brand_mix`` by dimension.

    Each row: ``{label, locked, unlocked, no_brand}``. Frontend renders
    a stacked-bar chart from this shape.
    """
    raise NotImplementedError


@timed_query("orders_routing_by_dimension")
async def orders_routing_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``orders_routing`` by dimension.

    Each row: ``{label, direct, via_facilitator}``.
    """
    raise NotImplementedError


@timed_query("orders_conversion_by_dimension")
async def orders_conversion_by_dimension(
    db: AsyncSession, client_id: str, filters: ReportFilters, dimension: str,
) -> list[dict]:
    """Group ``orders_conversion`` by dimension.

    Each row: ``{label, ordered, approved, picked_up}``.
    """
    raise NotImplementedError


# ── Overview page composer ────────────────────────────────────────────────────

async def overview_bundle(
    db: AsyncSession,
    client_id: str,
    filters: ReportFilters,
    prev_filters: ReportFilters,
) -> dict:
    """Compose the Overview page payload in one round-trip to the router.

    Calls the underlying single-metric functions in sequence — six
    Postgres round-trips today. Chosen deliberately over a single
    hand-rolled SQL so each sub-query keeps its own P95 log line
    (see feedback_reporting_architecture_escalation_ladder.md); if
    Overview becomes slow, the logs tell us WHICH sub-query to
    escalate first, not just "Overview is slow."

    ``prev_filters`` is the previous-period window (calendar-month
    prior by default) so headline cards can render deltas without a
    second round-trip.

    Returns a dict shaped for the ``/overview`` endpoint payload —
    exact contract locked when the router lands.
    """
    raise NotImplementedError
