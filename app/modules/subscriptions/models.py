import uuid
import enum
from datetime import datetime, timezone, date as date_type
from sqlalchemy import String, Text, Boolean, Integer, DateTime, ForeignKey, DECIMAL, UniqueConstraint, Date
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)

def new_uuid():
    return str(uuid.uuid4())


class SubscriptionStatus(str, enum.Enum):
    WAITLISTED = "WAITLISTED"
    ACTIVE = "ACTIVE"
    LAPSED = "LAPSED"
    CANCELLED = "CANCELLED"
    SUSPENDED = "SUSPENDED"


class SubscriptionType(str, enum.Enum):
    SELF = "SELF"
    ASSIGNED = "ASSIGNED"


class PromoterType(str, enum.Enum):
    DEALER = "DEALER"
    FACILITATOR = "FACILITATOR"
    COMPANY_DESIGNATED = "COMPANY_DESIGNATED"


class AssignmentStatus(str, enum.Enum):
    PENDING_FARMER_APPROVAL = "PENDING_FARMER_APPROVAL"
    ACTIVE = "ACTIVE"
    REJECTED_BY_FARMER = "REJECTED_BY_FARMER"
    # F-P B2 (2026-05-29) — auto-expire after 72h of no farmer response.
    EXPIRED = "EXPIRED"
    # F-P B2 — F-P withdraws their own PENDING_FARMER_APPROVAL.
    CANCELLED_BY_PROMOTER = "CANCELLED_BY_PROMOTER"


class AlertType(str, enum.Enum):
    START_DATE = "START_DATE"
    INPUT = "INPUT"


class AlertStatus(str, enum.Enum):
    SENT = "SENT"
    READ = "READ"


class Subscription(Base):
    """One row per farmer-PoP subscription. Central lifecycle entity."""
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    reference_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=True)
    farmer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    package_id: Mapped[str] = mapped_column(String(36), ForeignKey("packages.id"), nullable=False)
    promoter_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    subscription_type: Mapped[SubscriptionType] = mapped_column(String(20), nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(String(20), default=SubscriptionStatus.WAITLISTED)
    # Area-wise crop context. Used by volume calcs that multiply
    # per-acre dosage by acreage. Set + locked via the
    # /farm-area + /farm-area/confirm endpoints. Stays NULL for
    # plant-wise crops.
    farm_area_acres: Mapped[float] = mapped_column(DECIMAL(10, 2), nullable=True)
    area_unit: Mapped[str] = mapped_column(String(20), nullable=True, default="acres")
    farm_area_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # Plant-wise crop context (2026-05-27). Mirrors farm_area_acres
    # + farm_area_confirmed_at for perennial / plant-wise crops where
    # per-plant dosage + total plant count drive the volume calc
    # instead of acreage. planting_year is just the integer year
    # (e.g. 2015) — perennial age is rounded to years. Stays NULL
    # for area-wise crops.
    number_of_plants: Mapped[int] = mapped_column(Integer, nullable=True)
    planting_year: Mapped[int] = mapped_column(Integer, nullable=True)
    plant_count_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    crop_start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stamped on the FIRST PUT /start-date for this subscription.
    # Drives the 15-day edit window: farmer can change crop_start_date
    # within 15 calendar days of first-set; after that it locks.
    # NULL on legacy rows where the date was set pre-feature.
    crop_start_date_first_set_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Single optional extra alert recipient (replaces the old multi-row
    # FARMER+PROMOTER pattern in alert_recipients). Farmer always gets
    # push notifications regardless of this field. For ASSIGNED subs,
    # GET /alert-preferences falls back to the promoter's phone when
    # this is NULL.
    extra_alert_phone: Mapped[str] = mapped_column(String(20), nullable=True)
    extra_alert_name: Mapped[str] = mapped_column(String(100), nullable=True)
    subscription_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    lapsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    alert_recipients: Mapped[list["AlertRecipient"]] = relationship("AlertRecipient", back_populates="subscription")
    promoter_assignments: Mapped[list["PromoterAssignment"]] = relationship("PromoterAssignment", back_populates="subscription")


class SubscriptionWaitlist(Base):
    __tablename__ = "subscription_waitlist"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SubscriptionPool(Base):
    """Company subscription units purchased by CA.

    Phase B (Razorpay, 2026-05-04): each row now carries a payment audit
    trail. New rows are written only after Razorpay verification.
    Pre-Phase-B rows have NULL payment columns (the free-purchase endpoint
    was removed in commit d5e7b1a93f28).
    """
    __tablename__ = "subscription_pools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    units_purchased: Mapped[int] = mapped_column(Integer, nullable=False)
    units_consumed: Mapped[int] = mapped_column(Integer, default=0)
    purchased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    razorpay_order_id: Mapped[str] = mapped_column(String(64), nullable=True)
    razorpay_payment_id: Mapped[str] = mapped_column(String(64), nullable=True)
    amount_paid_paise: Mapped[int] = mapped_column(Integer, nullable=True)
    purchased_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )


class AlertRecipient(Base):
    __tablename__ = "alert_recipients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    recipient_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    recipient_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    subscription: Mapped["Subscription"] = relationship("Subscription", back_populates="alert_recipients")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    alert_type: Mapped[AlertType] = mapped_column(String(20), nullable=False)
    recipient_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    practice_id: Mapped[str] = mapped_column(String(36), ForeignKey("practices.id"), nullable=True)
    status: Mapped[AlertStatus] = mapped_column(String(20), default=AlertStatus.SENT)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PromoterAssignment(Base):
    __tablename__ = "promoter_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    promoter_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    promoter_type: Mapped[PromoterType] = mapped_column(String(30), nullable=False)
    status: Mapped[AssignmentStatus] = mapped_column(String(30), default=AssignmentStatus.PENDING_FARMER_APPROVAL)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    farmer_responded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    subscription: Mapped["Subscription"] = relationship("Subscription", back_populates="promoter_assignments")


class FarmerSubscriptionHistory(Base):
    """Crop history for QR code and History tab (BL-15, BL-16)."""
    __tablename__ = "farmer_subscriptions_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), unique=True, nullable=False)
    parameter_variable_summary: Mapped[str] = mapped_column(Text, nullable=True)
    qr_payload: Mapped[dict] = mapped_column(String(2000), nullable=True)


class SubscriptionPaymentRequest(Base):
    """Farmer asks someone to pay Rs. 199 — two flavours:

      DELEGATE   — specific user (Dealer/Facilitator); pays via the
                   PWA's Razorpay checkout. `requested_from_user_id`
                   is set; `razorpay_payment_link_id` is None.
      SHARE_LINK — anyone with the Razorpay Payment Link / QR
                   (e.g., farmer's son in a city). Payer is not a
                   registered RootsTalk user; `requested_from_user_id`
                   is None; `razorpay_payment_link_id` +
                   `payment_link_short_url` are set; webhook
                   activates the subscription.
    """
    __tablename__ = "subscription_payment_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    farmer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    # Nullable post-2026-05-29 migration 7e2270ba25aa: SHARE_LINK
    # rows have no designated payer.
    requested_from_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    amount: Mapped[float] = mapped_column(DECIMAL(10, 2), default=199.00)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    # 'DELEGATE' | 'SHARE_LINK'.
    method: Mapped[str] = mapped_column(String(20), default="DELEGATE", nullable=False)
    razorpay_payment_id: Mapped[str] = mapped_column(String(200), nullable=True)
    # SHARE_LINK fields — set when method='SHARE_LINK'.
    razorpay_payment_link_id: Mapped[str] = mapped_column(String(50), nullable=True)
    payment_link_short_url: Mapped[str] = mapped_column(String(500), nullable=True)
    # Masked UPI VPA of whoever paid (from Razorpay webhook payload).
    # Surfaces in the farmer's "paid by" line on the receipt.
    paid_by_vpa: Mapped[str] = mapped_column(String(100), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConditionalAnswer(Base):
    """BL-02: Farmer's YES/NO answer to a conditional question for today."""
    __tablename__ = "conditional_answers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    question_id: Mapped[str] = mapped_column(String(36), ForeignKey("conditional_questions.id"), nullable=False)
    answer_date: Mapped[date_type] = mapped_column(Date(), nullable=False)
    answer: Mapped[str] = mapped_column(String(10), nullable=False)  # YES | NO | BLANK
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("subscription_id", "question_id", "answer_date",
                                       name="uq_conditional_answer_per_day"),)


class TriggeredCHAEntry(Base):
    """Advisory entries triggered into a farmer's plan by a Pundit
    response or self-diagnosis. Despite the historical name (CHA-
    flavoured), this row hosts entries from BOTH advisory pipes that
    flow through a query response per UCAT — CHA and Q&A. The
    `recommendation_type` field discriminates: 'PG' / 'SP' for CHA,
    'QA' for the Q&A library.

    `problem_cosh_id` is nullable post-2026-05-09 — Q&A entries are
    rooted in a question rather than a Cosh-side problem identifier,
    so the column has no meaningful value for them. CHA paths
    continue to populate it.
    """
    __tablename__ = "triggered_cha_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    farmer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    problem_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)
    recommendation_type: Mapped[str] = mapped_column(String(5), nullable=False)  # SP | PG | QA
    recommendation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False)        # DIAGNOSIS | QUERY | DIRECT
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    problem_name: Mapped[str] = mapped_column(String(500), nullable=True)    # display name; CHA: problem; QA: question text
    parent_pg_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)  # resolved parent PG (CHA only)
