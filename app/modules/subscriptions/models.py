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
    crop_start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
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
    """Company subscription units purchased by CA."""
    __tablename__ = "subscription_pools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    units_purchased: Mapped[int] = mapped_column(Integer, nullable=False)
    units_consumed: Mapped[int] = mapped_column(Integer, default=0)
    purchased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    """Farmer asks dealer/facilitator to pay Rs. 199."""
    __tablename__ = "subscription_payment_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    farmer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    requested_from_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    amount: Mapped[float] = mapped_column(DECIMAL(10, 2), default=199.00)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    razorpay_payment_id: Mapped[str] = mapped_column(String(200), nullable=True)
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
    """CHA recommendation triggered by diagnosis (BL-08) or FarmPundit query response."""
    __tablename__ = "triggered_cha_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    farmer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    problem_cosh_id: Mapped[str] = mapped_column(String(200), nullable=False)
    recommendation_type: Mapped[str] = mapped_column(String(5), nullable=False)  # SP | PG
    recommendation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False)        # DIAGNOSIS | QUERY | DIRECT
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
