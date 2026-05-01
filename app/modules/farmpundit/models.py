import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import String, Text, Boolean, Integer, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)

def new_uuid():
    return str(uuid.uuid4())


class QueryStatus(str, enum.Enum):
    NEW = "NEW"
    FORWARDED = "FORWARDED"
    RETURNED = "RETURNED"
    RESPONDED = "RESPONDED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class QueryRemarkAction(str, enum.Enum):
    RECEIVED = "RECEIVED"
    FORWARDED = "FORWARDED"
    RETURNED = "RETURNED"
    RESPONDED = "RESPONDED"
    REJECTED = "REJECTED"


class PunditRole(str, enum.Enum):
    PRIMARY = "PRIMARY"
    PANEL = "PANEL"


class FarmPunditProfile(Base):
    __tablename__ = "farm_pundit_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=True)
    education: Mapped[str] = mapped_column(String(50), nullable=True)
    experience_band: Mapped[str] = mapped_column(String(30), nullable=True)
    support_method: Mapped[str] = mapped_column(String(20), nullable=True)
    cultivation_type: Mapped[str] = mapped_column(String(100), nullable=True)
    organisation_name: Mapped[str] = mapped_column(String(500), nullable=True)
    organisation_type_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    phone_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    declaration_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company_pundits: Mapped[list["ClientFarmPundit"]] = relationship("ClientFarmPundit", back_populates="pundit")
    queries_holding: Mapped[list["Query"]] = relationship("Query", back_populates="current_holder",
                                                           foreign_keys="Query.current_holder_id")


class FarmPunditExpertise(Base):
    __tablename__ = "farm_pundit_expertise"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    domain: Mapped[str] = mapped_column(String(100), nullable=False)


class FarmPunditSupportArea(Base):
    __tablename__ = "farm_pundit_support_areas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    state_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    district_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)


class FarmPunditLanguage(Base):
    __tablename__ = "farm_pundit_languages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    language_code: Mapped[str] = mapped_column(String(10), nullable=False)

    __table_args__ = (UniqueConstraint("pundit_id", "language_code"),)


class FarmPunditCropGroup(Base):
    __tablename__ = "farm_pundit_crop_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    crop_group_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)


class FarmPunditPreference(Base):
    """Farmer's preferred FarmPundit for a specific subscription (BL-12a priority 1)."""
    __tablename__ = "farm_pundit_preferences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), unique=True, nullable=False)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QueryResponseMedia(Base):
    __tablename__ = "query_response_media"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    response_id: Mapped[str] = mapped_column(String(36), ForeignKey("query_responses.id"), nullable=False)
    media_type: Mapped[str] = mapped_column(String(20), nullable=False)  # IMAGE|VIDEO|AUDIO|HYPERLINK
    url: Mapped[str] = mapped_column(Text, nullable=False)
    caption: Mapped[str] = mapped_column(String(500), nullable=True)


class ClientFarmPundit(Base):
    """Company's onboarded FarmPundits."""
    __tablename__ = "client_farm_pundits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    role: Mapped[PunditRole] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    round_robin_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    is_promoter_pundit: Mapped[bool] = mapped_column(Boolean, default=False)
    onboarded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pundit: Mapped["FarmPunditProfile"] = relationship("FarmPunditProfile", back_populates="company_pundits")

    __table_args__ = (UniqueConstraint("client_id", "pundit_id"),)


class PunditInvitation(Base):
    __tablename__ = "pundit_invitations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    role: Mapped[PunditRole] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Query(Base):
    __tablename__ = "queries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    farmer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    crop_age: Mapped[str] = mapped_column(String(100), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    current_holder_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=True)
    status: Mapped[QueryStatus] = mapped_column(String(20), default=QueryStatus.NEW)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    razorpay_payment_id: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    current_holder: Mapped["FarmPunditProfile"] = relationship("FarmPunditProfile", back_populates="queries_holding",
                                                                 foreign_keys=[current_holder_id])
    remarks: Mapped[list["QueryRemark"]] = relationship("QueryRemark", back_populates="query")
    response: Mapped["QueryResponse"] = relationship("QueryResponse", back_populates="query", uselist=False)


class QueryMedia(Base):
    __tablename__ = "query_media"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    query_id: Mapped[str] = mapped_column(String(36), ForeignKey("queries.id"), nullable=False)
    media_type: Mapped[str] = mapped_column(String(20), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)


class QueryRemark(Base):
    __tablename__ = "query_remarks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    query_id: Mapped[str] = mapped_column(String(36), ForeignKey("queries.id"), nullable=False)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=True)
    action: Mapped[QueryRemarkAction] = mapped_column(String(20), nullable=False)
    forwarded_to_pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=True)
    remark: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    query: Mapped["Query"] = relationship("Query", back_populates="remarks")


class QueryResponse(Base):
    __tablename__ = "query_responses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    query_id: Mapped[str] = mapped_column(String(36), ForeignKey("queries.id"), unique=True, nullable=False)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    problem_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=True)
    standard_response_id: Mapped[str] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    query: Mapped["Query"] = relationship("Query", back_populates="response")


class StandardResponse(Base):
    """Company Q&A library. Created by Subject Experts."""
    __tablename__ = "standard_responses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
