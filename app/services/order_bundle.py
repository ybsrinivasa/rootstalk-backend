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
    tl: Timeline, crop_start: date,
) -> tuple[date, date] | None:
    """Convert a Timeline's relative DAS/DBS offsets into absolute
    calendar dates, given the subscription's crop_start_date.
    Returns None for unsupported timing types (e.g. CALENDAR with
    no anchor in this minimal helper)."""
    ftype = tl.from_type.value if hasattr(tl.from_type, "value") else str(tl.from_type)
    if ftype == "DAS":
        return (
            crop_start + timedelta(days=tl.from_value),
            crop_start + timedelta(days=tl.to_value),
        )
    if ftype == "DBS":
        return (
            crop_start - timedelta(days=tl.from_value),
            crop_start - timedelta(days=tl.to_value),
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
    if subscription.crop_start_date is None:
        return {"practices": [], "excluded_already_ordered": 0}
    crop_start = (
        subscription.crop_start_date.date()
        if hasattr(subscription.crop_start_date, "date")
        else subscription.crop_start_date
    )

    l1_set = l1_set_for_category(category)
    l2_exclude = l2_exclude_for_category(category)
    if not l1_set:
        return {"practices": [], "excluded_already_ordered": 0}

    timelines = (await db.execute(
        select(Timeline).where(Timeline.package_id == subscription.package_id)
    )).scalars().all()

    eligible_tl_windows: dict[str, tuple[date, date]] = {}
    for tl in timelines:
        w = _timeline_window(tl, crop_start)
        if w is None:
            continue
        if windows_overlap(w[0], w[1], today, to_date):
            eligible_tl_windows[tl.id] = w
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
