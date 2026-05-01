import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


def new_uuid():
    return str(uuid.uuid4())


class PhoneOTP(Base):
    """OTP sent to farmer's phone for PWA registration/login."""
    __tablename__ = "phone_otps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    phone: Mapped[str] = mapped_column(String(15), nullable=False)
    otp_code: Mapped[str] = mapped_column(String(6), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
