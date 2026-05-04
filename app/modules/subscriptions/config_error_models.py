"""SQLAlchemy model for `data_config_errors`.

A small audit table that captures config-level failures from algorithm
endpoints (BL-01 today; future algorithms can reuse the same shape).
The SA team queries this via the admin endpoint so a Content Manager
gets visibility into seed/configuration drift without depending on
log scrapers or email alert infra.

Spec reference: rootstalk_business_logic.md §BL-01 — "Pool=0:
configuration error, log, alert Content Manager".
"""
from datetime import datetime
from sqlalchemy import String, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.modules.subscriptions.models import new_uuid, utcnow


class DataConfigError(Base):
    __tablename__ = "data_config_errors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # Algorithm identifier — "BL-01" today; "BL-02"/etc. when others land.
    algorithm: Mapped[str] = mapped_column(String(20), nullable=False)
    # Best-effort context. All nullable so other algorithms can leave them blank.
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clients.id", ondelete="CASCADE"), nullable=True,
    )
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    district_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    # Raw input that produced the error — useful for reproducing.
    answers_state: Mapped[str] = mapped_column(Text, nullable=True)
    # Free-form description for non-BL-01 callers in the future.
    details: Mapped[str] = mapped_column(Text, nullable=True)
    # The farmer (if any) whose request hit this. ON DELETE SET NULL so
    # account deletion doesn't cascade-remove operations history.
    observed_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )

    __table_args__ = (
        Index("ix_dce_algorithm_occurred_at", "algorithm", "occurred_at"),
    )
