import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)

def new_uuid():
    return str(uuid.uuid4())


CASE_CATEGORIES = ['ADVISORY_QUERY', 'ORDER_ISSUE', 'TECHNICAL', 'OTHER']
RESOLUTION_STATUSES = ['OPEN', 'RESOLVED']


class RMCase(Base):
    __tablename__ = "rm_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    raised_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=True)
    category: Mapped[str] = mapped_column(String(50), default="OTHER")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    call_log: Mapped[str] = mapped_column(Text, nullable=True)
    resolution_status: Mapped[str] = mapped_column(String(20), default="OPEN")
    is_escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    escalated_note: Mapped[str] = mapped_column(Text, nullable=True)
    escalated_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
