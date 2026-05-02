import uuid
import enum
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import (
    String, Text, Boolean, DateTime, ForeignKey,
    Enum as SAEnum, DECIMAL, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


def new_uuid():
    return str(uuid.uuid4())


# ── Enums ──────────────────────────────────────────────────────────────────────

class RoleType(str, enum.Enum):
    FARMER = "FARMER"
    DEALER = "DEALER"
    FACILITATOR = "FACILITATOR"
    FARM_PUNDIT = "FARM_PUNDIT"
    CONTENT_MANAGER = "CONTENT_MANAGER"
    RELATIONSHIP_MANAGER = "RELATIONSHIP_MANAGER"
    BUSINESS_MANAGER = "BUSINESS_MANAGER"


class StatusEnum(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


# ── Tables ─────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    phone: Mapped[str] = mapped_column(String(15), unique=True, nullable=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=True)
    photo_url: Mapped[str] = mapped_column(Text, nullable=True)
    language_code: Mapped[str] = mapped_column(String(10), default="en")
    gps_lat: Mapped[Decimal] = mapped_column(DECIMAL(10, 7), nullable=True)
    gps_lng: Mapped[Decimal] = mapped_column(DECIMAL(10, 7), nullable=True)
    address_line: Mapped[str] = mapped_column(Text, nullable=True)
    locality: Mapped[str] = mapped_column(String(255), nullable=True)
    town: Mapped[str] = mapped_column(String(255), nullable=True)
    pin_code: Mapped[str] = mapped_column(String(10), nullable=True)
    state_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    district_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    sub_district_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Session management — JWT jti is compared against this for single-device enforcement
    current_session_id: Mapped[str] = mapped_column(String(36), nullable=True)
    # 30-day grace deletion — set on confirm-delete, anonymised by daily Celery task
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    roles: Mapped[list["UserRole"]] = relationship("UserRole", back_populates="user")


class UserRole(Base):
    __tablename__ = "user_roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    role_type: Mapped[RoleType] = mapped_column(SAEnum(RoleType), nullable=False)
    status: Mapped[StatusEnum] = mapped_column(SAEnum(StatusEnum), default=StatusEnum.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship("User", back_populates="roles")

    __table_args__ = (UniqueConstraint("user_id", "role_type"),)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EnabledLanguage(Base):
    __tablename__ = "enabled_languages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    language_code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    language_name_en: Mapped[str] = mapped_column(String(100), nullable=False)
    language_name_native: Mapped[str] = mapped_column(String(100), nullable=False)
    script_direction: Mapped[str] = mapped_column(String(3), default="LTR")
    status: Mapped[StatusEnum] = mapped_column(SAEnum(StatusEnum), default=StatusEnum.INACTIVE)
    enabled_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
