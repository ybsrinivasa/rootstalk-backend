import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import String, Text, Boolean, Integer, DateTime, ForeignKey, DECIMAL, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)

def new_uuid():
    return str(uuid.uuid4())


class OrderStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SENT = "SENT"
    ACCEPTED = "ACCEPTED"
    PROCESSING = "PROCESSING"
    SENT_FOR_APPROVAL = "SENT_FOR_APPROVAL"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class OrderItemStatus(str, enum.Enum):
    PENDING = "PENDING"
    AVAILABLE = "AVAILABLE"
    POSTPONED = "POSTPONED"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    SENT_FOR_APPROVAL = "SENT_FOR_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NOT_NEEDED = "NOT_NEEDED"
    SKIPPED = "SKIPPED"
    REMOVED = "REMOVED"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    farmer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    dealer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    facilitator_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    date_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    date_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(String(30), default=OrderStatus.DRAFT)
    locked_timelines: Mapped[list] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    items: Mapped[list["OrderItem"]] = relationship("OrderItem", back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"), nullable=False)
    practice_id: Mapped[str] = mapped_column(String(36), ForeignKey("practices.id"), nullable=False)
    timeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("timelines.id"), nullable=False)
    brand_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)
    brand_name: Mapped[str] = mapped_column(String(500), nullable=True)
    given_volume: Mapped[float] = mapped_column(DECIMAL(10, 4), nullable=True)
    volume_unit: Mapped[str] = mapped_column(String(50), nullable=True)
    price: Mapped[float] = mapped_column(DECIMAL(10, 2), nullable=True)
    estimated_volume: Mapped[float] = mapped_column(DECIMAL(10, 4), nullable=True)
    relation_id: Mapped[str] = mapped_column(String(36), nullable=True)
    relation_type: Mapped[str] = mapped_column(String(20), nullable=True)
    relation_role: Mapped[str] = mapped_column(String(50), nullable=True)
    scan_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[OrderItemStatus] = mapped_column(String(30), default=OrderItemStatus.PENDING)
    postponed_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    order: Mapped["Order"] = relationship("Order", back_populates="items")


class SeedOrder(Base):
    __tablename__ = "seed_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    farmer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    variety_id: Mapped[str] = mapped_column(String(36), nullable=False)
    dealer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    facilitator_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    unit: Mapped[str] = mapped_column(String(20), nullable=True)
    quantity: Mapped[float] = mapped_column(DECIMAL(10, 3), nullable=True)
    total_price: Mapped[float] = mapped_column(DECIMAL(10, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="SENT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PackingList(Base):
    __tablename__ = "packing_lists"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"), nullable=True)
    seed_order_id: Mapped[str] = mapped_column(String(36), ForeignKey("seed_orders.id"), nullable=True)
    pdf_url: Mapped[str] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    first_shared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class DealerProfile(Base):
    __tablename__ = "dealer_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), unique=True, nullable=False)
    shop_name: Mapped[str] = mapped_column(String(500), nullable=True)
    shop_address: Mapped[str] = mapped_column(Text, nullable=True)
    sell_categories: Mapped[list] = mapped_column(JSON, nullable=True)
    pesticide_licence_url: Mapped[str] = mapped_column(Text, nullable=True)
    fertiliser_licence_url: Mapped[str] = mapped_column(Text, nullable=True)
    shop_registration_url: Mapped[str] = mapped_column(Text, nullable=True)
    shop_photo_url: Mapped[str] = mapped_column(Text, nullable=True)
    shop_gps_lat: Mapped[float] = mapped_column(DECIMAL(10, 7), nullable=True)
    shop_gps_lng: Mapped[float] = mapped_column(DECIMAL(10, 7), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DealerRelationship(Base):
    __tablename__ = "dealer_relationships"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    dealer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    manufacturer_name: Mapped[str] = mapped_column(String(500), nullable=False)
    manufacturer_client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MissingBrandReport(Base):
    __tablename__ = "missing_brand_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    dealer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    order_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("order_items.id"), nullable=False)
    brand_name_reported: Mapped[str] = mapped_column(String(500), nullable=False)
    manufacturer_name: Mapped[str] = mapped_column(String(500), nullable=True)
    l2_practice: Mapped[str] = mapped_column(String(100), nullable=True)
    additional_info: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    cm_notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
