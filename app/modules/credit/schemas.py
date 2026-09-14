"""Pydantic schemas for the Credit Management System endpoints.

Currency is exchanged in **paise** (integer) at the API boundary —
the client formats via `Intl.NumberFormat` for display. No floats.
"""
from datetime import date, datetime, time
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── Entry-shape aliases for readability ──────────────────────────────

EntryType = Literal[
    "OPENING_BALANCE", "CREDIT_ADVANCED", "PAYMENT_MADE",
    "ADJUSTMENT_UP", "ADJUSTMENT_DOWN", "VOID",
]
EntryStatus = Literal["PROPOSED", "CONFIRMED", "DISPUTED", "VOIDED"]
InitiatorParty = Literal["DEALER", "FARMER"]
PaymentMethod = Literal["CASH", "UPI", "BANK", "CHEQUE", "OTHER"]
TrustBadge = Literal[
    "RELIABLE", "USUALLY_ON_TIME", "MIXED",
    "OFTEN_LATE", "UNRELIABLE", "NEW_NO_HISTORY",
]


# ── Entry read shape (returned by both dealer + farmer detail pages) ─

class EntryOut(BaseModel):
    id: str
    entry_type: EntryType
    amount_paise: int
    entry_date: date
    due_date: Optional[date] = None
    initiated_by: InitiatorParty
    initiator_user_id: str
    status: EntryStatus
    related_sale_id: Optional[str] = None
    payment_method: Optional[PaymentMethod] = None
    payment_ref: Optional[str] = None
    receipt_media_id: Optional[str] = None
    initiator_note: Optional[str] = None
    confirmer_note: Optional[str] = None
    dispute_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    confirmed_at: Optional[datetime] = None
    confirmer_user_id: Optional[str] = None
    voided_at: Optional[datetime] = None
    # Marker for the farmer's UI so a mid-flight dealer edit surfaces.
    # True when a PROPOSED entry has been updated more than 60s after
    # its initial create — anchor for "Last updated at X" label.
    was_edited_after_proposal: bool = False


# ── Trust score ──────────────────────────────────────────────────────

class TrustScore(BaseModel):
    """Dealer-only. §10 spec."""
    score_pct: Optional[int] = None      # 0-100, or None when badge=NEW_NO_HISTORY
    badge: TrustBadge
    resolved_credits_used: int           # how many credits fed the score
    open_overdue_count: int = 0          # currently-open credits past due_date
    open_overdue_total_paise: int = 0    # sum of amount_paise for those


# ── Per-account balance snapshot ─────────────────────────────────────

class AccountBalance(BaseModel):
    confirmed_paise: int              # positive = farmer owes dealer this much
    pending_your_confirm_paise: int   # signed: positive if it would increase your net debt/receivable
    pending_their_confirm_paise: int  # the other party's PROPOSED entries you initiated


# ── Dealer portfolio (GET /dealer/credit) ────────────────────────────

class DealerPortfolioRow(BaseModel):
    farmer_user_id: str
    farmer_name: Optional[str] = None
    farmer_phone: Optional[str] = None
    farmer_photo_url: Optional[str] = None
    confirmed_balance_paise: int
    pending_confirm_count: int       # entries needing dealer's action
    oldest_overdue_days: Optional[int] = None  # 0+ if there's overdue open credit
    trust: TrustScore
    # True when the farmer has completed OTP self-registration on
    # RootsTalk (`User.self_registered_at IS NOT NULL`). False for
    # dealer-created unclaimed farmers who haven't installed / signed
    # in yet — those farmers won't see the credit in-app or receive
    # push notifications until they self-register. The UI surfaces this
    # as a badge on the farmer card + a banner on the per-farmer detail
    # page so the dealer knows the farmer's silence isn't disagreement.
    is_farmer_registered: bool = False


class DealerPortfolioResponse(BaseModel):
    rows: list[DealerPortfolioRow]
    total_outstanding_paise: int
    bucket_0_30_paise: int
    bucket_31_60_paise: int
    bucket_61_90_paise: int
    bucket_90_plus_paise: int


# ── Farmer portfolio (GET /farmer/credit) ────────────────────────────

class FarmerPortfolioRow(BaseModel):
    dealer_user_id: str
    dealer_name: Optional[str] = None
    dealer_phone: Optional[str] = None
    shop_name: Optional[str] = None
    shop_address: Optional[str] = None
    confirmed_balance_paise: int
    pending_confirm_count: int
    oldest_overdue_days: Optional[int] = None
    # Dealer's UPI id (from DealerProfile). Non-null → Copy-UPI-ID
    # button surfaces in the per-account payment flow. Farmer never
    # sees dealer's trust score for themselves — trust is dealer-only.
    dealer_upi_vpa: Optional[str] = None
    dealer_upi_display_name: Optional[str] = None


class FarmerPortfolioResponse(BaseModel):
    rows: list[FarmerPortfolioRow]
    total_owed_paise: int


# ── Per-account detail (dealer + farmer share the same shape) ───────

class AccountDetail(BaseModel):
    account_id: str
    dealer_user_id: str
    farmer_user_id: str
    # Denormalised counterparty display fields so the client doesn't
    # need a second round-trip. Field naming matches Farmer Ledger.
    counterparty_name: Optional[str] = None
    counterparty_phone: Optional[str] = None
    counterparty_photo_url: Optional[str] = None
    # Only populated when dealer views a farmer account
    dealer_upi_vpa: Optional[str] = None
    dealer_upi_display_name: Optional[str] = None
    balance: AccountBalance
    trust: Optional[TrustScore] = None   # dealer view only
    entries: list[EntryOut]
    can_add_opening_balance: bool        # dealer only + no confirmed OB yet
    is_active: bool
    # Populated in the dealer view. True when the farmer has completed
    # OTP self-registration; False for dealer-created unclaimed farmers.
    # NULL in the farmer view (a farmer looking at their own account
    # is by definition registered — the field carries no information).
    is_farmer_registered: Optional[bool] = None


# ── Create / edit entries ────────────────────────────────────────────

class CreateOpeningBalanceRequest(BaseModel):
    """Dealer-only. Auto-creates the account if it doesn't yet exist."""
    farmer_user_id: str
    amount_paise: int = Field(..., gt=0)
    as_of_date: date
    initiator_note: Optional[str] = None


class CreateCreditRequest(BaseModel):
    """Dealer-only. `entry_date` is implicitly today; not accepted from
    the client. `related_sale_id` optional — set when created via the
    On-credit checkbox on Add Sale."""
    farmer_user_id: str
    amount_paise: int = Field(..., gt=0)
    due_date: date
    initiator_note: Optional[str] = None
    related_sale_id: Optional[str] = None


class CreatePaymentRequest(BaseModel):
    """Either side can initiate. Counterparty confirms."""
    # For dealer view: farmer_user_id is required (which account).
    # For farmer view: dealer_user_id is required.
    farmer_user_id: Optional[str] = None
    dealer_user_id: Optional[str] = None
    amount_paise: int = Field(..., gt=0)
    entry_date: date
    payment_method: PaymentMethod
    payment_ref: Optional[str] = Field(None, max_length=200)
    receipt_media_id: Optional[str] = None
    initiator_note: Optional[str] = None


class UpdateProposedEntryRequest(BaseModel):
    """PROPOSED entries only, initiator only. Which fields are editable
    depends on entry_type — enforced in the service layer."""
    amount_paise: Optional[int] = Field(None, gt=0)
    entry_date: Optional[date] = None
    due_date: Optional[date] = None
    initiator_note: Optional[str] = None
    payment_method: Optional[PaymentMethod] = None
    payment_ref: Optional[str] = Field(None, max_length=200)
    receipt_media_id: Optional[str] = None


class ConfirmEntryRequest(BaseModel):
    confirmer_note: Optional[str] = None


class DisputeEntryRequest(BaseModel):
    dispute_reason: str = Field(..., min_length=1, max_length=500)


class WithdrawProposedEntryRequest(BaseModel):
    """Initiator withdraws own PROPOSED (or DISPUTED-after-my-proposal)
    entry — hard-deletes it. No confirmer signature needed."""
    pass


class ProposeVoidRequest(BaseModel):
    """Either side can propose voiding a CONFIRMED entry. Creates a
    paired VOID entry that references target_entry_id; when other party
    confirms, target flips to VOIDED."""
    target_entry_id: str
    reason: str = Field(..., min_length=1, max_length=500)


class EntryActionResponse(BaseModel):
    """Returned on every mutating endpoint — the affected entry plus
    the fresh account balance so the client can update in-place
    without a second GET."""
    entry: EntryOut
    balance: AccountBalance


# ── Reminder prefs ───────────────────────────────────────────────────

class ReminderPrefOut(BaseModel):
    daily_summary_enabled: bool
    weekly_summary_enabled: bool
    new_entry_push_enabled: bool
    quiet_hours_start: Optional[time] = None
    quiet_hours_end: Optional[time] = None


class ReminderPrefUpdate(BaseModel):
    daily_summary_enabled: Optional[bool] = None
    weekly_summary_enabled: Optional[bool] = None
    new_entry_push_enabled: Optional[bool] = None
    quiet_hours_start: Optional[time] = None
    quiet_hours_end: Optional[time] = None
