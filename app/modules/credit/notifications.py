"""Credit Management — FCM push helpers for the three immediate
state-change triggers (§9 first three rows). Daily digest + weekly
summary jobs are v1.1 (celery beat) and live elsewhere.

**English-hardcoded copy** — participates in the broader deferred
project `project_rootstalk_backend_push_i18n_deferred.md`. When that
lands, the templates below move into `PUSH_TEMPLATES` and pick per
`user.language_code`. Not fixing per-module.

All helpers are fire-and-forget — silently no-op if the recipient
doesn't have an fcm_token registered. Never raise; the caller's
DB transaction has already committed and a push failure must not
un-do it.

Copy discipline (per scoping §9 + design principle §2.2): factual,
not judgmental. We describe what happened; we don't editorialise
about who's right or how the reader should feel.
"""
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.credit.models import (
    CreditAccount, CreditEntry, CreditEntryType, InitiatorParty,
)
from app.modules.platform.models import User
from app.services.fcm_service import send_fcm


logger = logging.getLogger(__name__)


def _rupees(paise: int) -> str:
    """Bare rupee-formatting for push copy. `Intl.NumberFormat` on
    the client handles the locale-aware version; here we just need
    a compact string."""
    rupees = paise // 100
    # Indian grouping: 1,23,456 (last three, then twos).
    s = str(rupees)
    if len(s) <= 3:
        return f"₹{s}"
    head, tail = s[:-3], s[-3:]
    # Insert commas every 2 digits from the right in `head`.
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return f"₹{','.join(parts)},{tail}"


def _entry_type_label(entry_type: str) -> str:
    return {
        CreditEntryType.OPENING_BALANCE.value: "opening balance",
        CreditEntryType.CREDIT_ADVANCED.value: "credit",
        CreditEntryType.PAYMENT_MADE.value:    "payment",
        CreditEntryType.ADJUSTMENT_UP.value:   "adjustment",
        CreditEntryType.ADJUSTMENT_DOWN.value: "adjustment",
        CreditEntryType.VOID.value:            "void",
    }.get(entry_type, "entry")


async def _load_users(
    db: AsyncSession, account: CreditAccount,
) -> tuple[Optional[User], Optional[User]]:
    """(dealer, farmer) — either may be None on a corrupted row."""
    users = (await db.execute(
        select(User).where(User.id.in_([account.dealer_user_id, account.farmer_user_id]))
    )).scalars().all()
    dealer = next((u for u in users if u.id == account.dealer_user_id), None)
    farmer = next((u for u in users if u.id == account.farmer_user_id), None)
    return dealer, farmer


def _click_action(entry: CreditEntry, account: CreditAccount, viewer_party: str) -> str:
    """Deep-link the receiver into the right per-account detail page."""
    if viewer_party == InitiatorParty.DEALER.value:
        return f"/dealer/credit/farmers/{account.farmer_user_id}"
    return f"/farmer/credit/dealers/{account.dealer_user_id}"


async def push_entry_proposed(
    db: AsyncSession, entry: CreditEntry, account: CreditAccount,
) -> None:
    """Notify the counterparty (the one who must confirm) about a
    freshly created PROPOSED entry."""
    dealer, farmer = await _load_users(db, account)
    recipient_party = (
        InitiatorParty.FARMER.value
        if entry.initiated_by == InitiatorParty.DEALER.value
        else InitiatorParty.DEALER.value
    )
    recipient = farmer if recipient_party == InitiatorParty.FARMER.value else dealer
    if recipient is None or not recipient.fcm_token:
        return

    initiator_name = (dealer.name if entry.initiated_by == InitiatorParty.DEALER.value
                      else farmer.name) or "The other party"
    amount = _rupees(entry.amount_paise)
    etype = _entry_type_label(entry.entry_type)

    if entry.entry_type == CreditEntryType.CREDIT_ADVANCED.value and entry.due_date:
        title = f"New credit from {initiator_name}"
        body = f"{amount} recorded. Settle by {entry.due_date.strftime('%d %b')}. Tap to confirm."
    elif entry.entry_type == CreditEntryType.PAYMENT_MADE.value:
        title = f"{initiator_name} recorded a payment"
        body = f"{amount} payment. Tap to confirm."
    elif entry.entry_type == CreditEntryType.OPENING_BALANCE.value:
        title = f"{initiator_name} entered an opening balance"
        body = f"{amount} as of {entry.entry_date.strftime('%d %b %Y')}. Tap to confirm."
    elif entry.entry_type == CreditEntryType.VOID.value:
        title = f"{initiator_name} wants to void an entry"
        body = f"Void of {amount}. Tap to review."
    else:
        title = f"{initiator_name} added a new {etype}"
        body = f"{amount}. Tap to confirm."

    try:
        await send_fcm(
            token=recipient.fcm_token,
            title=title,
            body=body,
            data={
                "type": "CREDIT_ENTRY_PROPOSED",
                "entry_id": entry.id,
                "account_id": account.id,
                "click_action": _click_action(entry, account, recipient_party),
            },
        )
    except Exception as exc:
        logger.warning("credit push_entry_proposed failed: %s", exc)


async def push_entry_confirmed(
    db: AsyncSession, entry: CreditEntry, account: CreditAccount,
) -> None:
    """Notify the initiator that their entry was accepted."""
    dealer, farmer = await _load_users(db, account)
    initiator = dealer if entry.initiated_by == InitiatorParty.DEALER.value else farmer
    confirmer = farmer if entry.initiated_by == InitiatorParty.DEALER.value else dealer
    if initiator is None or not initiator.fcm_token:
        return
    initiator_party = entry.initiated_by
    confirmer_name = (confirmer.name if confirmer else None) or "The other party"
    amount = _rupees(entry.amount_paise)
    etype = _entry_type_label(entry.entry_type)

    try:
        await send_fcm(
            token=initiator.fcm_token,
            title=f"{confirmer_name} confirmed",
            body=f"The {amount} {etype} was confirmed.",
            data={
                "type": "CREDIT_ENTRY_CONFIRMED",
                "entry_id": entry.id,
                "account_id": account.id,
                "click_action": _click_action(entry, account, initiator_party),
            },
        )
    except Exception as exc:
        logger.warning("credit push_entry_confirmed failed: %s", exc)


async def push_entry_disputed(
    db: AsyncSession, entry: CreditEntry, account: CreditAccount,
) -> None:
    """Notify the initiator that their entry was flagged."""
    dealer, farmer = await _load_users(db, account)
    initiator = dealer if entry.initiated_by == InitiatorParty.DEALER.value else farmer
    disputer = farmer if entry.initiated_by == InitiatorParty.DEALER.value else dealer
    if initiator is None or not initiator.fcm_token:
        return
    initiator_party = entry.initiated_by
    disputer_name = (disputer.name if disputer else None) or "The other party"
    amount = _rupees(entry.amount_paise)
    etype = _entry_type_label(entry.entry_type)

    try:
        await send_fcm(
            token=initiator.fcm_token,
            title=f"{disputer_name} flagged an entry",
            body=f"The {amount} {etype} needs review. Tap to see why.",
            data={
                "type": "CREDIT_ENTRY_DISPUTED",
                "entry_id": entry.id,
                "account_id": account.id,
                "click_action": _click_action(entry, account, initiator_party),
            },
        )
    except Exception as exc:
        logger.warning("credit push_entry_disputed failed: %s", exc)
