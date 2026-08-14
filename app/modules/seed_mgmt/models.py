import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import String, Text, Boolean, Integer, DateTime, ForeignKey, DECIMAL, JSON, UniqueConstraint, false as sa_false
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)

def new_uuid():
    return str(uuid.uuid4())


class SeedVariety(Base):
    """A seed/seedling variety managed by the client's Seed Data Manager."""
    __tablename__ = "seed_varieties"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    variety_type: Mapped[str] = mapped_column(String(20), default="SEED")
    description_points: Mapped[list] = mapped_column(JSON, nullable=True)
    dus_characters: Mapped[dict] = mapped_column(JSON, nullable=True)
    photos: Mapped[list] = mapped_column(JSON, nullable=True)
    # 2026-07-05 — Govt-mandated seed-pouch QR write-up. Plain text,
    # rendered on the public verify page below the product details
    # when the QR resolves to a seed variety.
    cultivation_notes: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    pop_assignments: Mapped[list["VarietyPoP"]] = relationship("VarietyPoP", back_populates="variety")

    __table_args__ = (UniqueConstraint("client_id", "crop_cosh_id", "name"),)


class VarietyPoP(Base):
    """Assigns a variety to a PoP (package). Auto-activates when PoP published."""
    __tablename__ = "variety_pop_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    variety_id: Mapped[str] = mapped_column(String(36), ForeignKey("seed_varieties.id"), nullable=False)
    package_id: Mapped[str] = mapped_column(String(36), ForeignKey("packages.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    variety: Mapped["SeedVariety"] = relationship("SeedVariety", back_populates="pop_assignments")

    __table_args__ = (UniqueConstraint("variety_id", "package_id"),)


class SeedOrderStatus(str, enum.Enum):
    # Orders V2 (2026-05-31) — seeds share the OrderItem vocabulary.
    # DRAFT exists so cancel can migrate the order to a fresh row
    # the farmer then sends to a new recipient.
    DRAFT = "DRAFT"
    SENT = "SENT"
    ACCEPTED = "ACCEPTED"
    AVAILABLE = "AVAILABLE"
    POSTPONED = "POSTPONED"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    SENT_FOR_APPROVAL = "SENT_FOR_APPROVAL"
    # 2026-06-19 — Pre-fix, farmer-approval took the order straight
    # to PURCHASED (terminal). The dealer had nothing left to act on
    # and the seed packet sat on the shelf with no system signal.
    # READY_FOR_PICKUP sits between approval and terminal: dealer
    # sees the order in a "Packing / Hand over" pill, taps
    # `/handover` once the farmer picks up → PURCHASED.
    READY_FOR_PICKUP = "READY_FOR_PICKUP"
    PURCHASED = "PURCHASED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    REROUTED = "REROUTED"


class SeedOrderFull(Base):
    """A complete seed/seedling order placed by a farmer."""
    __tablename__ = "seed_orders_full"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    farmer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    variety_id: Mapped[str] = mapped_column(String(36), ForeignKey("seed_varieties.id"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    dealer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    facilitator_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    unit: Mapped[str] = mapped_column(String(20), nullable=True)
    quantity: Mapped[float] = mapped_column(DECIMAL(10, 3), nullable=True)
    total_price: Mapped[float] = mapped_column(DECIMAL(10, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default=SeedOrderStatus.SENT)
    # Orders V2 Batch 12 — postpone-with-days surface for seeds.
    postponed_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Journey identifier across cancel-migrate. Each fresh DRAFT
    # inherits the same lineage_id so reports can trace a seed
    # order across dealer hops.
    lineage_id: Mapped[str] = mapped_column(String(36), nullable=False, default=new_uuid)
    # 2026-06-19 — Human-readable Order ID, parity with the
    # `orders` table. RT-YY-NNNNNN format, lineage-shared.
    # Backfilled for existing rows in migration `c4a91e07f3d6`.
    reference_number: Mapped[str] = mapped_column(String(20), nullable=True)
    # 2026-07-05 — Parity with OrderItem.scan_verified. Flipped by
    # `/farmer/qr/scan` when a matching seed QR is scanned against
    # this seed order. PWA farmer surface renders a "✓ Verified"
    # chip on the seed order card when true.
    scan_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false(),
    )
    # 2026-08-11 — Presence lease mirror of orders.dealer_viewing_until.
    # PUT'd forward by the dealer PWA heartbeat while a dealer-side seed
    # order detail screen is active (no such screen exists today; column
    # is future-ready). Farmer cancel refuses when this is in the future.
    dealer_viewing_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # 2026-08-11 — Cancel-migrate marker (Model B reinstated). TRUE when
    # the farmer taps Cancel — the row flips to DRAFT + clears dealer/
    # facilitator and this flag turns on so the Returned pill picks it
    # up and the /discard endpoint accepts it. Flag clears once the
    # DRAFT is forwarded (flips to SENT).
    is_returned_to_farmer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false(),
    )
    # 2026-08-11 — "Cancelled by you · Previously with X" hint. Copied
    # from dealer_user_id / facilitator_user_id at cancel-time (right
    # before those are cleared for the in-place DRAFT flip). Cleared
    # again when /send picks a new recipient. Informational only.
    released_dealer_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True,
    )
    released_facilitator_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True,
    )
    # 2026-08-12 — See orders.models.Order.return_reason. Values:
    # farmer_cancel | dealer_declined | facilitator_declined.
    return_reason: Mapped[str] = mapped_column(String(30), nullable=True)
    # 2026-08-12 — Facilitator-side parallel (see Order for full note).
    is_returned_to_facilitator: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false(),
    )
    # 2026-08-14 — Dealer's Final Confirmation timestamp (Phase 2 of the
    # order-lifecycle rework). See order_items.final_confirmed_at for
    # the semantics; same rule applies to the single-item seed order.
    final_confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
