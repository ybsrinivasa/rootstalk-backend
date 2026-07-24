import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import (
    String, Text, Boolean, DateTime, ForeignKey, Index,
    Enum as SAEnum, JSON, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
from app.modules.platform.models import StatusEnum


def utcnow():
    return datetime.now(timezone.utc)


def new_uuid():
    return str(uuid.uuid4())


# ── Enums ──────────────────────────────────────────────────────────────────────

class ClientStatus(str, enum.Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    REJECTED = "REJECTED"


class ClientUserRole(str, enum.Enum):
    CA = "CA"
    SUBJECT_EXPERT = "SUBJECT_EXPERT"
    FIELD_MANAGER = "FIELD_MANAGER"
    # Legacy role kept for the SAEnum value list compatibility while
    # the Batch X migration backfills existing rows. New users can
    # no longer be created with this role; the equivalent permission
    # is now granted via ClientUserPrivilege(SEED_DATA) layered on
    # top of SUBJECT_EXPERT. Remove the value in a future batch once
    # we confirm zero usage across all environments.
    SEED_DATA_MANAGER = "SEED_DATA_MANAGER"
    REPORT_USER = "REPORT_USER"
    CLIENT_RM = "CLIENT_RM"
    PRODUCT_MANAGER = "PRODUCT_MANAGER"


class CMRights(str, enum.Enum):
    EDIT = "EDIT"
    VIEW = "VIEW"


class CMPrivilege(str, enum.Enum):
    CROP_HEALTH_CROPS = "CROP_HEALTH_CROPS"
    BRAND_HANDLING = "BRAND_HANDLING"
    VOLUME_CALCULATIONS = "VOLUME_CALCULATIONS"


class ClientUserPrivilege(str, enum.Enum):
    """Batch X (2026-05-19) — per-client single-holder privilege
    held by a Subject Expert. Mirrors the CM-side `CMPrivilege` shape
    but scoped to a single client (so each Seed Company can have its
    own SE holding the Seed Data privilege).

    Initial enum has just `SEED_DATA` — the SE who can manage Seed
    Varieties + CCA Packages for the company. Layered on top of the
    SUBJECT_EXPERT role: the privilege holder is always an SE first.
    """
    SEED_DATA = "SEED_DATA"


class PaymentModel(str, enum.Enum):
    """Per spec §11.1 — client-level subscription configuration.

    COMPANY_PAYS: farmers cannot self-subscribe; only Promoters assign
                  packages on behalf of the company. Pool required.

    FARMER_PAYS:  farmers can self-subscribe (paying directly), AND
                  the company can additionally assign via Promoters.
                  Pool required for the company-assignment side.

    The label "Farmer Pays" refers to availability of farmer self-
    subscription, not an exclusive model — under FARMER_PAYS the
    company still retains the option to assign via Promoters.
    """
    COMPANY_PAYS = "COMPANY_PAYS"
    FARMER_PAYS = "FARMER_PAYS"


# ── Tables ─────────────────────────────────────────────────────────────────────

class Client(Base):
    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    full_name: Mapped[str] = mapped_column(String(500), nullable=False)
    short_name: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=True)
    tagline: Mapped[str] = mapped_column(String(500), nullable=True)
    logo_url: Mapped[str] = mapped_column(Text, nullable=True)
    primary_colour: Mapped[str] = mapped_column(String(7), nullable=True)
    secondary_colour: Mapped[str] = mapped_column(String(7), nullable=True)
    gst_number: Mapped[str] = mapped_column(String(15), unique=True, nullable=True)
    pan_number: Mapped[str] = mapped_column(String(10), unique=True, nullable=True)
    hq_address: Mapped[str] = mapped_column(Text, nullable=True)
    website: Mapped[str] = mapped_column(Text, nullable=True)
    social_links: Mapped[dict] = mapped_column(JSON, nullable=True)
    support_phone: Mapped[str] = mapped_column(String(20), nullable=True)
    office_phone: Mapped[str] = mapped_column(String(20), nullable=True)
    is_manufacturer: Mapped[bool] = mapped_column(Boolean, default=False)
    payment_model: Mapped[PaymentModel] = mapped_column(
        SAEnum(PaymentModel), nullable=False,
    )
    # 2026-07-04 — SA-only "hide from farmer discovery" flag. When True,
    # the Client is excluded from `/farmer/discover/crops-and-companies`
    # (the Crops & Companies drawer surface). Enforced allowed only for
    # COMPANY_PAYS clients — FARMER_PAYS clients need to be discoverable
    # by definition. Used to hide internal/testing/demo clients (e.g.,
    # Testorg on prod) that were created for staff use but must not
    # appear on a farmer's district-scoped discovery list.
    hidden_from_discovery: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False,
    )
    # 2026-07-24 — Training Sandbox V1. When True, this Client is a
    # shadow training child of `parent_client_id` — created by the CA
    # via POST /client/{cid}/training/start, lives for 12 days, then
    # 24h WINDING_DOWN grace, then hard-cascade-deleted by the hourly
    # `training_expiry` celery task. Everything under a training
    # client (Subscriptions, Orders, Queries, PromoterAssignments)
    # inherits the training marker via `client_id` — no per-entity
    # flag needed. See project_rootstalk_training_sandbox_2026_07_24.md
    # (once the memory lands) for the full design.
    is_training: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False,
    )
    parent_client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clients.id", ondelete="RESTRICT"),
        nullable=True,
    )
    training_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    training_ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # 'ACTIVE' during the 12-day window, 'WINDING_DOWN' for the 24h
    # grace after training_ends_at (no new writes, in-flight can
    # complete), then the row is hard-deleted.
    training_status: Mapped[str] = mapped_column(
        String(20), nullable=True,
    )
    # 2026-07-05 — QR Product Authentication: the Cosh
    # `input_manufacturers` cosh_id this client corresponds to. Set
    # by the SA at approval or via the edit modal — deterministic
    # link, no fuzzy string matching. Only meaningful when
    # is_manufacturer=True (backend guard refuses otherwise).
    # Powers `/client/{id}/qr/portfolio/candidates`: walks the
    # `tradename_manufacturer` Cosh Connect to list this
    # manufacturer's brands for the CA's Brand Portfolio picker.
    # Seed varieties come from RootsTalk, not Cosh, so this field
    # is orthogonal to seed-flavour clients.
    cosh_manufacturer_id: Mapped[str] = mapped_column(
        String(200), nullable=True,
    )
    status: Mapped[ClientStatus] = mapped_column(SAEnum(ClientStatus), default=ClientStatus.PENDING_REVIEW)
    onboarding_link_token: Mapped[str] = mapped_column(Text, nullable=True)
    onboarding_link_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=True)
    # SA-side fields
    ca_name: Mapped[str] = mapped_column(String(255), nullable=False)
    ca_phone: Mapped[str] = mapped_column(String(15), nullable=False)
    ca_email: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    organisation_types: Mapped[list["ClientOrganisationType"]] = relationship("ClientOrganisationType", back_populates="client")
    client_users: Mapped[list["ClientUser"]] = relationship("ClientUser", back_populates="client")
    locations: Mapped[list["ClientLocation"]] = relationship("ClientLocation", back_populates="client")
    crops: Mapped[list["ClientCrop"]] = relationship("ClientCrop", back_populates="client")


class ClientOrganisationType(Base):
    __tablename__ = "client_organisation_types"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    org_type_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)

    client: Mapped["Client"] = relationship("Client", back_populates="organisation_types")


class ClientUser(Base):
    __tablename__ = "client_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    role: Mapped[ClientUserRole] = mapped_column(SAEnum(ClientUserRole), nullable=False)
    status: Mapped[StatusEnum] = mapped_column(
        SAEnum(StatusEnum, native_enum=False, length=20),
        default=StatusEnum.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 2026-07-07 — stamped when a row flips ACTIVE→INACTIVE (SA
    # rotates the CA to someone else, or explicitly deactivates
    # from the per-client CA table). Cleared on reactivation. Used
    # by the CA-history list for chronological ordering.
    deactivated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    client: Mapped["Client"] = relationship("Client", back_populates="client_users")

    __table_args__ = (UniqueConstraint("client_id", "user_id", "role"),)


class ClientLocation(Base):
    __tablename__ = "client_locations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    state_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    district_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[StatusEnum] = mapped_column(
        SAEnum(StatusEnum, native_enum=False, length=20),
        default=StatusEnum.ACTIVE,
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    client: Mapped["Client"] = relationship("Client", back_populates="locations")


class ClientCrop(Base):
    __tablename__ = "client_crops"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    removed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    crop_name_en: Mapped[str] = mapped_column(Text, nullable=True)
    crop_scientific_name: Mapped[str] = mapped_column(Text, nullable=True)
    crop_area_or_plant: Mapped[str] = mapped_column(String(20), nullable=True)

    client: Mapped["Client"] = relationship("Client", back_populates="crops")

    __table_args__ = (UniqueConstraint("client_id", "crop_cosh_id"),)


class CropExpertAssignment(Base):
    __tablename__ = "crop_expert_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CMClientAssignment(Base):
    __tablename__ = "cm_client_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cm_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    rights: Mapped[CMRights] = mapped_column(SAEnum(CMRights), default=CMRights.EDIT)
    status: Mapped[StatusEnum] = mapped_column(
        SAEnum(StatusEnum, native_enum=False, length=20),
        default=StatusEnum.ACTIVE,
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("cm_user_id", "client_id"),)


class CMPrivilegeModel(Base):
    __tablename__ = "cm_privileges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cm_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    privilege: Mapped[CMPrivilege] = mapped_column(SAEnum(CMPrivilege), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("cm_user_id", "privilege"),
        # Batch U (2026-05-18): single-holder invariant — at most one
        # CM holds each privilege at any time. Belt-and-braces with
        # the application-level demote-before-grant logic in
        # PUT /admin/cm-privileges/{privilege}.
        Index("uq_cm_privilege_single_holder", "privilege", unique=True),
    )


class ClientUserPrivilegeModel(Base):
    """Batch X (2026-05-19) — per-client single-holder privilege held
    by a Subject Expert. Mirrors CMPrivilegeModel but scoped to a
    single client (each Seed Company can have its own SE holding the
    Seed Data privilege).
    """
    __tablename__ = "client_user_privileges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clients.id"), nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False,
    )
    privilege: Mapped[ClientUserPrivilege] = mapped_column(
        SAEnum(
            ClientUserPrivilege,
            native_enum=False, length=30,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )

    __table_args__ = (
        UniqueConstraint("client_id", "user_id", "privilege"),
        # Single-holder invariant per (client, privilege): at most one
        # user inside a client holds each privilege at any time.
        # Belt-and-braces with the demote-before-grant logic in the
        # PUT /client/{cid}/privileges/{privilege} endpoint.
        Index(
            "uq_client_user_privilege_single_holder",
            "client_id", "privilege", unique=True,
        ),
    )


class ClientPromoter(Base):
    """Links a Dealer or Facilitator user to a client.

    Architectural note (Option C, 2026-05-08): the table name is
    historical. A row in this table is the **company-onboarding link**
    for a Dealer or Facilitator — that is, the act of a Field Manager
    recognising the user as a member of this company's Dealer or
    Facilitator ecosystem. Per the user's described model, a row
    here doesn't automatically make the user a *Promoter* (someone
    who can assign packages to farmers). The `is_promoter` flag on
    top of the link is what designates them as a Promoter.

    Pre-Option-C semantics conflated the two — every row was treated
    as a Promoter. The Alembic migration backfills existing rows to
    `is_promoter=True` to preserve current behaviour. New rows
    created by the existing CA-portal flow also default to True
    until the V1.1 redesign separates the onboarding step from the
    Promoter-designation step in the UI.

    Eligibility rules:
      Plain Facilitator (is_promoter=False) — multi-company OK.
      Facilitator-Promoter (FACILITATOR + is_promoter=True) — exclusive
        per spec §11.2 ("one company at a time"). Enforced in
        register_promoter and in any future `mark_as_promoter` toggle.
      Dealer / Dealer-Promoter — multi-company always.
    """
    __tablename__ = "client_promoters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    promoter_type: Mapped[str] = mapped_column(String(20), nullable=False)  # DEALER / FACILITATOR
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    is_promoter: Mapped[bool] = mapped_column(default=False, nullable=False)
    # R9 (2026-05-29): Promoter-invitation lifecycle on the same row.
    # NONE → PENDING → (ACCEPTED | DECLINED). NONE again after either
    # side revokes / steps down. See migration bf27c207ed07 for the
    # full transition table.
    promoter_request_status: Mapped[str] = mapped_column(
        String(20), default="NONE", nullable=False,
    )
    promoter_request_sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    promoter_request_responded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    territory_notes: Mapped[str] = mapped_column(Text, nullable=True)
    registered_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 2026-05-31 — Promoter-as-Pundit (P-P) designation. Mirrors the
    # historical `ClientFarmPundit.is_promoter_pundit` flag so a
    # Promoter (Facilitator or Dealer) can answer farmer queries
    # without first having to register as a FarmPundit. Toggled by
    # the Field Manager on their Promoter list. Mutually exclusive
    # with `ClientFarmPundit.is_promoter_pundit` per (user, client)
    # for V1 — write-time guard refuses if the same user is already
    # a P-P via the FarmPundit path on this client.
    is_promoter_pundit: Mapped[bool] = mapped_column(
        default=False, nullable=False,
    )

    __table_args__ = (UniqueConstraint("client_id", "user_id", "promoter_type"),)
