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


class ProductCategoryScope(str, enum.Enum):
    """Decision L — scope of a product category.

    GLOBAL: platform-seeded, shared across all Clients. All v1 categories.
    CLIENT: Client-added (v2+); CLIENT-scoped. Not used in v1.
    """
    GLOBAL = "GLOBAL"
    CLIENT = "CLIENT"


class ProductFieldType(str, enum.Enum):
    """Decision L — product category field types."""
    TEXT = "TEXT"
    NUMBER = "NUMBER"
    ENUM = "ENUM"
    MULTI_SELECT = "MULTI_SELECT"


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


class ProductCategory(Base):
    """Decision L — platform-seeded product category list.

    v1 seeds two GLOBAL categories: Direct Soil Application, Water Solubles.
    Categorized by application method, not product type — micronutrients
    fold into one of the two based on how they're applied; soil amendments
    (gypsum, lime) go in Direct Soil Application. Each category drives
    the field schema (ProductCategoryField) that both the Client Global
    Product List and the Fallback Catalogue use for product authoring.
    """
    __tablename__ = "product_categories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    scope: Mapped[ProductCategoryScope] = mapped_column(
        SAEnum(ProductCategoryScope, native_enum=False, length=20),
        default=ProductCategoryScope.GLOBAL,
    )
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    fields: Mapped[list["ProductCategoryField"]] = relationship(
        "ProductCategoryField", back_populates="category",
        order_by="ProductCategoryField.display_order",
    )


class ProductCategoryField(Base):
    """Decision L — per-category field schema.

    Each row is one input field a product of this category must or may
    carry. The authoring UI renders fields dynamically from this
    schema; the storage layer uses hybrid columns (hot fields like N%
    as columns on client_products / fallback_products, everything
    else in a data_json blob) — field_key is the join key on both
    sides.
    """
    __tablename__ = "product_category_fields"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    category_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    field_key: Mapped[str] = mapped_column(String(100), nullable=False)
    field_label: Mapped[str] = mapped_column(String(200), nullable=False)
    field_type: Mapped[ProductFieldType] = mapped_column(
        SAEnum(ProductFieldType, native_enum=False, length=20), nullable=False,
    )
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    unit: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # For ENUM / MULTI_SELECT: {"options": ["prilled", "granule", ...]}
    enum_values_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )

    category: Mapped["ProductCategory"] = relationship(
        "ProductCategory", back_populates="fields",
    )

    __table_args__ = (
        UniqueConstraint("category_id", "field_key", name="uq_category_field_key"),
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
    """Decision L — Client's Global Product List (Tier 1 of 3-tier
    recommendation hierarchy).

    Per-Client product catalogue, authored by CA/SE/CM-EDIT in the
    Client Portal. The Soil Nutrient engine's Tier 1 candidate loader
    pulls from these first; only when Tier 1 can't deliver does it
    fall through to Tier 2 (FallbackProduct) or Tier 3 (pure dosage).

    Schema deliberately mirrors FallbackProduct so the engine's
    candidate-loader can produce a uniform Candidate shape regardless
    of source. The only semantic difference is the `client_id` scope.
    """
    __tablename__ = "client_products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_categories.id", ondelete="RESTRICT"),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    manufacturer: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    grade: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    formulation: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    n_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p2o5_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    k2o_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ca_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mg_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    s_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    zn_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fe_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mn_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cu_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    b_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    application_methods: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    data_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Status values match Decision L: ACTIVE | DISCONTINUED (used
    # when the Client stops stocking the product but keeps the row
    # for historical references).
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

    category: Mapped["ProductCategory"] = relationship("ProductCategory")
    crop_assignments: Mapped[list["ClientProductCropAssignment"]] = relationship(
        "ClientProductCropAssignment",
        back_populates="product",
        cascade="all, delete-orphan",
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


class ClientNormalRange(Base):
    """Decision N — crop-level default Normal Range for one parameter.

    The baseline the Soil Nutrient engine classifies a farmer's measured
    value against when no Package-level override exists for that
    parameter. Authored by CA/SE/CM-EDIT in the Client Portal
    (Portal → Normal Ranges), one row per (client, crop, parameter).

    Partial authoring is allowed — a parameter without a row for
    this (client, crop) is simply NOT classified at engine time
    (no error, no guess). Matches real behaviour where not every lab
    reports every nutrient.

    Resolution order at classification time: Package override (if
    present per `PackageNormalRangeOverride`) → this crop default →
    not classified.
    """
    __tablename__ = "client_normal_ranges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
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
            "client_id", "crop_cosh_id", "parameter_id",
            name="uq_client_crop_parameter_range",
        ),
    )


class PackageNormalRangeOverride(Base):
    """Decision N — sparse per-Package override for one parameter.

    When a specific Package's region or crop subtype needs tighter
    thresholds than the Client's crop-level default, CA/SE/CM-EDIT
    adds an override inside the Package editor (Package →
    'Override Normal Range' tab). Only parameters that genuinely need
    overriding carry a row; unaddressed parameters inherit the crop
    default.

    Resolution order at classification time: this override (if
    present) → crop default via `ClientNormalRange` → not classified.
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
    operator: Mapped[NormalRangeOperator] = mapped_column(
        SAEnum(NormalRangeOperator, native_enum=False, length=20),
        nullable=False,
    )
    low_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    high_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
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
