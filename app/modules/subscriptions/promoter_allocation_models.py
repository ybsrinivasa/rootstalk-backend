"""SQLAlchemy model for `promoter_allocations`.

Per-promoter sub-account of the company's subscription pool. The CA
allocates units from the company's unallocated balance into each
promoter's row; promoters consume their own row when they assign a
subscription to a farmer; the CA can reclaim unconsumed units back to
the company unallocated balance at any time.

Invariant (asserted at write time, not enforced by the schema):
    units_balance == allocated_total - reclaimed_total - consumed_total

Spec: BL-11 (Subscription State Machine — per-promoter allocation,
2026-05-04).
"""
from datetime import datetime
from sqlalchemy import Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.modules.subscriptions.models import new_uuid, utcnow


class PromoterAllocation(Base):
    __tablename__ = "promoter_allocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    promoter_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Currently-available units the promoter can spend on assignments.
    units_balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Audit running totals — useful for the CA's "history" view and
    # verifying the invariant. Never decrement these; they only ever grow.
    allocated_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reclaimed_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consumed_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "client_id", "promoter_user_id",
            name="uq_promoter_alloc_client_promoter",
        ),
    )
