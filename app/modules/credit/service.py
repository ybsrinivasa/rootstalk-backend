"""Credit Management System — service layer.

Owns:
- Account resolution (auto-create on first use; enforce 1:1 pair).
- Entry state machine (PROPOSED → CONFIRMED / DISPUTED / VOIDED)
  and the strict immutability rule (CONFIRMED entries never change;
  corrections go through paired-void).
- Per-type entry_date + due_date editability rules
  (CREDIT_ADVANCED locked to today; OPENING_BALANCE / PAYMENT_MADE
  editable while PROPOSED).
- Balance math — signed sum over CONFIRMED entries, computed on read
  (no denormalised balance column, no drift).
- Coaching sandbox isolation — every account op is refused if the
  dealer/farmer pair crosses a coaching-workspace boundary.

Notifications hooks are placeholders — the actual push dispatch lives
in `notifications.py` (v1.0 immediate triggers) and celery tasks
(v1.1 daily/weekly summaries).
"""
from datetime import date, datetime, timezone, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.coaching.service import get_coaching_student_for_user
from app.modules.credit.models import (
    CreditAccount, CreditEntry, CreditEntryStatus, CreditEntryType,
    CreditReminderPref, InitiatorParty, PaymentMethod, new_uuid, utcnow,
)
from app.modules.platform.models import User


# ── Signed contribution per entry_type ────────────────────────────────────
# Convention: positive means "farmer owes dealer more". Same axis from
# both parties' perspective — dealer sees +ve as receivable, farmer sees
# +ve as debt. Both parties look at the same signed number.

_ENTRY_SIGN: dict[str, int] = {
    CreditEntryType.OPENING_BALANCE.value: +1,
    CreditEntryType.CREDIT_ADVANCED.value: +1,
    CreditEntryType.ADJUSTMENT_UP.value:   +1,
    CreditEntryType.PAYMENT_MADE.value:    -1,
    CreditEntryType.ADJUSTMENT_DOWN.value: -1,
    # VOID entries contribute 0 by themselves — they flip a TARGET
    # entry to VOIDED status, which removes that target from the sum.
    CreditEntryType.VOID.value:             0,
}


def _signed_amount(entry: CreditEntry) -> int:
    return _ENTRY_SIGN.get(entry.entry_type, 0) * entry.amount_paise


# ── Coaching isolation guard ─────────────────────────────────────────────

async def guard_coaching_credit_account(
    db: AsyncSession, dealer_user_id: str, farmer_user_id: str,
) -> None:
    """Refuse credit ops that cross a coaching-workspace boundary.

    Coaching students practise all roles as themselves — the only valid
    coaching credit account has dealer_user_id == farmer_user_id ==
    student.user_id. Any real dealer paired with a coaching-student
    farmer (or vice versa) leaks a coaching identity into the real
    world; refused.

    Also refuses dealer_user_id == farmer_user_id for real (non-
    coaching) users — a real dealer can't hold credit against themself.
    """
    if dealer_user_id == farmer_user_id:
        student = await get_coaching_student_for_user(db, dealer_user_id)
        if student is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "dealer_farmer_must_differ",
                    "message": "Dealer and farmer must be different users.",
                },
            )
        return  # coaching self-play — allowed

    dealer_cs = await get_coaching_student_for_user(db, dealer_user_id)
    farmer_cs = await get_coaching_student_for_user(db, farmer_user_id)
    if dealer_cs is None and farmer_cs is None:
        return  # both real — normal
    raise HTTPException(
        status_code=403,
        detail={
            "code": "coaching_credit_isolation_violated",
            "message": (
                "Credit accounts inside a coaching workspace must be "
                "between the student and themselves. Real-world dealer/"
                "farmer identities cannot be mixed into coaching practice."
            ),
        },
    )


# ── Account lookup + auto-create ─────────────────────────────────────────

async def get_account_by_pair(
    db: AsyncSession, dealer_user_id: str, farmer_user_id: str,
) -> Optional[CreditAccount]:
    return (await db.execute(
        select(CreditAccount).where(
            CreditAccount.dealer_user_id == dealer_user_id,
            CreditAccount.farmer_user_id == farmer_user_id,
        )
    )).scalar_one_or_none()


async def get_account_or_404(db: AsyncSession, account_id: str) -> CreditAccount:
    account = await db.get(CreditAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Credit account not found")
    return account


async def get_or_create_account(
    db: AsyncSession, *,
    dealer_user_id: str, farmer_user_id: str, opened_by: str,
) -> CreditAccount:
    """Return existing account for the pair or create a new one.

    Callers must have already run `guard_coaching_credit_account`.
    Idempotent — safe to call from multiple entry-creation paths.
    """
    account = await get_account_by_pair(db, dealer_user_id, farmer_user_id)
    if account is not None:
        return account
    account = CreditAccount(
        id=new_uuid(),
        dealer_user_id=dealer_user_id,
        farmer_user_id=farmer_user_id,
        opened_at=utcnow(),
        opened_by=opened_by,
        is_active=True,
    )
    db.add(account)
    await db.flush()
    return account


def account_party_for(account: CreditAccount, user_id: str) -> str:
    """Return DEALER or FARMER for a user_id known to belong to the
    account. Raises 403 for outsiders — the router's auth check should
    have already caught this; this is defense-in-depth."""
    if user_id == account.dealer_user_id:
        return InitiatorParty.DEALER.value
    if user_id == account.farmer_user_id:
        return InitiatorParty.FARMER.value
    raise HTTPException(
        status_code=403,
        detail="You are not a party to this credit account.",
    )


def other_party_user_id(account: CreditAccount, user_id: str) -> str:
    if user_id == account.dealer_user_id:
        return account.farmer_user_id
    if user_id == account.farmer_user_id:
        return account.dealer_user_id
    raise HTTPException(
        status_code=403,
        detail="You are not a party to this credit account.",
    )


# ── Entry lookup ─────────────────────────────────────────────────────────

async def get_entry_or_404(db: AsyncSession, entry_id: str) -> CreditEntry:
    entry = await db.get(CreditEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Credit entry not found")
    return entry


async def get_entry_scoped(
    db: AsyncSession, entry_id: str, user_id: str,
) -> tuple[CreditEntry, CreditAccount, str]:
    """Fetch an entry + its account, verifying the caller is a party.
    Returns (entry, account, caller_party). 404 conflated with 403 so
    outsiders can't enumerate entry ids."""
    entry = await get_entry_or_404(db, entry_id)
    account = await get_account_or_404(db, entry.account_id)
    if user_id != account.dealer_user_id and user_id != account.farmer_user_id:
        # Conflate with not-found — don't leak existence to strangers.
        raise HTTPException(status_code=404, detail="Credit entry not found")
    caller_party = account_party_for(account, user_id)
    return entry, account, caller_party


# ── Balance math ─────────────────────────────────────────────────────────

async def compute_confirmed_balance_paise(
    db: AsyncSession, account_id: str,
) -> int:
    """Signed sum over CONFIRMED entries. Positive = farmer owes dealer.
    VOIDED entries are excluded by status filter; they don't count."""
    rows = (await db.execute(
        select(CreditEntry.entry_type, CreditEntry.amount_paise)
        .where(
            CreditEntry.account_id == account_id,
            CreditEntry.status == CreditEntryStatus.CONFIRMED.value,
        )
    )).all()
    total = 0
    for etype, amount in rows:
        total += _ENTRY_SIGN.get(etype, 0) * (amount or 0)
    return total


async def compute_pending_signed_paise_awaiting(
    db: AsyncSession, account_id: str, *, awaiting_party: str,
) -> int:
    """Sum of signed amounts across PROPOSED entries whose confirmer
    is `awaiting_party`. Used by both the dealer + farmer to see
    "what would land in my balance if I confirmed everything pending".
    """
    # An entry needing PARTY's confirm = initiated_by != PARTY.
    initiator_is = (
        InitiatorParty.FARMER.value if awaiting_party == InitiatorParty.DEALER.value
        else InitiatorParty.DEALER.value
    )
    rows = (await db.execute(
        select(CreditEntry.entry_type, CreditEntry.amount_paise)
        .where(
            CreditEntry.account_id == account_id,
            CreditEntry.status == CreditEntryStatus.PROPOSED.value,
            CreditEntry.initiated_by == initiator_is,
        )
    )).all()
    total = 0
    for etype, amount in rows:
        total += _ENTRY_SIGN.get(etype, 0) * (amount or 0)
    return total


async def compute_balance(
    db: AsyncSession, account_id: str, *, viewer_party: str,
) -> dict:
    """Return the three-number balance snapshot for the viewer.
    Sign convention is uniform (positive = farmer owes dealer)."""
    confirmed = await compute_confirmed_balance_paise(db, account_id)
    other = (
        InitiatorParty.FARMER.value if viewer_party == InitiatorParty.DEALER.value
        else InitiatorParty.DEALER.value
    )
    pending_your = await compute_pending_signed_paise_awaiting(
        db, account_id, awaiting_party=viewer_party,
    )
    pending_theirs = await compute_pending_signed_paise_awaiting(
        db, account_id, awaiting_party=other,
    )
    return {
        "confirmed_paise": confirmed,
        "pending_your_confirm_paise": pending_your,
        "pending_their_confirm_paise": pending_theirs,
    }


# ── Editability rules (per entry_type) ────────────────────────────────────

_EDITABLE_FIELDS_BY_TYPE: dict[str, set[str]] = {
    # CREDIT_ADVANCED: entry_date locked to today; can change amount,
    # due_date, note (dealer edits before farmer's confirm).
    CreditEntryType.CREDIT_ADVANCED.value: {
        "amount_paise", "due_date", "initiator_note",
    },
    # OPENING_BALANCE: the "as of" date IS editable — dealer may
    # backdate to reflect when the pre-RootsTalk book was last
    # squared. Amount + note also editable.
    CreditEntryType.OPENING_BALANCE.value: {
        "amount_paise", "entry_date", "initiator_note",
    },
    # PAYMENT_MADE: initiator may correct the amount, when they paid
    # (entry_date), the method, ref, receipt photo, and their note.
    CreditEntryType.PAYMENT_MADE.value: {
        "amount_paise", "entry_date", "payment_method",
        "payment_ref", "receipt_media_id", "initiator_note",
    },
    # Adjustments + VOID are ephemeral corrections and don't support
    # in-place edits — withdraw and re-propose if wrong.
    CreditEntryType.ADJUSTMENT_UP.value:   set(),
    CreditEntryType.ADJUSTMENT_DOWN.value: set(),
    CreditEntryType.VOID.value:            set(),
}


# ── Entry creation ───────────────────────────────────────────────────────

async def create_opening_balance(
    db: AsyncSession, *,
    dealer_user_id: str, farmer_user_id: str, initiator_user_id: str,
    amount_paise: int, as_of_date: date,
    initiator_note: Optional[str] = None,
) -> CreditEntry:
    """Only-once per account. Refuses if a CONFIRMED opening balance
    already exists (§7.5 rule)."""
    await guard_coaching_credit_account(db, dealer_user_id, farmer_user_id)
    account = await get_or_create_account(
        db, dealer_user_id=dealer_user_id, farmer_user_id=farmer_user_id,
        opened_by=InitiatorParty.DEALER.value,
    )
    # Any prior opening balance (PROPOSED or CONFIRMED) blocks another.
    existing = (await db.execute(
        select(CreditEntry.id).where(
            CreditEntry.account_id == account.id,
            CreditEntry.entry_type == CreditEntryType.OPENING_BALANCE.value,
            CreditEntry.status.in_([
                CreditEntryStatus.PROPOSED.value,
                CreditEntryStatus.CONFIRMED.value,
            ]),
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "opening_balance_already_exists",
                "message": (
                    "This account already has an opening balance. "
                    "Only one opening balance is allowed per account."
                ),
            },
        )
    entry = CreditEntry(
        id=new_uuid(),
        account_id=account.id,
        entry_type=CreditEntryType.OPENING_BALANCE.value,
        amount_paise=amount_paise,
        entry_date=as_of_date,
        due_date=None,
        initiated_by=InitiatorParty.DEALER.value,
        initiator_user_id=initiator_user_id,
        status=CreditEntryStatus.PROPOSED.value,
        initiator_note=initiator_note,
    )
    db.add(entry)
    await db.flush()
    return entry


async def create_credit_advanced(
    db: AsyncSession, *,
    dealer_user_id: str, farmer_user_id: str, initiator_user_id: str,
    amount_paise: int, due_date: date,
    initiator_note: Optional[str] = None,
    related_sale_id: Optional[str] = None,
) -> CreditEntry:
    """`entry_date` is always today — never accepted from caller.
    `due_date` must be today or later."""
    await guard_coaching_credit_account(db, dealer_user_id, farmer_user_id)
    today = date.today()
    if due_date < today:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "due_date_in_past",
                "message": "Settle-by date must be today or later.",
            },
        )
    account = await get_or_create_account(
        db, dealer_user_id=dealer_user_id, farmer_user_id=farmer_user_id,
        opened_by=InitiatorParty.DEALER.value,
    )
    entry = CreditEntry(
        id=new_uuid(),
        account_id=account.id,
        entry_type=CreditEntryType.CREDIT_ADVANCED.value,
        amount_paise=amount_paise,
        entry_date=today,
        due_date=due_date,
        initiated_by=InitiatorParty.DEALER.value,
        initiator_user_id=initiator_user_id,
        status=CreditEntryStatus.PROPOSED.value,
        initiator_note=initiator_note,
        related_sale_id=related_sale_id,
    )
    db.add(entry)
    await db.flush()
    return entry


# Sanity guard: don't accept payments claimed to have been made more
# than a year ago; a legitimate late-record still lands within a year.
_MAX_PAYMENT_BACKDATE_DAYS = 365


async def create_payment(
    db: AsyncSession, *,
    dealer_user_id: str, farmer_user_id: str,
    initiator_user_id: str, initiator_party: str,
    amount_paise: int, entry_date: date, payment_method: str,
    payment_ref: Optional[str] = None,
    receipt_media_id: Optional[str] = None,
    initiator_note: Optional[str] = None,
) -> CreditEntry:
    await guard_coaching_credit_account(db, dealer_user_id, farmer_user_id)
    today = date.today()
    if entry_date > today:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "payment_date_in_future",
                "message": "Payment date cannot be in the future.",
            },
        )
    if (today - entry_date).days > _MAX_PAYMENT_BACKDATE_DAYS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "payment_date_too_old",
                "message": "Payment date is more than a year in the past.",
            },
        )
    account = await get_or_create_account(
        db, dealer_user_id=dealer_user_id, farmer_user_id=farmer_user_id,
        opened_by=initiator_party,
    )
    entry = CreditEntry(
        id=new_uuid(),
        account_id=account.id,
        entry_type=CreditEntryType.PAYMENT_MADE.value,
        amount_paise=amount_paise,
        entry_date=entry_date,
        due_date=None,
        initiated_by=initiator_party,
        initiator_user_id=initiator_user_id,
        status=CreditEntryStatus.PROPOSED.value,
        payment_method=payment_method,
        payment_ref=payment_ref,
        receipt_media_id=receipt_media_id,
        initiator_note=initiator_note,
    )
    db.add(entry)
    await db.flush()
    return entry


async def propose_void(
    db: AsyncSession, *,
    target_entry: CreditEntry, initiator_user_id: str, initiator_party: str,
    reason: str,
) -> CreditEntry:
    """Paired-void: initiator proposes a VOID entry that references the
    target via `initiator_note` (encoded so history can reconstruct
    the pair). When the other party CONFIRMS the VOID, the TARGET
    entry's status flips to VOIDED (removed from balance) — handled
    in `confirm_entry`.
    """
    if target_entry.status != CreditEntryStatus.CONFIRMED.value:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "void_target_not_confirmed",
                "message": (
                    "Only confirmed entries can be voided. To undo a "
                    "pending entry, ask the initiator to withdraw it."
                ),
            },
        )
    account = await get_account_or_404(db, target_entry.account_id)
    # Refuse duplicate void proposals — only one live VOID proposal
    # per target at a time.
    dup = (await db.execute(
        select(CreditEntry.id).where(
            CreditEntry.account_id == account.id,
            CreditEntry.entry_type == CreditEntryType.VOID.value,
            CreditEntry.status == CreditEntryStatus.PROPOSED.value,
            CreditEntry.payment_ref == target_entry.id,
        )
    )).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "void_already_proposed",
                "message": "A void is already awaiting the other party's confirmation for this entry.",
            },
        )
    void_entry = CreditEntry(
        id=new_uuid(),
        account_id=account.id,
        entry_type=CreditEntryType.VOID.value,
        # Amount mirrors the target so the audit list surfaces "void of ₹X".
        amount_paise=target_entry.amount_paise,
        entry_date=date.today(),
        due_date=None,
        initiated_by=initiator_party,
        initiator_user_id=initiator_user_id,
        status=CreditEntryStatus.PROPOSED.value,
        # Re-use payment_ref to encode the target entry_id pointer —
        # avoids a schema change for a rare op. Fine because VOID
        # entries never carry a payment ref of their own.
        payment_ref=target_entry.id,
        dispute_reason=reason,
    )
    db.add(void_entry)
    await db.flush()
    return void_entry


# ── Entry state transitions ─────────────────────────────────────────────

async def edit_proposed_entry(
    db: AsyncSession, *,
    entry: CreditEntry, initiator_user_id: str, updates: dict,
) -> CreditEntry:
    """Initiator-only edits on PROPOSED entries. Per-type editability
    is enforced against `_EDITABLE_FIELDS_BY_TYPE`. Unknown or non-
    editable fields are refused (422) rather than silently ignored —
    surfaces schema mistakes early."""
    if entry.status != CreditEntryStatus.PROPOSED.value:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "entry_not_editable",
                "message": (
                    "This entry can no longer be edited. Confirmed "
                    "entries are immutable; propose a void or adjustment "
                    "instead."
                ),
            },
        )
    if entry.initiator_user_id != initiator_user_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "not_entry_initiator",
                "message": "Only the entry's initiator can edit it while pending confirmation.",
            },
        )
    allowed = _EDITABLE_FIELDS_BY_TYPE.get(entry.entry_type, set())
    illegal = set(updates.keys()) - allowed
    if illegal:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "fields_not_editable_for_type",
                "message": (
                    f"Cannot edit fields {sorted(illegal)} on a "
                    f"{entry.entry_type} entry."
                ),
            },
        )
    # Apply.
    if "amount_paise" in updates:
        amt = updates["amount_paise"]
        if amt is None or amt <= 0:
            raise HTTPException(status_code=422, detail="amount_paise must be a positive integer")
        entry.amount_paise = amt
    if "due_date" in updates and updates["due_date"] is not None:
        if updates["due_date"] < date.today():
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "due_date_in_past",
                    "message": "Settle-by date must be today or later.",
                },
            )
        entry.due_date = updates["due_date"]
    if "entry_date" in updates and updates["entry_date"] is not None:
        ed = updates["entry_date"]
        # Reuse the payment backdate window for opening balance too.
        today = date.today()
        if ed > today:
            raise HTTPException(status_code=422, detail="entry_date cannot be in the future")
        if (today - ed).days > _MAX_PAYMENT_BACKDATE_DAYS:
            raise HTTPException(status_code=422, detail="entry_date is more than a year in the past")
        entry.entry_date = ed
    if "initiator_note" in updates:
        entry.initiator_note = updates["initiator_note"]
    if "payment_method" in updates:
        entry.payment_method = updates["payment_method"]
    if "payment_ref" in updates:
        entry.payment_ref = updates["payment_ref"]
    if "receipt_media_id" in updates:
        entry.receipt_media_id = updates["receipt_media_id"]
    entry.updated_at = utcnow()
    await db.flush()
    return entry


async def confirm_entry(
    db: AsyncSession, *,
    entry: CreditEntry, confirmer_user_id: str, confirmer_party: str,
    confirmer_note: Optional[str] = None,
) -> CreditEntry:
    """Confirmer must be the counter-party (NOT the initiator).
    On CONFIRMED, the entry becomes immutable. If the entry is a
    VOID, also flip the target entry to VOIDED."""
    if entry.status != CreditEntryStatus.PROPOSED.value:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "entry_not_pending",
                "message": "This entry is no longer awaiting confirmation.",
            },
        )
    if entry.initiated_by == confirmer_party:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "cannot_self_confirm",
                "message": "The other party must confirm entries you initiate.",
            },
        )
    entry.status = CreditEntryStatus.CONFIRMED.value
    entry.confirmed_at = utcnow()
    entry.confirmer_user_id = confirmer_user_id
    entry.confirmer_note = confirmer_note
    entry.updated_at = utcnow()

    # Paired-void cascade: if this is a VOID entry confirmation, flip
    # the target (encoded in payment_ref) to VOIDED so it drops out
    # of the balance sum.
    if entry.entry_type == CreditEntryType.VOID.value and entry.payment_ref:
        target = await db.get(CreditEntry, entry.payment_ref)
        if target is not None and target.status == CreditEntryStatus.CONFIRMED.value:
            target.status = CreditEntryStatus.VOIDED.value
            target.voided_at = utcnow()
            target.updated_at = utcnow()
    await db.flush()
    return entry


async def dispute_entry(
    db: AsyncSession, *,
    entry: CreditEntry, confirmer_user_id: str, confirmer_party: str,
    dispute_reason: str,
) -> CreditEntry:
    if entry.status != CreditEntryStatus.PROPOSED.value:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "entry_not_pending",
                "message": "This entry is no longer awaiting confirmation.",
            },
        )
    if entry.initiated_by == confirmer_party:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "cannot_self_dispute",
                "message": "Only the other party can dispute an entry you initiated.",
            },
        )
    entry.status = CreditEntryStatus.DISPUTED.value
    entry.dispute_reason = dispute_reason
    # `confirmer_user_id` here captures WHO disputed (still the party
    # who was expected to confirm). `confirmed_at` remains NULL —
    # a DISPUTED entry never reached the confirmed state.
    entry.confirmer_user_id = confirmer_user_id
    entry.updated_at = utcnow()
    await db.flush()
    return entry


async def withdraw_proposed_entry(
    db: AsyncSession, *,
    entry: CreditEntry, initiator_user_id: str,
) -> None:
    """Initiator withdraws their own PROPOSED entry (typo / mistake)
    or their own DISPUTED entry (counter-proposal will follow).
    Hard-deletes — no history retention for these transient states."""
    if entry.status not in (
        CreditEntryStatus.PROPOSED.value, CreditEntryStatus.DISPUTED.value,
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "entry_not_withdrawable",
                "message": (
                    "Only pending or disputed entries can be withdrawn. "
                    "Confirmed entries must be voided via the other party."
                ),
            },
        )
    if entry.initiator_user_id != initiator_user_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "not_entry_initiator",
                "message": "Only the entry's initiator can withdraw it.",
            },
        )
    await db.execute(
        delete(CreditEntry).where(CreditEntry.id == entry.id)
    )
    await db.flush()


# ── Reminder prefs (lazy create) ─────────────────────────────────────────

async def get_or_create_reminder_pref(
    db: AsyncSession, user_id: str,
) -> CreditReminderPref:
    pref = await db.get(CreditReminderPref, user_id)
    if pref is not None:
        return pref
    pref = CreditReminderPref(user_id=user_id)
    db.add(pref)
    await db.flush()
    return pref


# ── Freshness marker helper (for the farmer's UI) ───────────────────────

_PROPOSED_EDIT_MARKER_WINDOW = timedelta(seconds=60)


def was_edited_after_proposal(entry: CreditEntry) -> bool:
    """True when a PROPOSED entry has been touched > 60s after its
    initial create — used to render "Last updated HH:MM" so the
    farmer's Confirm tap always reflects the current state."""
    if entry.status != CreditEntryStatus.PROPOSED.value:
        return False
    if entry.created_at is None or entry.updated_at is None:
        return False
    return (entry.updated_at - entry.created_at) > _PROPOSED_EDIT_MARKER_WINDOW


# ── Bulk listing (used by both dealer + farmer per-account detail) ──────

async def list_entries_for_account(
    db: AsyncSession, account_id: str,
) -> list[CreditEntry]:
    return list((await db.execute(
        select(CreditEntry)
        .where(CreditEntry.account_id == account_id)
        .order_by(
            CreditEntry.entry_date.desc(), CreditEntry.created_at.desc(),
        )
    )).scalars().all())


# ── Overdue helpers (used by trust score + portfolio views) ─────────────

async def list_open_overdue_credits(
    db: AsyncSession, account_id: str, *, today: Optional[date] = None,
) -> list[CreditEntry]:
    """CONFIRMED CREDIT_ADVANCED entries whose due_date is past AND
    whose payment coverage hasn't fully retired them yet.

    Coverage is tracked per-account, not per-credit — we don't earmark
    payments to specific credits. So "unretired" here is a rough proxy:
    if the account's confirmed balance is > 0, any confirmed credit
    past its due_date is considered still open. Callers that need
    per-credit resolution should walk the entry list themselves; this
    helper is for the trust-score + portfolio overdue counters where
    the coarse view is fine.
    """
    if today is None:
        today = date.today()
    confirmed_balance = await compute_confirmed_balance_paise(db, account_id)
    if confirmed_balance <= 0:
        return []
    return list((await db.execute(
        select(CreditEntry)
        .where(
            CreditEntry.account_id == account_id,
            CreditEntry.status == CreditEntryStatus.CONFIRMED.value,
            CreditEntry.entry_type == CreditEntryType.CREDIT_ADVANCED.value,
            CreditEntry.due_date < today,
        )
        .order_by(CreditEntry.due_date)
    )).scalars().all())
