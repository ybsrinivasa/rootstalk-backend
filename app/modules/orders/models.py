import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import String, Text, Boolean, Integer, DateTime, ForeignKey, DECIMAL, JSON, Index
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
    # Orders V2 (2026-05-31): the original row stays on the CANCELLED
    # husk after the items have been migrated to a fresh DRAFT. The
    # UI hides REROUTED items from "active item" lists; reports can
    # still walk them via lineage_id to reconstruct the journey.
    REROUTED = "REROUTED"


class OrderCategory(str, enum.Enum):
    """Hard category on Order — set at create-time, immutable.

    SEED orders live on SeedOrder (single-variety, no items list).
    The Order table only ever sees PESTICIDE / FERTILIZER.
    Storing it discretely avoids re-deriving from items on every
    licence-match / locked-brand check.
    """
    PESTICIDE = "PESTICIDE"
    FERTILIZER = "FERTILIZER"


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
    # PESTICIDE or FERTILIZER. Nullable on legacy orders; required
    # for new orders (Batch 2 will enforce this in the create gate).
    category: Mapped[str] = mapped_column(String(20), nullable=True)
    status: Mapped[OrderStatus] = mapped_column(String(30), default=OrderStatus.DRAFT)
    locked_timelines: Mapped[list] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # Heartbeat lease — set by the dealer's app while they're on the
    # order detail screen. Farmer cancel refuses while this is in the
    # future. Each heartbeat extends by 30 s. NULL = not viewing.
    dealer_viewing_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Batch 28 — partial dealer edits captured between brand pick and
    # Mark Available. Map of item_id → {brand_cosh_id, brand_name,
    # given_volume, volume_unit, price}. Server clears an entry when
    # its item flips to AVAILABLE. Default `{}` so reads never NULL.
    dealer_draft: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}",
    )
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
    # Per-subscription versioning Phase 3.2: pointer to the locked snapshot in
    # force at order-create time. Nullable for backwards compatibility — orders
    # placed before Phase 3.2 have NULL and fall back to master at read time.
    snapshot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("locked_timeline_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Journey ID across re-routes. When the farmer cancels and the
    # items get migrated to a fresh DRAFT, the new OrderItem row
    # inherits the same lineage_id. Reports group by this to
    # reconstruct the full dealer-by-dealer history.
    lineage_id: Mapped[str] = mapped_column(String(36), nullable=False, default=new_uuid)
    # Tandem-archive marker (Orders V2 Batch 8). Stamped when the
    # item's timeline window closes. Active surfaces filter
    # `archived_at IS NULL`; History views show archived rows too.
    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
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
    # Journey ID — seeds share the lineage vocabulary even though
    # they don't share OrderItem. Each SeedOrder is its own item
    # for re-route accounting.
    lineage_id: Mapped[str] = mapped_column(String(36), nullable=False, default=new_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OrderItemEvent(Base):
    """Append-only audit trail for an item's journey across orders.

    One row per state change — every accept, postpone, return,
    re-route, cancel, expire. Reports group by `lineage_id` to
    reconstruct the full story: "Pesticide P1 went to Dealer D1,
    came back Not Available, went to Facilitator F1 → Dealer D2,
    got postponed 3 days, ended Available, bought".

    Either `order_item_id` or `seed_order_id` is set (mutually
    exclusive) — `order_id` is set when the event has an order
    context but no item (e.g. CANCELLED_BY_FARMER on an empty
    husk after the items already migrated to a fresh DRAFT).
    """
    __tablename__ = "order_item_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # Index lives in the migration as `ix_order_item_events_lineage`;
    # don't pass index=True here or alembic will keep proposing a
    # second auto-named index for the same column.
    lineage_id: Mapped[str] = mapped_column(String(36), nullable=False)
    order_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("order_items.id", ondelete="SET NULL"), nullable=True,
    )
    # Batch 12 (f1c4d7b25e88) dropped the FK: the live seed flow uses
    # `seed_orders_full`, not `seed_orders` — pointing at one or the
    # other would lock us out of the other. Plain UUID; lineage_id
    # is the primary lookup anyway.
    seed_order_id: Mapped[str] = mapped_column(String(36), nullable=True)
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True,
    )
    actor_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    actor_role: Mapped[str] = mapped_column(String(20), nullable=True)
    # Free-form to allow new event types without migrations. Values
    # in use: CREATED, SENT, ACCEPTED, MARKED_AVAILABLE,
    # MARKED_POSTPONED, MARKED_NOT_AVAILABLE, POSTPONE_EXPIRED,
    # REROUTED_FROM, REROUTED_TO, CANCELLED_BY_FARMER,
    # TIMELINE_EXPIRED, PURCHASE_RECORDED.
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    prev_status: Mapped[str] = mapped_column(String(30), nullable=True)
    new_status: Mapped[str] = mapped_column(String(30), nullable=True)
    # SQLAlchemy reserves `metadata` on Base; keep this name.
    event_metadata: Mapped[dict] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    # 2026-05-21 — Cosh-resolved selection. Replaces the free-text-only
    # workflow with one driven by the manufacturer catalog. Nullable
    # because pre-2026-05-21 rows captured names only (no cosh_id).
    manufacturer_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    # PESTICIDE | FERTILIZER. The same manufacturer can have one row per
    # category — a dealer might stock Bayer pesticides but not Bayer
    # fertilizers (or vice versa). Nullable for legacy rows.
    category: Mapped[str] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DealerManufacturerCatalog(Base):
    """Materialised view of the Cosh manufacturer walk, per category.

    Every dealer sees the same list — it's a pure function of Cosh
    data — so we cache it once instead of redoing the L2-→-CN-→-MFR
    walk per request. Cosh data turns over slowly; we accept staleness
    up to the next rebuild (lazy on first read, or manual via an
    admin endpoint).

    Composite PK: a manufacturer in both PESTICIDE and FERTILIZER is
    two rows. Truncate-and-reload is the only write path.
    """
    __tablename__ = "dealer_manufacturer_catalog"

    category: Mapped[str] = mapped_column(String(20), primary_key=True)
    manufacturer_cosh_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    manufacturer_name: Mapped[str] = mapped_column(String(500), nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )


class BrandLookupCache(Base):
    """Materialised brand catalog. Walks Cosh's
    `tradename_commonname` × `tradename_manufacturer` ×
    `tradename_formulation` Connects once and lands one row per
    (common_name, trade_name) pair so BL-07's brand picker stays
    sub-second over the 13k+ trade-name dataset.

    Fix 2026-06-01 — BL-07 was looking for `core_type='brand'` Core
    rows; Cosh stores brands as `trade_names` + Connects instead, so
    the search returned empty. This cache materialises the Connect
    walk for the dealer surface.

    Truncate-and-reload is the only write path. Refresh is SA-
    triggered via /admin/brand-cache/refresh (or lazy on first read).
    """
    __tablename__ = "brand_lookup_cache"

    common_name_cosh_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    trade_name_cosh_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    trade_name: Mapped[str] = mapped_column(String(500), nullable=False)
    manufacturer_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    manufacturer_name: Mapped[str] = mapped_column(String(500), nullable=True)
    formulation_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    formulation_name: Mapped[str] = mapped_column(String(500), nullable=True)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )

    __table_args__ = (
        Index("idx_brand_cache_cn", "common_name_cosh_id"),
    )


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
