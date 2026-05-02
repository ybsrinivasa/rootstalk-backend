import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Boolean, Integer, DateTime, ForeignKey, Date, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)

def new_uuid():
    return str(uuid.uuid4())


class ManufacturerBrandPortfolio(Base):
    __tablename__ = "manufacturer_brand_portfolios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    product_type: Mapped[str] = mapped_column(String(20), nullable=False)
    brand_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)
    variety_id: Mapped[str] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProductQRCode(Base):
    __tablename__ = "product_qr_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    product_type: Mapped[str] = mapped_column(String(20), nullable=False)
    brand_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)
    variety_id: Mapped[str] = mapped_column(String(36), nullable=True)
    product_display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    manufacture_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    expiry_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    batch_lot_number: Mapped[str] = mapped_column(String(200), nullable=False)
    qr_payload: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("client_id", "brand_cosh_id", "batch_lot_number", name="uq_qr_pesticide"),
        UniqueConstraint("client_id", "variety_id", "batch_lot_number", name="uq_qr_seed"),
    )


class QRScan(Base):
    __tablename__ = "qr_scans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    qr_code_id: Mapped[str] = mapped_column(String(36), ForeignKey("product_qr_codes.id"), nullable=True)
    farmer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    order_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("order_items.id"), nullable=False)
    match_status: Mapped[str] = mapped_column(String(20), nullable=False)
    expected_brand_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)
    scanned_brand_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)
    scan_attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
