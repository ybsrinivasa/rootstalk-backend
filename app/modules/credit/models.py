"""SQLAlchemy models for the Credit Management System (CMS) v1.

See `docs/CMS_v1_scoping.md` for design rationale. Three tables:

- `credit_account`: one per (dealer, farmer) pair — a shared ledger.
- `credit_entry`: individual credit / payment / adjustment entries
  going through PROPOSED → CONFIRMED (immutable) / DISPUTED / VOIDED.
- `credit_reminder_pref`: per-user push cadence preferences.

Currency is always in **paise** (bigint) — no float, no rupees. All
display formatting lives at the API boundary via `Intl.NumberFormat`
on the client side.

Immutability rule (enforced in the service layer, not the model): a
CONFIRMED CreditEntry cannot be edited. Corrections happen via the
paired-void flow (initiator creates an ADJUSTMENT_UP/DOWN or VOID
entry; other party signs it). The original entry stays visible in
history with grey styling.
"""
import enum
import uuid
from datetime import datetime, date, timezone

from sqlalchemy import (
    BigInteger, String, Text, Date, DateTime, ForeignKey, Index,
    UniqueConstraint, Time, Boolean,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class CreditEntryType(str, enum.Enum):
    """The kind of movement an entry represents.

    Directional semantics for the running balance:
      OPENING_BALANCE, CREDIT_ADVANCED, ADJUSTMENT_UP  → increase farmer's debt
      PAYMENT_MADE,                     ADJUSTMENT_DOWN → decrease farmer's debt
      VOID                                              → nullifies a paired entry (no balance impact by itself)
    """
    OPENING_BALANCE = "OPENING_BALANCE"
    CREDIT_ADVANCED = "CREDIT_ADVANCED"
    PAYMENT_MADE = "PAYMENT_MADE"
    ADJUSTMENT_UP = "ADJUSTMENT_UP"
    ADJUSTMENT_DOWN = "ADJUSTMENT_DOWN"
    VOID = "VOID"


class CreditEntryStatus(str, enum.Enum):
    PROPOSED = "PROPOSED"       # editable by initiator; not counted in balance
    CONFIRMED = "CONFIRMED"     # immutable contract; counted in balance
    DISPUTED = "DISPUTED"       # confirmer flagged wrong; resolution required
    VOIDED = "VOIDED"           # nullified via paired-void; kept for audit


class InitiatorParty(str, enum.Enum):
    DEALER = "DEALER"
    FARMER = "FARMER"


class PaymentMethod(str, enum.Enum):
    CASH = "CASH"
    UPI = "UPI"
    BANK = "BANK"
    CHEQUE = "CHEQUE"
    OTHER = "OTHER"


class CreditAccount(Base):
    """One per (dealer, farmer) pair. Auto-created on the first credit
    entry — dealer doesn't explicitly open an account. The UNIQUE
    constraint enforces the 1:1 pairing.
    """

    __tablename__ = "credit_account"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    dealer_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False,
    )
    farmer_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False,
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    # Which party's first action triggered account creation. Almost
    # always DEALER in practice (dealer records first credit sale).
    opened_by: Mapped[str] = mapped_column(String(10), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Free-form dealer-private note (like the passbook marginalia on
    # DealerFarmerNote). Never surfaced to the farmer.
    dealer_notes: Mapped[str] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            'dealer_user_id', 'farmer_user_id', name='uq_credit_account_pair',
        ),
        Index('ix_credit_account_dealer', 'dealer_user_id'),
        Index('ix_credit_account_farmer', 'farmer_user_id'),
    )


class CreditEntry(Base):
    """Individual ledger entry. `amount_paise` is always positive; the
    entry_type carries the sign implicit in its directional semantics
    (see CreditEntryType docstring). Balance math sums the appropriate
    signs at read time — no denormalised balance column, avoids drift.

    Immutability: once `status == CONFIRMED`, no field on this row may
    change. Corrections happen via the paired-void flow (a new entry
    of type ADJUSTMENT_* or VOID that requires the other party's
    signature). This is enforced in the service layer.

    Freshness marker: `updated_at` is exposed to the farmer's UI when
    an entry is PROPOSED — a mid-flight dealer edit surfaces as
    "Last updated HH:MM" so the farmer's Confirm tap always applies
    to the current state.
    """

    __tablename__ = "credit_entry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("credit_account.id"), nullable=False,
    )
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # For CREDIT_ADVANCED: auto-set to today, never editable.
    # For OPENING_BALANCE: dealer-set "as of" date, editable while PROPOSED.
    # For PAYMENT_MADE: initiator-set actual payment date, editable while PROPOSED.
    # For ADJUSTMENT_*/VOID: today, not editable.
    entry_date: Mapped[date] = mapped_column(Date(), nullable=False)
    # Only for CREDIT_ADVANCED — the agreed settlement deadline. Set
    # by dealer, editable while PROPOSED, immutable on CONFIRM. Anchors
    # the trust score calculation.
    due_date: Mapped[date] = mapped_column(Date(), nullable=True)

    initiated_by: Mapped[str] = mapped_column(String(10), nullable=False)  # DEALER | FARMER
    initiator_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CreditEntryStatus.PROPOSED.value,
    )

    # Optional forward link to a Farmer Ledger sale when the credit was
    # created via the "On credit" checkbox on Add Sale. NULL for
    # opening balances, standalone credits, payments, adjustments.
    related_sale_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dealer_manual_sale.id"), nullable=True,
    )

    # PAYMENT_MADE only. NULL for other types.
    payment_method: Mapped[str] = mapped_column(String(20), nullable=True)
    # UPI txn id, cheque number, bank ref — whatever the initiator has.
    payment_ref: Mapped[str] = mapped_column(String(200), nullable=True)
    # Farmer's photo of a paper receipt, uploaded via /media/upload.
    # Not enforced FK because the media table lives in a different
    # module and its FK contract isn't stable.
    receipt_media_id: Mapped[str] = mapped_column(String(36), nullable=True)

    initiator_note: Mapped[str] = mapped_column(Text, nullable=True)
    confirmer_note: Mapped[str] = mapped_column(Text, nullable=True)
    dispute_reason: Mapped[str] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False,
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    confirmer_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True,
    )
    voided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        Index('ix_credit_entry_account', 'account_id'),
        Index('ix_credit_entry_account_status', 'account_id', 'status'),
        Index('ix_credit_entry_due_date', 'due_date'),
    )


class CreditReminderPref(Base):
    """Per-user push cadence preferences. One row per user; created
    lazily on first use of any CMS surface. Defaults chosen to be
    useful-not-annoying (see scoping §9)."""

    __tablename__ = "credit_reminder_pref"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), primary_key=True,
    )
    # Only meaningful for dealers — farmers get weekly summary only.
    daily_summary_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
    )
    weekly_summary_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
    )
    new_entry_push_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
    )
    quiet_hours_start: Mapped[str] = mapped_column(Time, nullable=True)
    quiet_hours_end: Mapped[str] = mapped_column(Time, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False,
    )
