"""SQLAlchemy models for the Soil Nutrient Management module (Phase 1).

Platform-scoped tables — authored by SA or CM in the SA Portal, consumed
by all soil-nutrient-enabled Clients. See design doc Decisions L, N, P.

Client-scoped tables (client_products, client_normal_ranges, etc.)
and Fallback Catalogue arrive in later phases.
"""
import uuid
import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON, Boolean, DateTime, Enum as SAEnum, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


def new_uuid():
    return str(uuid.uuid4())


# ── Enums ──────────────────────────────────────────────────────────────────────

class CategoryGroup(str, enum.Enum):
    """Decision N — grouping of soil parameters in the Normal Range canon.

    Mirrors the structure of the ICL Narayanswamy soil-test report that
    anchored the design (2026-10-15):
      BASE      — soil chemistry (pH, EC, Free CaCO3, Organic Carbon)
      PRIMARY   — macronutrients (N, P, K)
      SECONDARY — secondary nutrients (Ca, Mg, S)
      MICRO     — micronutrients (Fe, Mn, Zn, Cu, B)
      OTHER     — special/trace (Na)
      RATIO     — derived parameters computed from raw values
    """
    BASE = "BASE"
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    MICRO = "MICRO"
    OTHER = "OTHER"
    RATIO = "RATIO"


class NormalRangeOperator(str, enum.Enum):
    """Decision N — one of three operator shapes for a Normal Range row.

    RANGE: closed `[low_value, high_value]`. Below low = Low; above
      high = High; between = Optimum. Both values required.
    UPPER_BOUND: `< high_value`. At or below = Optimum; above = High.
      Low concept absent. Only high_value required.
    LOWER_BOUND: `> low_value`. Below = Low; at or above = Optimum.
      High concept absent. Only low_value required.

    Validation of the required-fields invariant lives in the service
    layer so bulk-upsert can reject early with a clear message.
    """
    RANGE = "RANGE"
    UPPER_BOUND = "UPPER_BOUND"
    LOWER_BOUND = "LOWER_BOUND"


# ── Tables ────────────────────────────────────────────────────────────────────

class PlatformSoilParameter(Base):
    """Decision N — the soil-parameter canon for Normal Range authoring.

    Platform-seeded; SA/CM maintains in the SA Portal. Clients author
    Normal Range VALUES against these parameters per-crop (and
    optionally per-Package override) in the Client Portal.

    Seeded v1 with 22 parameters modeled on the ICL Narayanswamy soil
    test report (2026-01-23, Grape Red globe): 4 base + 3 primary +
    3 secondary + 5 micros + 1 other (Na) + 6 ratios.
    """
    __tablename__ = "platform_soil_parameters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    # Unit for the parameter: "dSm-1", "kg/ha", "ppm", "%".
    # NULL for pH (dimensionless) and ratios (dimensionless).
    unit: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    category_group: Mapped[CategoryGroup] = mapped_column(
        SAEnum(CategoryGroup, native_enum=False, length=20), nullable=False,
    )
    # Formula for derived parameters (ratios): "Ca / Mg", "P / Zn", etc.
    # NULL for raw parameters. The engine evaluates the formula against
    # raw parameter values at classification time.
    formula: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # Lab measurement method note for traceability (e.g., "DTPA
    # extraction + AAS detection" for micros, "Walkley-Black" for OC).
    # v1 does not branch the engine on method; this is informational.
    # Methodology-aware ranges slated for v2 per Decision N.
    measurement_method_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )


class FallbackProduct(Base):
    """Decision U (2026-10-17) — Fallback Catalogue (Tier 2) as a pointer
    table over Cosh products.

    Walk-back of the original Decision M "NOT Cosh-sourced" clause,
    triggered by agronomist SME review 2026-10-16. The old native
    authoring surface (shipped Phase 2, with its own N%/P%/K%
    columns) is replaced with this pointer-table shape so Fallback
    products automatically inherit Cosh-side infrastructure: Brand
    linkage, Dealer Volume Calculations (BL-06 + npk-ranking), QR
    Product Authentication.

    SA/CM curates a subset of Cosh fertilizer products by adding
    rows here, each pointing at a `common_names_of_inputs` cosh_id.
    Nutrient %, formulation, manufacturer, brand all resolved from
    Cosh at read time via `load_fertiliser_candidates()` (shared with
    the Dealer npk-select pipeline).

    Priority at engine time: Tier 1 (Client Products) always wins;
    Fallback is engine-of-last-resort before Tier 3 (pure dosage).
    """
    __tablename__ = "fallback_products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # Pointer into Cosh's common_names_of_inputs Core. Must be unique —
    # a given Cosh product is in or out of the Fallback catalogue;
    # it can't appear twice.
    cosh_product_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    crop_assignments: Mapped[list["FallbackProductCropAssignment"]] = relationship(
        "FallbackProductCropAssignment",
        back_populates="product",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("cosh_product_id", name="uq_fallback_cosh_product"),
    )


class FallbackProductCropAssignment(Base):
    """Decision M (unchanged by U) — per-crop scoping for a Fallback product.

    No crop_assignments rows for a product → the product is available
    for personalisation on any crop (universal). At least one row →
    restricted to those crops only.

    `crop_cosh_id` matches the Cosh crop taxonomy (same String(100)
    pattern used throughout RootsTalk — e.g., `client_crops`,
    `platform_soil_parameters` consumers, Package.crop_cosh_id).
    """
    __tablename__ = "fallback_product_crop_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("fallback_products.id", ondelete="CASCADE"),
        nullable=False,
    )
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )

    product: Mapped["FallbackProduct"] = relationship(
        "FallbackProduct", back_populates="crop_assignments",
    )

    __table_args__ = (
        UniqueConstraint(
            "product_id", "crop_cosh_id",
            name="uq_fallback_product_crop",
        ),
    )


class ClientProduct(Base):
    """Decision U (2026-10-17) — Client's Global Product List (Tier 1)
    as a pointer table over Cosh products.

    Walk-back of Decision L's original native-authoring clause,
    triggered by the agronomist SME review 2026-10-16 and settled
    with Decision U. The Client Portal picker is filtered server-side
    to common_names made by the Client's linked Manufacturers (via
    `client_cosh_manufacturers`) — turning a 10,000-row Cosh catalogue
    into a 40-row list of "my own brands". Nutrient %, formulation,
    manufacturer, brand all resolved from Cosh at read time via
    `load_fertiliser_candidates()` (shared with Dealer npk-select).

    Status values match the walked-forward Decision L semantics:
    ACTIVE | DISCONTINUED (DISCONTINUED keeps the row for historical
    references while excluding it from future personalisation runs).
    """
    __tablename__ = "client_products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Pointer into Cosh's common_names_of_inputs Core. UNIQUE per
    # client — a given Cosh product is in or out of a Client's Global
    # List, never duplicated.
    cosh_product_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    crop_assignments: Mapped[list["ClientProductCropAssignment"]] = relationship(
        "ClientProductCropAssignment",
        back_populates="product",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "client_id", "cosh_product_id",
            name="uq_client_product_cosh",
        ),
    )


class ClientProductCropAssignment(Base):
    """Decision L — per-crop assignment on a Client's Global Product.

    Tab 2 of the Client Portal Products page authors these. The
    engine's Tier 1 loader queries by `(client_id, crop_cosh_id)` to
    produce the active candidate set for a specific subscription.

    Unlike FallbackProductCropAssignment where no rows = universal,
    Client Products default to being considered only when at least
    one crop assignment exists for the subscription's crop. Products
    with zero assignments stay in the Global List but don't
    participate in personalisation until the Client assigns them.
    """
    __tablename__ = "client_product_crop_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("client_products.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )

    product: Mapped["ClientProduct"] = relationship(
        "ClientProduct", back_populates="crop_assignments",
    )

    __table_args__ = (
        UniqueConstraint(
            "product_id", "crop_cosh_id",
            name="uq_client_product_crop",
        ),
    )


class SoilRangeTemplate(Base):
    """Decision W (2026-10-17) — client-level, geography-based Soil
    Range Template library row.

    Walks back Decision N's crop-level scope. Soil property norms
    (pH, EC, OC, free CaCO3, base N/P/K levels) are governed by
    geography — parent material, climate, drainage — not by crop.
    One Dharwad-red template serves Mango, Pomegranate, and Grape
    Packages equally. SE picks a template on each Package; Package-
    level overrides (PackageNormalRangeOverride) handle rare
    exceptions and the Decision V % adjustments.

    Name is unique per Client — "Dharwad red", "Belgaum black cotton",
    "Udupi coastal" — one Client's naming has no bearing on another's.
    """
    __tablename__ = "soil_range_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    rows: Mapped[list["SoilRangeTemplateRow"]] = relationship(
        "SoilRangeTemplateRow", back_populates="template",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "client_id", "name",
            name="uq_soil_range_template_client_name",
        ),
    )


class SoilRangeTemplateRow(Base):
    """Decision W — one parameter's operator + Low + High on a template.

    Partial authoring is fine: a parameter without a row simply isn't
    classified by the engine for Packages that use this template.
    Matches real behaviour where not every lab reports every nutrient.

    No `low_pct_adjustment` / `high_pct_adjustment` here — those are
    crop-sensitive and live on `PackageNormalRangeOverride` so each
    Package's SE can tune the shift for the Package's crop.
    """
    __tablename__ = "soil_range_template_rows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("soil_range_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    parameter_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform_soil_parameters.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operator: Mapped[NormalRangeOperator] = mapped_column(
        SAEnum(NormalRangeOperator, native_enum=False, length=20),
        nullable=False,
    )
    low_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    high_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    template: Mapped["SoilRangeTemplate"] = relationship(
        "SoilRangeTemplate", back_populates="rows",
    )

    __table_args__ = (
        UniqueConstraint(
            "template_id", "parameter_id",
            name="uq_soil_range_template_parameter",
        ),
    )


class PackageNormalRangeOverride(Base):
    """Decision W (2026-10-17, updated) — sparse per-Package row.

    Two things ride on this row (both optional per column, not per row):
      1. Bounds override — operator / low_value / high_value. When
         populated, overrides the matching parameter in the Package's
         chosen SoilRangeTemplate. Leave operator null to inherit.
      2. Decision V % adjustments — low_pct_adjustment /
         high_pct_adjustment. First-class Package data (NOT an
         "override" of anything on the template — templates carry no
         adjustments). Default 0.0.

    Resolution order at classification time: template row from
    `packages.soil_range_template_id` → this row's bounds (if the
    operator column is populated) → not classified. Adjustments
    always come from this row (or default 0 when no row exists).
    """
    __tablename__ = "package_normal_range_overrides"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    package_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("packages.id", ondelete="CASCADE"),
        nullable=False,
    )
    parameter_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform_soil_parameters.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Decision W — operator + bounds now nullable. NULL operator means
    # "inherit bounds from template"; non-NULL means "override template
    # bounds with this row's low/high". low_pct_adjustment /
    # high_pct_adjustment are first-class Package data (not an override
    # of anything on the template) and always carry a value — default
    # 0.0 when the Client hasn't configured one.
    operator: Mapped[Optional[NormalRangeOperator]] = mapped_column(
        SAEnum(NormalRangeOperator, native_enum=False, length=20),
        nullable=True,
    )
    low_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    high_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    low_pct_adjustment: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0",
    )
    high_pct_adjustment: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0",
    )
    created_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "package_id", "parameter_id",
            name="uq_package_parameter_override",
        ),
    )


class PresetSoilProfile(Base):
    """Decision P + MQ5-B — platform-seeded preset soil-value profiles.

    These are SOIL-VALUE PROFILES ONLY (not full Mocks). A Mock is
    always bound to a specific real Package at the Client; the preset
    just pre-fills the hypothetical-soil-values step of the Mock flow
    with a canonical pattern.

    `values_json` is keyed by `platform_soil_parameters.name`, e.g.:
        {
            "pH": 5.2,
            "Available Nitrogen": 150,
            "Available Zinc": 0.3
        }
    Not every parameter needs a value — missing keys = engine does not
    classify that parameter for this profile (matches real behaviour
    for partial lab reports).
    """
    __tablename__ = "preset_soil_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    values_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )
