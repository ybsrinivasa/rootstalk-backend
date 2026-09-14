"""Credit Management System (CMS) — HTTP endpoints.

Three URL prefixes:

- `/dealer/credit/*` — dealer-facing (portfolio + per-farmer detail +
  dealer-initiated entries). Requires the caller to hold an ACTIVE
  DEALER role. Every query is scoped to `dealer_user_id = current_user.id`.
- `/farmer/credit/*` — farmer-facing (portfolio + per-dealer detail +
  farmer-initiated entries). Any authenticated user can call these —
  farmer isn't a formal role, just "whoever the dealer records credit
  against".
- `/credit/entries/*` — symmetric entry-lifecycle ops (edit, confirm,
  dispute, withdraw, propose-void). Auth is inferred from user vs
  account: caller must be a party to the account that owns the entry.

The coaching sandbox is enforced inside the service layer via
`guard_coaching_credit_account`. Real dealers cannot open a credit
account against a coaching-student farmer (and vice versa); coaching
students play both sides as themselves within their own workspace.
"""
from datetime import date, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_roles
from app.modules.coaching.service import get_coaching_student_for_user
from app.modules.credit import notifications as credit_notifications
from app.modules.credit import service as credit_service
from app.modules.credit.models import (
    CreditAccount, CreditEntry, CreditEntryStatus, CreditEntryType,
    InitiatorParty,
)
from app.modules.credit.schemas import (
    AccountBalance, AccountDetail, ConfirmEntryRequest,
    CreateCreditRequest, CreateOpeningBalanceRequest, CreatePaymentRequest,
    DealerPortfolioResponse, DealerPortfolioRow, DisputeEntryRequest,
    EntryActionResponse, EntryOut, FarmerPortfolioResponse,
    FarmerPortfolioRow, ProposeVoidRequest, ReminderPrefOut,
    ReminderPrefUpdate, TrustScore, UpdateProposedEntryRequest,
)
from app.modules.credit.trust_score import compute_trust_for_account
from app.modules.orders.models import DealerProfile
from app.modules.platform.models import RoleType, User


router = APIRouter(tags=["Credit Management"])

require_dealer = require_roles(RoleType.DEALER)


# ── Serialisation helper ─────────────────────────────────────────────────

def _entry_to_out(entry: CreditEntry) -> EntryOut:
    return EntryOut(
        id=entry.id,
        entry_type=entry.entry_type,
        amount_paise=entry.amount_paise,
        entry_date=entry.entry_date,
        due_date=entry.due_date,
        initiated_by=entry.initiated_by,
        initiator_user_id=entry.initiator_user_id,
        status=entry.status,
        related_sale_id=entry.related_sale_id,
        payment_method=entry.payment_method,
        payment_ref=entry.payment_ref,
        receipt_media_id=entry.receipt_media_id,
        initiator_note=entry.initiator_note,
        confirmer_note=entry.confirmer_note,
        dispute_reason=entry.dispute_reason,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        confirmed_at=entry.confirmed_at,
        confirmer_user_id=entry.confirmer_user_id,
        voided_at=entry.voided_at,
        was_edited_after_proposal=credit_service.was_edited_after_proposal(entry),
    )


async def _load_balance(
    db: AsyncSession, account_id: str, viewer_party: str,
) -> AccountBalance:
    b = await credit_service.compute_balance(
        db, account_id, viewer_party=viewer_party,
    )
    return AccountBalance(**b)


async def _load_trust(db: AsyncSession, account_id: str) -> TrustScore:
    r = await compute_trust_for_account(db, account_id)
    return TrustScore(
        score_pct=r.score_pct,
        badge=r.badge,
        resolved_credits_used=r.resolved_credits_used,
        open_overdue_count=r.open_overdue_count,
        open_overdue_total_paise=r.open_overdue_total_paise,
    )


# ════════════════════════════════════════════════════════════════════════
# DEALER-FACING ENDPOINTS
# ════════════════════════════════════════════════════════════════════════

@router.get("/dealer/credit", response_model=DealerPortfolioResponse)
async def dealer_portfolio(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    """Dealer's portfolio home — one row per farmer with outstanding
    credit. Includes overdue buckets aggregated across all farmers."""
    accounts = list((await db.execute(
        select(CreditAccount).where(
            CreditAccount.dealer_user_id == current_user.id,
            CreditAccount.is_active.is_(True),
        )
    )).scalars().all())

    # Preload farmer users to avoid N+1.
    farmer_ids = [a.farmer_user_id for a in accounts]
    farmers_by_id: dict[str, User] = {}
    if farmer_ids:
        farmers_by_id = {
            u.id: u for u in (await db.execute(
                select(User).where(User.id.in_(farmer_ids))
            )).scalars().all()
        }

    rows: list[DealerPortfolioRow] = []
    total = 0
    b0_30 = b31_60 = b61_90 = b90p = 0
    for account in accounts:
        confirmed = await credit_service.compute_confirmed_balance_paise(db, account.id)
        pending_count = (await db.execute(
            select(CreditEntry.id).where(
                CreditEntry.account_id == account.id,
                CreditEntry.status == CreditEntryStatus.PROPOSED.value,
                CreditEntry.initiated_by == InitiatorParty.FARMER.value,
            )
        )).all()
        trust = await compute_trust_for_account(db, account.id)
        farmer = farmers_by_id.get(account.farmer_user_id)
        rows.append(DealerPortfolioRow(
            farmer_user_id=account.farmer_user_id,
            farmer_name=farmer.name if farmer else None,
            farmer_phone=farmer.phone if farmer else None,
            farmer_photo_url=getattr(farmer, "photo_url", None) if farmer else None,
            confirmed_balance_paise=confirmed,
            pending_confirm_count=len(pending_count),
            oldest_overdue_days=trust.oldest_overdue_days,
            trust=TrustScore(
                score_pct=trust.score_pct,
                badge=trust.badge,
                resolved_credits_used=trust.resolved_credits_used,
                open_overdue_count=trust.open_overdue_count,
                open_overdue_total_paise=trust.open_overdue_total_paise,
            ),
        ))
        if confirmed > 0:
            total += confirmed
            od = trust.oldest_overdue_days
            if od is None:
                b0_30 += confirmed  # not overdue → counts in 0-30 by convention
            elif od <= 30:
                b0_30 += confirmed
            elif od <= 60:
                b31_60 += confirmed
            elif od <= 90:
                b61_90 += confirmed
            else:
                b90p += confirmed

    # Sort: overdue-days desc (nulls last), then amount desc.
    rows.sort(key=lambda r: (
        -(r.oldest_overdue_days if r.oldest_overdue_days is not None else -1),
        -r.confirmed_balance_paise,
    ))

    return DealerPortfolioResponse(
        rows=rows,
        total_outstanding_paise=total,
        bucket_0_30_paise=b0_30,
        bucket_31_60_paise=b31_60,
        bucket_61_90_paise=b61_90,
        bucket_90_plus_paise=b90p,
    )


@router.get(
    "/dealer/credit/farmers/{farmer_user_id}",
    response_model=AccountDetail,
)
async def dealer_account_detail(
    farmer_user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    """Per-farmer account detail from the dealer's perspective."""
    account = await credit_service.get_account_by_pair(
        db, dealer_user_id=current_user.id, farmer_user_id=farmer_user_id,
    )
    if account is None:
        # No account yet — return an empty shell so the UI can render
        # the "Enter opening balance" prompt without a second call.
        farmer = await db.get(User, farmer_user_id)
        dealer_profile = (await db.execute(
            select(DealerProfile).where(DealerProfile.user_id == current_user.id)
        )).scalar_one_or_none()
        return AccountDetail(
            account_id="",
            dealer_user_id=current_user.id,
            farmer_user_id=farmer_user_id,
            counterparty_name=farmer.name if farmer else None,
            counterparty_phone=farmer.phone if farmer else None,
            counterparty_photo_url=getattr(farmer, "photo_url", None) if farmer else None,
            dealer_upi_vpa=dealer_profile.upi_vpa if dealer_profile else None,
            dealer_upi_display_name=(
                dealer_profile.payment_display_name or dealer_profile.shop_name
            ) if dealer_profile else None,
            balance=AccountBalance(
                confirmed_paise=0,
                pending_your_confirm_paise=0,
                pending_their_confirm_paise=0,
            ),
            trust=None,
            entries=[],
            can_add_opening_balance=True,
            is_active=True,
        )

    entries = await credit_service.list_entries_for_account(db, account.id)
    balance = await _load_balance(db, account.id, InitiatorParty.DEALER.value)
    trust = await _load_trust(db, account.id)
    farmer = await db.get(User, farmer_user_id)
    dealer_profile = (await db.execute(
        select(DealerProfile).where(DealerProfile.user_id == current_user.id)
    )).scalar_one_or_none()

    # Opening balance is one-and-done — hide the button if any
    # PROPOSED / CONFIRMED opening balance exists.
    has_opening = any(
        e.entry_type == CreditEntryType.OPENING_BALANCE.value
        and e.status in (CreditEntryStatus.PROPOSED.value, CreditEntryStatus.CONFIRMED.value)
        for e in entries
    )

    return AccountDetail(
        account_id=account.id,
        dealer_user_id=account.dealer_user_id,
        farmer_user_id=account.farmer_user_id,
        counterparty_name=farmer.name if farmer else None,
        counterparty_phone=farmer.phone if farmer else None,
        counterparty_photo_url=getattr(farmer, "photo_url", None) if farmer else None,
        dealer_upi_vpa=dealer_profile.upi_vpa if dealer_profile else None,
        dealer_upi_display_name=(
            dealer_profile.payment_display_name or dealer_profile.shop_name
        ) if dealer_profile else None,
        balance=balance,
        trust=trust,
        entries=[_entry_to_out(e) for e in entries],
        can_add_opening_balance=not has_opening,
        is_active=account.is_active,
    )


@router.post(
    "/dealer/credit/opening-balance",
    response_model=EntryActionResponse, status_code=201,
)
async def dealer_create_opening_balance(
    request: CreateOpeningBalanceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    """§7.5 — dealer records a lump-sum opening balance. One per
    account, ever. Farmer confirms once → folds the entire pre-
    RootsTalk history into a single confirmed number."""
    entry = await credit_service.create_opening_balance(
        db,
        dealer_user_id=current_user.id,
        farmer_user_id=request.farmer_user_id,
        initiator_user_id=current_user.id,
        amount_paise=request.amount_paise,
        as_of_date=request.as_of_date,
        initiator_note=request.initiator_note,
    )
    await db.commit()
    await db.refresh(entry)
    account = await credit_service.get_account_or_404(db, entry.account_id)
    await credit_notifications.push_entry_proposed(db, entry, account)
    balance = await _load_balance(db, entry.account_id, InitiatorParty.DEALER.value)
    return EntryActionResponse(entry=_entry_to_out(entry), balance=balance)


@router.post(
    "/dealer/credit/credit",
    response_model=EntryActionResponse, status_code=201,
)
async def dealer_create_credit(
    request: CreateCreditRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    """§7.1 — dealer records a new credit advance. `entry_date` is
    always today (not accepted from client); `due_date` is the
    agreed settle-by date."""
    entry = await credit_service.create_credit_advanced(
        db,
        dealer_user_id=current_user.id,
        farmer_user_id=request.farmer_user_id,
        initiator_user_id=current_user.id,
        amount_paise=request.amount_paise,
        due_date=request.due_date,
        initiator_note=request.initiator_note,
        related_sale_id=request.related_sale_id,
    )
    await db.commit()
    await db.refresh(entry)
    account = await credit_service.get_account_or_404(db, entry.account_id)
    await credit_notifications.push_entry_proposed(db, entry, account)
    balance = await _load_balance(db, entry.account_id, InitiatorParty.DEALER.value)
    return EntryActionResponse(entry=_entry_to_out(entry), balance=balance)


@router.post(
    "/dealer/credit/payment",
    response_model=EntryActionResponse, status_code=201,
)
async def dealer_create_payment(
    request: CreatePaymentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    """§7.4 — dealer records a payment they received in cash / other
    channel. Farmer confirms."""
    if not request.farmer_user_id:
        raise HTTPException(422, detail="farmer_user_id required for dealer-initiated payment")
    entry = await credit_service.create_payment(
        db,
        dealer_user_id=current_user.id,
        farmer_user_id=request.farmer_user_id,
        initiator_user_id=current_user.id,
        initiator_party=InitiatorParty.DEALER.value,
        amount_paise=request.amount_paise,
        entry_date=request.entry_date,
        payment_method=request.payment_method,
        payment_ref=request.payment_ref,
        receipt_media_id=request.receipt_media_id,
        initiator_note=request.initiator_note,
    )
    await db.commit()
    await db.refresh(entry)
    account = await credit_service.get_account_or_404(db, entry.account_id)
    await credit_notifications.push_entry_proposed(db, entry, account)
    balance = await _load_balance(db, entry.account_id, InitiatorParty.DEALER.value)
    return EntryActionResponse(entry=_entry_to_out(entry), balance=balance)


# ════════════════════════════════════════════════════════════════════════
# FARMER-FACING ENDPOINTS
# ════════════════════════════════════════════════════════════════════════

@router.get("/farmer/credit", response_model=FarmerPortfolioResponse)
async def farmer_portfolio(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer's portfolio — one row per dealer with outstanding debt."""
    accounts = list((await db.execute(
        select(CreditAccount).where(
            CreditAccount.farmer_user_id == current_user.id,
            CreditAccount.is_active.is_(True),
        )
    )).scalars().all())

    dealer_ids = [a.dealer_user_id for a in accounts]
    dealers_by_id: dict[str, User] = {}
    dealer_profiles_by_id: dict[str, DealerProfile] = {}
    if dealer_ids:
        dealers_by_id = {
            u.id: u for u in (await db.execute(
                select(User).where(User.id.in_(dealer_ids))
            )).scalars().all()
        }
        dealer_profiles_by_id = {
            dp.user_id: dp for dp in (await db.execute(
                select(DealerProfile).where(DealerProfile.user_id.in_(dealer_ids))
            )).scalars().all()
        }

    rows: list[FarmerPortfolioRow] = []
    total = 0
    for account in accounts:
        confirmed = await credit_service.compute_confirmed_balance_paise(db, account.id)
        pending_count = (await db.execute(
            select(CreditEntry.id).where(
                CreditEntry.account_id == account.id,
                CreditEntry.status == CreditEntryStatus.PROPOSED.value,
                CreditEntry.initiated_by == InitiatorParty.DEALER.value,
            )
        )).all()
        trust = await compute_trust_for_account(db, account.id)
        dealer = dealers_by_id.get(account.dealer_user_id)
        dp = dealer_profiles_by_id.get(account.dealer_user_id)
        rows.append(FarmerPortfolioRow(
            dealer_user_id=account.dealer_user_id,
            dealer_name=dealer.name if dealer else None,
            dealer_phone=dealer.phone if dealer else None,
            shop_name=dp.shop_name if dp else None,
            shop_address=dp.shop_address if dp else None,
            confirmed_balance_paise=confirmed,
            pending_confirm_count=len(pending_count),
            oldest_overdue_days=trust.oldest_overdue_days,
            dealer_upi_vpa=dp.upi_vpa if dp else None,
            dealer_upi_display_name=(
                dp.payment_display_name or dp.shop_name
            ) if dp else None,
        ))
        if confirmed > 0:
            total += confirmed

    rows.sort(key=lambda r: (
        -(r.oldest_overdue_days if r.oldest_overdue_days is not None else -1),
        -r.confirmed_balance_paise,
    ))
    return FarmerPortfolioResponse(rows=rows, total_owed_paise=total)


@router.get(
    "/farmer/credit/dealers/{dealer_user_id}",
    response_model=AccountDetail,
)
async def farmer_account_detail(
    dealer_user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Per-dealer account detail from the farmer's perspective. Trust
    score is deliberately omitted — dealer-only visibility (§10)."""
    account = await credit_service.get_account_by_pair(
        db, dealer_user_id=dealer_user_id, farmer_user_id=current_user.id,
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Credit account not found")

    entries = await credit_service.list_entries_for_account(db, account.id)
    balance = await _load_balance(db, account.id, InitiatorParty.FARMER.value)
    dealer = await db.get(User, dealer_user_id)
    dp = (await db.execute(
        select(DealerProfile).where(DealerProfile.user_id == dealer_user_id)
    )).scalar_one_or_none()

    return AccountDetail(
        account_id=account.id,
        dealer_user_id=account.dealer_user_id,
        farmer_user_id=account.farmer_user_id,
        counterparty_name=dealer.name if dealer else None,
        counterparty_phone=dealer.phone if dealer else None,
        counterparty_photo_url=getattr(dealer, "photo_url", None) if dealer else None,
        dealer_upi_vpa=dp.upi_vpa if dp else None,
        dealer_upi_display_name=(
            dp.payment_display_name or dp.shop_name
        ) if dp else None,
        balance=balance,
        trust=None,   # never shown to farmer
        entries=[_entry_to_out(e) for e in entries],
        can_add_opening_balance=False,  # dealer-only capability
        is_active=account.is_active,
    )


@router.post(
    "/farmer/credit/payment",
    response_model=EntryActionResponse, status_code=201,
)
async def farmer_create_payment(
    request: CreatePaymentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """§7.3 — farmer records a payment they made. Dealer confirms."""
    if not request.dealer_user_id:
        raise HTTPException(422, detail="dealer_user_id required for farmer-initiated payment")
    entry = await credit_service.create_payment(
        db,
        dealer_user_id=request.dealer_user_id,
        farmer_user_id=current_user.id,
        initiator_user_id=current_user.id,
        initiator_party=InitiatorParty.FARMER.value,
        amount_paise=request.amount_paise,
        entry_date=request.entry_date,
        payment_method=request.payment_method,
        payment_ref=request.payment_ref,
        receipt_media_id=request.receipt_media_id,
        initiator_note=request.initiator_note,
    )
    await db.commit()
    await db.refresh(entry)
    account = await credit_service.get_account_or_404(db, entry.account_id)
    await credit_notifications.push_entry_proposed(db, entry, account)
    balance = await _load_balance(db, entry.account_id, InitiatorParty.FARMER.value)
    return EntryActionResponse(entry=_entry_to_out(entry), balance=balance)


# ════════════════════════════════════════════════════════════════════════
# SYMMETRIC ENTRY-LIFECYCLE ENDPOINTS
# ════════════════════════════════════════════════════════════════════════

@router.patch(
    "/credit/entries/{entry_id}",
    response_model=EntryActionResponse,
)
async def edit_entry(
    entry_id: str,
    request: UpdateProposedEntryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Initiator-only edit while PROPOSED. Per-type field editability
    enforced in the service layer."""
    entry, account, caller_party = await credit_service.get_entry_scoped(
        db, entry_id, current_user.id,
    )
    updates = request.model_dump(exclude_unset=True)
    entry = await credit_service.edit_proposed_entry(
        db, entry=entry, initiator_user_id=current_user.id, updates=updates,
    )
    await db.commit()
    await db.refresh(entry)
    balance = await _load_balance(db, entry.account_id, caller_party)
    return EntryActionResponse(entry=_entry_to_out(entry), balance=balance)


@router.post(
    "/credit/entries/{entry_id}/confirm",
    response_model=EntryActionResponse,
)
async def confirm_entry(
    entry_id: str,
    request: ConfirmEntryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Counterparty confirms a PROPOSED entry → CONFIRMED (immutable)."""
    entry, account, caller_party = await credit_service.get_entry_scoped(
        db, entry_id, current_user.id,
    )
    entry = await credit_service.confirm_entry(
        db, entry=entry,
        confirmer_user_id=current_user.id, confirmer_party=caller_party,
        confirmer_note=request.confirmer_note,
    )
    await db.commit()
    await db.refresh(entry)
    await credit_notifications.push_entry_confirmed(db, entry, account)
    balance = await _load_balance(db, entry.account_id, caller_party)
    return EntryActionResponse(entry=_entry_to_out(entry), balance=balance)


@router.post(
    "/credit/entries/{entry_id}/dispute",
    response_model=EntryActionResponse,
)
async def dispute_entry(
    entry_id: str,
    request: DisputeEntryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Counterparty disputes a PROPOSED entry with a reason."""
    entry, account, caller_party = await credit_service.get_entry_scoped(
        db, entry_id, current_user.id,
    )
    entry = await credit_service.dispute_entry(
        db, entry=entry,
        confirmer_user_id=current_user.id, confirmer_party=caller_party,
        dispute_reason=request.dispute_reason,
    )
    await db.commit()
    await db.refresh(entry)
    await credit_notifications.push_entry_disputed(db, entry, account)
    balance = await _load_balance(db, entry.account_id, caller_party)
    return EntryActionResponse(entry=_entry_to_out(entry), balance=balance)


@router.delete(
    "/credit/entries/{entry_id}", status_code=204,
)
async def withdraw_entry(
    entry_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Initiator withdraws own PROPOSED / DISPUTED entry (hard delete)."""
    entry, _account, _caller_party = await credit_service.get_entry_scoped(
        db, entry_id, current_user.id,
    )
    await credit_service.withdraw_proposed_entry(
        db, entry=entry, initiator_user_id=current_user.id,
    )
    await db.commit()


@router.post(
    "/credit/entries/{entry_id}/propose-void",
    response_model=EntryActionResponse, status_code=201,
)
async def propose_void(
    entry_id: str,
    request: ProposeVoidRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Either party can propose voiding a CONFIRMED entry. Creates a
    paired VOID entry that requires the other party's confirm; on
    confirm, the target entry flips to VOIDED (removed from balance)."""
    if request.target_entry_id != entry_id:
        raise HTTPException(422, detail="target_entry_id in body must match entry_id in URL")
    target, account, caller_party = await credit_service.get_entry_scoped(
        db, entry_id, current_user.id,
    )
    void_entry = await credit_service.propose_void(
        db, target_entry=target,
        initiator_user_id=current_user.id, initiator_party=caller_party,
        reason=request.reason,
    )
    await db.commit()
    await db.refresh(void_entry)
    await credit_notifications.push_entry_proposed(db, void_entry, account)
    balance = await _load_balance(db, void_entry.account_id, caller_party)
    return EntryActionResponse(entry=_entry_to_out(void_entry), balance=balance)


# ════════════════════════════════════════════════════════════════════════
# REMINDER PREFERENCES
# ════════════════════════════════════════════════════════════════════════

@router.get("/credit/prefs", response_model=ReminderPrefOut)
async def get_reminder_prefs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pref = await credit_service.get_or_create_reminder_pref(db, current_user.id)
    await db.commit()
    return ReminderPrefOut(
        daily_summary_enabled=pref.daily_summary_enabled,
        weekly_summary_enabled=pref.weekly_summary_enabled,
        new_entry_push_enabled=pref.new_entry_push_enabled,
        quiet_hours_start=pref.quiet_hours_start,
        quiet_hours_end=pref.quiet_hours_end,
    )


@router.patch("/credit/prefs", response_model=ReminderPrefOut)
async def update_reminder_prefs(
    request: ReminderPrefUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pref = await credit_service.get_or_create_reminder_pref(db, current_user.id)
    updates = request.model_dump(exclude_unset=True)
    for k, v in updates.items():
        setattr(pref, k, v)
    await db.commit()
    await db.refresh(pref)
    return ReminderPrefOut(
        daily_summary_enabled=pref.daily_summary_enabled,
        weekly_summary_enabled=pref.weekly_summary_enabled,
        new_entry_push_enabled=pref.new_entry_push_enabled,
        quiet_hours_start=pref.quiet_hours_start,
        quiet_hours_end=pref.quiet_hours_end,
    )
