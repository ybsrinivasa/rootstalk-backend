"""Trust score computation — dealer-side behavioural cue.

See `docs/CMS_v1_scoping.md` §10 for the design + rationale.

The score answers "how well does this farmer adhere to what they've
agreed to?" It's anchored to the **agreed due date** of each credit
— not to any RootsTalk-supplied standard — because we don't interpose
opinions on their agreement (design principle §2.2).

Algorithm:

1. Fetch all CONFIRMED entries for the account.
2. FIFO-retire debt-increasing entries with debt-decreasing entries in
   chronological order of entry_date. Payments don't earmark to
   specific credits — we assume older debts clear first, which
   matches how dealers naturally reconcile.
3. For each resolved CREDIT_ADVANCED (fully paid OR past due_date),
   classify into a weight bucket based on how much was paid by
   due_date and how late (if ever) the credit was fully settled.
4. Take the rolling window: **last 5 resolved credits OR credits
   resolved within the last 12 months, whichever gives more data**.
   Prevents low-volume farmers being stuck on ancient events and
   reflects present-day behaviour honestly for high-volume ones.
5. Trust score = arithmetic mean of weights × 100.

Currently-open overdue credits are a separate live warning — they
don't dilute the historical score but are surfaced alongside it.

OPENING_BALANCE + ADJUSTMENT_UP entries increase debt (and can be
retired by payments) but never contribute to the score directly —
they have no agreed due_date and reflect starting-state or
correction rather than a fresh commitment.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.credit.models import (
    CreditEntry, CreditEntryStatus, CreditEntryType,
)


# ── Bucket weights + display thresholds (§10) ────────────────────────────

_WEIGHT_FULL_ON_TIME     = 1.0
_WEIGHT_PARTIAL_ON_TIME  = 0.5
_WEIGHT_LATE_LT30        = 0.3
_WEIGHT_LATE_GE30        = 0.2
_WEIGHT_UNPAID           = 0.0

_LATE_LT30_DAYS = 30

_ROLLING_MIN_COUNT      = 5
_ROLLING_WINDOW_DAYS    = 365
_MIN_RESOLVED_FOR_SCORE = 2

# Score-percentage → badge key thresholds (inclusive lower bounds).
# Keys match the TrustBadge Literal in schemas.py.
_BADGE_THRESHOLDS = [
    (85, "RELIABLE"),
    (65, "USUALLY_ON_TIME"),
    (45, "MIXED"),
    (25, "OFTEN_LATE"),
    (0,  "UNRELIABLE"),
]


_DEBT_INCREASING = {
    CreditEntryType.OPENING_BALANCE.value,
    CreditEntryType.CREDIT_ADVANCED.value,
    CreditEntryType.ADJUSTMENT_UP.value,
}
_DEBT_DECREASING = {
    CreditEntryType.PAYMENT_MADE.value,
    CreditEntryType.ADJUSTMENT_DOWN.value,
}


@dataclass
class TrustResult:
    """Return shape — matches the TrustScore Pydantic schema field-for-field."""
    score_pct: Optional[int]
    badge: str
    resolved_credits_used: int
    open_overdue_count: int
    open_overdue_total_paise: int
    oldest_overdue_days: Optional[int]  # extra: used by dealer portfolio row


def _bucket_badge(pct: int) -> str:
    for lo, badge in _BADGE_THRESHOLDS:
        if pct >= lo:
            return badge
    return "UNRELIABLE"


def _classify_credit_weight(
    principal: int, remaining_at_end: int,
    paid_by_due_paise: int, fully_paid_date: Optional[date],
    due_date: date,
) -> float:
    """Pure function over one credit's retirement history."""
    # Full settlement on or before due date.
    if fully_paid_date is not None and fully_paid_date <= due_date:
        return _WEIGHT_FULL_ON_TIME
    # Partial ≥ 50% by due date — regardless of what happens later.
    if paid_by_due_paise * 2 >= principal:  # >= 50% (integer-safe)
        return _WEIGHT_PARTIAL_ON_TIME
    # Full settlement after due date.
    if fully_paid_date is not None:
        days_late = (fully_paid_date - due_date).days
        return _WEIGHT_LATE_LT30 if days_late < _LATE_LT30_DAYS else _WEIGHT_LATE_GE30
    # < 50% by due date AND never fully settled.
    return _WEIGHT_UNPAID


def _compute_from_entries(
    entries: list[CreditEntry], *, today: date,
) -> TrustResult:
    """Pure function — tests can construct entries and call directly."""
    confirmed = [
        e for e in entries if e.status == CreditEntryStatus.CONFIRMED.value
    ]
    # FIFO order — oldest entry_date first, tiebreak by created_at.
    debts = sorted(
        [e for e in confirmed if e.entry_type in _DEBT_INCREASING],
        key=lambda e: (e.entry_date, e.created_at),
    )
    payments = sorted(
        [e for e in confirmed if e.entry_type in _DEBT_DECREASING],
        key=lambda e: (e.entry_date, e.created_at),
    )
    # Voided entries are already excluded (status != CONFIRMED).

    # Per-debt remaining + retirement history.
    remaining: list[int] = [e.amount_paise for e in debts]
    # retirement[i] = list of (payment_date, contribution_paise)
    retirement: list[list[tuple[date, int]]] = [[] for _ in debts]

    for pay in payments:
        pay_amount = pay.amount_paise
        for i in range(len(debts)):
            if pay_amount <= 0:
                break
            if remaining[i] <= 0:
                continue
            take = min(remaining[i], pay_amount)
            remaining[i] -= take
            pay_amount -= take
            retirement[i].append((pay.entry_date, take))
        # Any overpayment (pay_amount > 0 at end) is quietly discarded
        # for this calc — it doesn't retire any additional credit.

    # Score-eligible credits: CREDIT_ADVANCED with a due_date that are
    # either fully paid or past due.
    resolutions: list[tuple[date, float]] = []  # (resolved_at, weight)
    for i, credit in enumerate(debts):
        if credit.entry_type != CreditEntryType.CREDIT_ADVANCED.value:
            continue
        if credit.due_date is None:
            continue  # defensive — CREDIT_ADVANCED should always have due_date
        principal = credit.amount_paise
        fully_paid_date: Optional[date] = None
        if remaining[i] == 0 and retirement[i]:
            fully_paid_date = max(pd for pd, _ in retirement[i])
        # Not yet resolved — still open, still within due date.
        if fully_paid_date is None and today <= credit.due_date:
            continue
        paid_by_due = sum(
            amt for pd, amt in retirement[i] if pd <= credit.due_date
        )
        weight = _classify_credit_weight(
            principal=principal,
            remaining_at_end=remaining[i],
            paid_by_due_paise=paid_by_due,
            fully_paid_date=fully_paid_date,
            due_date=credit.due_date,
        )
        resolved_at = fully_paid_date if fully_paid_date else credit.due_date
        resolutions.append((resolved_at, weight))

    # Rolling window pick: last-5 OR last-12mo, whichever gives more.
    resolutions.sort(key=lambda t: t[0], reverse=True)
    cutoff = today - timedelta(days=_ROLLING_WINDOW_DAYS)
    within_window = [(d, w) for d, w in resolutions if d >= cutoff]
    last_n = resolutions[:_ROLLING_MIN_COUNT]
    used = within_window if len(within_window) > len(last_n) else last_n

    # Open overdue counts (independent of score).
    open_overdue_count = 0
    open_overdue_total = 0
    oldest_overdue_days: Optional[int] = None
    for i, credit in enumerate(debts):
        if credit.entry_type != CreditEntryType.CREDIT_ADVANCED.value:
            continue
        if credit.due_date is None:
            continue
        if credit.due_date < today and remaining[i] > 0:
            open_overdue_count += 1
            open_overdue_total += remaining[i]
            days_over = (today - credit.due_date).days
            if oldest_overdue_days is None or days_over > oldest_overdue_days:
                oldest_overdue_days = days_over

    if len(used) < _MIN_RESOLVED_FOR_SCORE:
        return TrustResult(
            score_pct=None,
            badge="NEW_NO_HISTORY",
            resolved_credits_used=len(used),
            open_overdue_count=open_overdue_count,
            open_overdue_total_paise=open_overdue_total,
            oldest_overdue_days=oldest_overdue_days,
        )

    avg = sum(w for _, w in used) / len(used)
    pct = round(avg * 100)
    return TrustResult(
        score_pct=pct,
        badge=_bucket_badge(pct),
        resolved_credits_used=len(used),
        open_overdue_count=open_overdue_count,
        open_overdue_total_paise=open_overdue_total,
        oldest_overdue_days=oldest_overdue_days,
    )


async def compute_trust_for_account(
    db: AsyncSession, account_id: str, *, today: Optional[date] = None,
) -> TrustResult:
    if today is None:
        today = date.today()
    entries = list((await db.execute(
        select(CreditEntry).where(CreditEntry.account_id == account_id)
    )).scalars().all())
    return _compute_from_entries(entries, today=today)
