"""SQLAlchemy models for the Farmer Ledger — dealer-facing farmer
roster + purchase history. See
`alembic/versions/e2a5c7b3f091_dealer_farmer_ledger.py`.
"""
import enum
import uuid
from datetime import datetime, date, timezone
from decimal import Decimal

from sqlalchemy import (
    String, Text, DECIMAL, Date, DateTime, ForeignKey, Index,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class ManualSaleCategory(str, enum.Enum):
    """Kept in sync with `dealer_manual_sale.category`. Presentation
    strings are i18n keys on the PWA — dealer never sees the enum
    literal directly."""
    SEED = "SEED"
    PESTICIDE = "PESTICIDE"
    FERTILIZER = "FERTILIZER"


class DealerManualSale(Base):
    """A dealer-recorded off-RootsTalk sale. Deliberately NO
    subscription_id — see the migration docstring for why."""

    __tablename__ = "dealer_manual_sale"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    dealer_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False,
    )
    farmer_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False,
    )
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    brand: Mapped[str] = mapped_column(String(255), nullable=True)
    manufacturer: Mapped[str] = mapped_column(String(255), nullable=True)
    qty: Mapped[Decimal] = mapped_column(DECIMAL(12, 3), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    price: Mapped[Decimal] = mapped_column(DECIMAL(12, 2), nullable=True)
    sale_date: Mapped[date] = mapped_column(Date(), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False,
    )

    __table_args__ = (
        Index('ix_dealer_manual_sale_dealer_farmer', 'dealer_user_id', 'farmer_user_id'),
        Index('ix_dealer_manual_sale_dealer_sale_date', 'dealer_user_id', 'sale_date'),
    )


class DealerFarmerNote(Base):
    """Per-dealer per-farmer free-text passbook marginalia. Never
    exposed to any other dealer, the farmer, or CA/SA surfaces."""

    __tablename__ = "dealer_farmer_note"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    dealer_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False,
    )
    farmer_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False,
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False,
    )

    __table_args__ = (
        UniqueConstraint('dealer_user_id', 'farmer_user_id', name='uq_dealer_farmer_note'),
    )
