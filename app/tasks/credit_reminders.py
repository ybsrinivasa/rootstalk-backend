"""CMS v1.1 — daily digest + weekly summary celery tasks.

Three scheduled tasks; each fans out FCM pushes to eligible recipients.
No SMS variant — CMS is app-only by design.

Schedule (see celery_app.py beat_schedule):
- `dispatch_dealer_daily_digest`      — daily 14:30 UTC = 20:00 IST
- `dispatch_dealer_weekly_overdue`    — Monday 02:30 UTC = 08:00 IST
- `dispatch_farmer_weekly_summary`    — Sunday 02:30 UTC = 08:00 IST

Copy is factual, not judgmental (§9 + design principle §2.2). All copy
is English-hardcoded; participates in the broader deferred backend push
i18n project.

Preference gating: `CreditReminderPref.daily_summary_enabled` for the
daily digest; `CreditReminderPref.weekly_summary_enabled` for both
weekly tasks. Missing pref rows are treated as opted-in (defaults are
True). Quiet hours are not enforced in v1.1 — tasks fire at consistent
IST times and the whole cohort is on IST for now.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.credit.models import (
    CreditAccount, CreditEntry, CreditEntryStatus, CreditEntryType,
    CreditReminderPref, InitiatorParty,
)
from app.modules.credit.notifications import _rupees
from app.modules.credit.trust_score import compute_trust_for_account
from app.modules.platform.models import User
from app.services.fcm_service import send_fcm


logger = logging.getLogger(__name__)


# ── Celery entry points ──────────────────────────────────────────────────

@celery_app.task
def dispatch_dealer_daily_digest() -> dict:
    return asyncio.run(_run_dealer_daily_digest())


@celery_app.task
def dispatch_dealer_weekly_overdue() -> dict:
    return asyncio.run(_run_dealer_weekly_overdue())


@celery_app.task
def dispatch_farmer_weekly_summary() -> dict:
    return asyncio.run(_run_farmer_weekly_summary())


# ── Shared helper ────────────────────────────────────────────────────────

async def _pref_allows(
    db: AsyncSession, user_id: str, flag: str,
) -> bool:
    """Check the boolean pref flag. Missing pref row = defaults apply
    (all enabled — see model defaults)."""
    pref = await db.get(CreditReminderPref, user_id)
    if pref is None:
        return True
    return bool(getattr(pref, flag, True))


# ── Daily digest (dealer) ────────────────────────────────────────────────

async def _run_dealer_daily_digest() -> dict:
    stats = {"dealers_scanned": 0, "pushes_sent": 0, "skipped_silent": 0}
    async with AsyncSessionLocal() as db:
        today = date.today()
        tomorrow = today + timedelta(days=1)

        dealer_ids = [r[0] for r in (await db.execute(
            select(CreditAccount.dealer_user_id).distinct()
            .where(CreditAccount.is_active.is_(True))
        )).all()]

        for dealer_id in dealer_ids:
            stats["dealers_scanned"] += 1
            if not await _pref_allows(db, dealer_id, "daily_summary_enabled"):
                continue
            body = await _compose_dealer_daily_body(db, dealer_id, today, tomorrow)
            if body is None:
                # Nothing to report — no push. Avoids "you have 0 things
                # to do" spam that would train dealers to ignore the tab.
                stats["skipped_silent"] += 1
                continue
            dealer = await db.get(User, dealer_id)
            if dealer is None or not dealer.fcm_token:
                continue
            try:
                await send_fcm(
                    token=dealer.fcm_token,
                    title="Today in your credit book",
                    body=body,
                    data={
                        "type": "CREDIT_DAILY_DIGEST",
                        "click_action": "/dealer/credit",
                    },
                )
                stats["pushes_sent"] += 1
            except Exception as exc:
                logger.warning(
                    "dispatch_dealer_daily_digest: push failed for %s: %s",
                    dealer_id, exc,
                )
    logger.info("dispatch_dealer_daily_digest done: %s", stats)
    return stats


async def _compose_dealer_daily_body(
    db: AsyncSession, dealer_id: str, today: date, tomorrow: date,
) -> Optional[str]:
    account_ids = [r[0] for r in (await db.execute(
        select(CreditAccount.id).where(
            CreditAccount.dealer_user_id == dealer_id,
            CreditAccount.is_active.is_(True),
        )
    )).all()]
    if not account_ids:
        return None

    # (1) Payments confirmed today (any direction — farmer paid or
    # dealer-recorded farmer-confirmed). Use confirmed_at::date == today.
    row = (await db.execute(
        select(
            func.count(CreditEntry.id),
            func.coalesce(func.sum(CreditEntry.amount_paise), 0),
        ).where(
            CreditEntry.account_id.in_(account_ids),
            CreditEntry.entry_type == CreditEntryType.PAYMENT_MADE.value,
            CreditEntry.status == CreditEntryStatus.CONFIRMED.value,
            func.date(CreditEntry.confirmed_at) == today,
        )
    )).first()
    payments_count, payments_total = (row or (0, 0))

    # (2) Entries needing dealer's confirm
    pending_count = (await db.execute(
        select(func.count(CreditEntry.id)).where(
            CreditEntry.account_id.in_(account_ids),
            CreditEntry.status == CreditEntryStatus.PROPOSED.value,
            CreditEntry.initiated_by == InitiatorParty.FARMER.value,
        )
    )).scalar() or 0

    # (3) Credits due tomorrow (that are still CONFIRMED — a resolved
    # credit has status flipping to VOIDED via paired-void, so filter
    # by status is sufficient; retirement isn't tracked at column level).
    due_tomorrow_count = (await db.execute(
        select(func.count(CreditEntry.id)).where(
            CreditEntry.account_id.in_(account_ids),
            CreditEntry.entry_type == CreditEntryType.CREDIT_ADVANCED.value,
            CreditEntry.status == CreditEntryStatus.CONFIRMED.value,
            CreditEntry.due_date == tomorrow,
        )
    )).scalar() or 0

    # (4) Overdue count — use trust_score's FIFO-retirement-aware
    # open_overdue count for accuracy. O(accounts) but fine for daily
    # digest scale.
    overdue_count = 0
    for aid in account_ids:
        trust = await compute_trust_for_account(db, aid, today=today)
        overdue_count += trust.open_overdue_count

    if payments_count == 0 and pending_count == 0 and due_tomorrow_count == 0 and overdue_count == 0:
        return None

    parts: list[str] = []
    if payments_count > 0:
        parts.append(f"{payments_count} payment{'s' if payments_count != 1 else ''} received ({_rupees(payments_total)})")
    if pending_count > 0:
        parts.append(f"{pending_count} need{'s' if pending_count == 1 else ''} your confirmation")
    if due_tomorrow_count > 0:
        parts.append(f"{due_tomorrow_count} due tomorrow")
    if overdue_count > 0:
        parts.append(f"{overdue_count} overdue")
    return ". ".join(parts) + "."


# ── Weekly overdue nudge (dealer) ────────────────────────────────────────

async def _run_dealer_weekly_overdue() -> dict:
    stats = {"dealers_scanned": 0, "pushes_sent": 0, "skipped_silent": 0}
    async with AsyncSessionLocal() as db:
        today = date.today()
        past_60_cutoff = today - timedelta(days=60)

        dealer_ids = [r[0] for r in (await db.execute(
            select(CreditAccount.dealer_user_id).distinct()
            .where(CreditAccount.is_active.is_(True))
        )).all()]

        for dealer_id in dealer_ids:
            stats["dealers_scanned"] += 1
            if not await _pref_allows(db, dealer_id, "weekly_summary_enabled"):
                continue
            body = await _compose_dealer_weekly_body(db, dealer_id, today, past_60_cutoff)
            if body is None:
                stats["skipped_silent"] += 1
                continue
            dealer = await db.get(User, dealer_id)
            if dealer is None or not dealer.fcm_token:
                continue
            try:
                await send_fcm(
                    token=dealer.fcm_token,
                    title="Weekly overdue summary",
                    body=body,
                    data={
                        "type": "CREDIT_WEEKLY_DEALER",
                        "click_action": "/dealer/credit",
                    },
                )
                stats["pushes_sent"] += 1
            except Exception as exc:
                logger.warning(
                    "dispatch_dealer_weekly_overdue: push failed for %s: %s",
                    dealer_id, exc,
                )
    logger.info("dispatch_dealer_weekly_overdue done: %s", stats)
    return stats


async def _compose_dealer_weekly_body(
    db: AsyncSession, dealer_id: str, today: date, past_60_cutoff: date,
) -> Optional[str]:
    accounts = list((await db.execute(
        select(CreditAccount).where(
            CreditAccount.dealer_user_id == dealer_id,
            CreditAccount.is_active.is_(True),
        )
    )).scalars().all())
    if not accounts:
        return None

    total_outstanding = 0
    farmers_with_outstanding = 0
    farmers_past_60 = 0
    for account in accounts:
        trust = await compute_trust_for_account(db, account.id, today=today)
        # Use confirmed balance for the total.
        from app.modules.credit.service import compute_confirmed_balance_paise
        confirmed = await compute_confirmed_balance_paise(db, account.id)
        if confirmed > 0:
            total_outstanding += confirmed
            farmers_with_outstanding += 1
        if trust.oldest_overdue_days is not None and trust.oldest_overdue_days >= 60:
            farmers_past_60 += 1

    if farmers_with_outstanding == 0:
        return None

    body = f"{farmers_with_outstanding} farmer{'s' if farmers_with_outstanding != 1 else ''} owe you {_rupees(total_outstanding)} total."
    if farmers_past_60 > 0:
        body += f" {farmers_past_60} {'are' if farmers_past_60 != 1 else 'is'} past 60 days."
    return body


# ── Weekly summary (farmer) ──────────────────────────────────────────────

async def _run_farmer_weekly_summary() -> dict:
    stats = {"farmers_scanned": 0, "pushes_sent": 0, "skipped_silent": 0}
    async with AsyncSessionLocal() as db:
        today = date.today()
        soon_cutoff = today + timedelta(days=7)

        farmer_ids = [r[0] for r in (await db.execute(
            select(CreditAccount.farmer_user_id).distinct()
            .where(CreditAccount.is_active.is_(True))
        )).all()]

        for farmer_id in farmer_ids:
            stats["farmers_scanned"] += 1
            if not await _pref_allows(db, farmer_id, "weekly_summary_enabled"):
                continue
            body = await _compose_farmer_weekly_body(db, farmer_id, today, soon_cutoff)
            if body is None:
                stats["skipped_silent"] += 1
                continue
            farmer = await db.get(User, farmer_id)
            if farmer is None or not farmer.fcm_token:
                continue
            try:
                await send_fcm(
                    token=farmer.fcm_token,
                    title="Your weekly credit summary",
                    body=body,
                    data={
                        "type": "CREDIT_WEEKLY_FARMER",
                        "click_action": "/credit",
                    },
                )
                stats["pushes_sent"] += 1
            except Exception as exc:
                logger.warning(
                    "dispatch_farmer_weekly_summary: push failed for %s: %s",
                    farmer_id, exc,
                )
    logger.info("dispatch_farmer_weekly_summary done: %s", stats)
    return stats


async def _compose_farmer_weekly_body(
    db: AsyncSession, farmer_id: str, today: date, soon_cutoff: date,
) -> Optional[str]:
    from app.modules.credit.service import compute_confirmed_balance_paise
    accounts = list((await db.execute(
        select(CreditAccount).where(
            CreditAccount.farmer_user_id == farmer_id,
            CreditAccount.is_active.is_(True),
        )
    )).scalars().all())
    if not accounts:
        return None

    total_owed = 0
    dealer_count = 0
    upcoming_lines: list[tuple[int, str]] = []  # (days_out, line)
    overdue_lines: list[tuple[int, str]] = []
    dealer_ids = [a.dealer_user_id for a in accounts]
    dealer_names_by_id: dict[str, str] = {}
    if dealer_ids:
        rows = (await db.execute(
            select(User.id, User.name).where(User.id.in_(dealer_ids))
        )).all()
        dealer_names_by_id = {r[0]: r[1] for r in rows}

    for account in accounts:
        confirmed = await compute_confirmed_balance_paise(db, account.id)
        if confirmed > 0:
            total_owed += confirmed
            dealer_count += 1
        # Upcoming: CONFIRMED CREDIT_ADVANCED with due_date in [today, today+7]
        # AND account still has non-zero balance (i.e. not fully settled).
        # For "not fully settled" we rely on confirmed balance > 0 (rough
        # per-account proxy — enough for a weekly nudge).
        if confirmed <= 0:
            continue
        upcoming = (await db.execute(
            select(CreditEntry).where(
                CreditEntry.account_id == account.id,
                CreditEntry.entry_type == CreditEntryType.CREDIT_ADVANCED.value,
                CreditEntry.status == CreditEntryStatus.CONFIRMED.value,
                CreditEntry.due_date > today,
                CreditEntry.due_date <= soon_cutoff,
            ).order_by(CreditEntry.due_date)
        )).scalars().all()
        for c in upcoming:
            days_out = (c.due_date - today).days
            dealer_name = dealer_names_by_id.get(account.dealer_user_id) or "a dealer"
            upcoming_lines.append((days_out,
                f"{_rupees(c.amount_paise)} to {dealer_name} due in {days_out} day{'s' if days_out != 1 else ''}"))
        overdue = (await db.execute(
            select(CreditEntry).where(
                CreditEntry.account_id == account.id,
                CreditEntry.entry_type == CreditEntryType.CREDIT_ADVANCED.value,
                CreditEntry.status == CreditEntryStatus.CONFIRMED.value,
                CreditEntry.due_date < today,
            ).order_by(CreditEntry.due_date)
        )).scalars().all()
        for c in overdue:
            days_over = (today - c.due_date).days
            dealer_name = dealer_names_by_id.get(account.dealer_user_id) or "a dealer"
            overdue_lines.append((days_over,
                f"{_rupees(c.amount_paise)} to {dealer_name} is {days_over} days overdue"))

    if total_owed == 0 and not upcoming_lines and not overdue_lines:
        return None

    # Keep the body compact — one summary line + up to two highlights.
    lines = [f"You owe {_rupees(total_owed)} across {dealer_count} dealer{'s' if dealer_count != 1 else ''}."]
    # Prefer the most urgent overdue then the soonest upcoming.
    overdue_lines.sort(key=lambda t: -t[0])
    upcoming_lines.sort(key=lambda t: t[0])
    for _, line in overdue_lines[:1]:
        lines.append(line + ".")
    for _, line in upcoming_lines[:1]:
        lines.append(line + ".")
    return " ".join(lines)
