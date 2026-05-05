"""BL-09 — Daily Alert Triggering (pure functions, no DB).

Two alert types:
- START_DATE: subscription is ACTIVE but `crop_start_date` is unset.
- INPUT: an INPUT-class practice is in window today AND no order
  exists for that practice yet.

Both alerts are once-per-(subscription, type, day): the live task
checks the `alerts` table for today's rows before resending.

Recipient resolution (per the BL-09 spec):
- If the farmer has explicitly configured `alert_recipients` rows for
  the subscription, those rows are authoritative (respects the farmer's
  override of the local person, opt-out from self-alerts, etc).
- If no rows are configured, defaults apply: the farmer plus the
  subscription's `promoter_user_id` (the promoter who assigned). For
  self-subscribed subs `promoter_user_id` is None, so the default is
  the farmer alone.
- Company-RM alerts are not auto-defaulted — they are written only
  when explicitly configured (deeper data-model work; out of audit scope).

This module is pure: it takes already-loaded inputs and returns
decisions. The live Celery task in `app/tasks/alerts.py` does the I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.services.snapshot_render import TimelineMetadata, cca_window_active


# ── Inputs ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SubscriptionView:
    """Just the fields BL-09 needs from a Subscription row."""
    subscription_id: str
    subscription_type: str            # "SELF" | "ASSIGNED"
    farmer_user_id: str
    promoter_user_id: Optional[str]
    crop_start_date: Optional[date]   # None ⇒ START_DATE alert candidate


@dataclass(frozen=True)
class ConfiguredRecipient:
    """One ACTIVE row from `alert_recipients` for a subscription."""
    user_id: str
    role: str                         # "FARMER" | "LOCAL_PERSON" | "PROMOTER" | "COMPANY_RM"


@dataclass(frozen=True)
class TimelineWindow:
    """A timeline + the input-class practices it owns."""
    timeline_id: str
    from_type: str                    # "DAS" | "DBS" | "CALENDAR"
    from_value: int
    to_value: int
    input_practice_ids: tuple[str, ...]


# ── Outputs ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AlertRecipientSpec:
    user_id: str
    role: str                         # "FARMER" | "LOCAL_PERSON" | "COMPANY_RM"


# ── Recipient resolution ──────────────────────────────────────────────────────

def resolve_alert_recipients(
    sub: SubscriptionView,
    configured: list[ConfiguredRecipient],
) -> list[AlertRecipientSpec]:
    """Resolve who should be alerted for this subscription.

    Rule: if the farmer has explicit `configured` rows, those win
    verbatim (farmer's stated preference; includes opting out of self
    by simply not having a FARMER row). Otherwise apply defaults:
    farmer + promoter (skipping the promoter slot if there is none, e.g.
    self-subscribed without a configured local person).

    The 'PROMOTER' role from `alert_recipients` is normalised to
    'LOCAL_PERSON' on output — they are the same slot from BL-09's
    perspective; the table just predates the renaming.
    """
    if configured:
        seen: dict[str, str] = {}
        for r in configured:
            normalised = "LOCAL_PERSON" if r.role == "PROMOTER" else r.role
            seen.setdefault(r.user_id, normalised)
        return [AlertRecipientSpec(user_id=u, role=role) for u, role in seen.items()]

    out: list[AlertRecipientSpec] = [
        AlertRecipientSpec(user_id=sub.farmer_user_id, role="FARMER"),
    ]
    if sub.promoter_user_id and sub.promoter_user_id != sub.farmer_user_id:
        out.append(AlertRecipientSpec(
            user_id=sub.promoter_user_id, role="LOCAL_PERSON",
        ))
    return out


# ── Alert decisions ───────────────────────────────────────────────────────────

def should_send_start_date_alert(
    sub: SubscriptionView,
    sent_today: bool,
) -> bool:
    """START_DATE fires when the subscription is missing its
    `crop_start_date` AND no START_DATE alert has been recorded
    for this subscription today."""
    return sub.crop_start_date is None and not sent_today


def find_input_practices_due_today(
    timelines: list[TimelineWindow],
    day_offset: int,
) -> list[str]:
    """Return the IDs of all INPUT practices whose timeline window is
    active for `day_offset` (= today_date − crop_start_date).

    Delegates DAS/DBS arithmetic to `cca_window_active` (single source
    of truth, BL-04 fix: DBS uses -from <= offset <= -to with the
    production from > to convention). CALENDAR is intentionally not
    handled here — `cca_window_active` defers it, matching the BL-04
    today route. CALENDAR alert support is a separate spec gap.
    """
    out: list[str] = []
    for tl in timelines:
        meta = TimelineMetadata(
            from_type=tl.from_type,
            from_value=tl.from_value,
            to_value=tl.to_value,
        )
        if cca_window_active(meta, day_offset):
            out.extend(tl.input_practice_ids)
    return out


# OrderStatus values that suppress an INPUT alert. EXPIRED and CANCELLED
# do NOT suppress — once an order is dead, the farmer needs to be nudged
# again (the input window may still be open).
_SUPPRESSING_ORDER_STATUSES = frozenset({
    "DRAFT", "SENT", "ACCEPTED", "PROCESSING",
    "SENT_FOR_APPROVAL", "PARTIALLY_APPROVED", "COMPLETED",
})


def practices_still_unordered(
    due_practice_ids: list[str],
    practice_ids_with_active_orders: set[str],
) -> list[str]:
    """Of the practices due today, which still have no live order?
    The caller pre-computes `practice_ids_with_active_orders` from
    OrderItem ⨝ Order rows whose order status is in
    `_SUPPRESSING_ORDER_STATUSES`. Returning [] means every due
    practice has been ordered already → no INPUT alert today."""
    return [pid for pid in due_practice_ids if pid not in practice_ids_with_active_orders]


def should_send_input_alert(
    sub: SubscriptionView,
    due_practice_ids: list[str],
    practice_ids_with_active_orders: set[str],
    sent_today: bool,
) -> bool:
    """INPUT fires when the farmer has set their start date, at least
    one input practice is in window today, that practice has not been
    ordered yet, and we haven't already sent an INPUT alert today for
    this subscription."""
    if sub.crop_start_date is None:
        return False
    if sent_today:
        return False
    return bool(practices_still_unordered(
        due_practice_ids, practice_ids_with_active_orders,
    ))
