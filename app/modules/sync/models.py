import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Integer, DateTime, JSON, UniqueConstraint, Index, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


def new_uuid():
    return str(uuid.uuid4())


class CoshSyncLog(Base):
    __tablename__ = "cosh_sync_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sync_id: Mapped[str] = mapped_column(String(200), nullable=False)
    initiated_by: Mapped[str] = mapped_column(String(200), nullable=True)
    sync_mode: Mapped[str] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="IN_PROGRESS")
    items_synced: Mapped[int] = mapped_column(Integer, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, default=0)
    error_log: Mapped[dict] = mapped_column(JSON, nullable=True)
    # 2026-05-21 — per-batch breakdown of what got synced. Surfaced
    # on the SA Sync Log page so the human reading the log can see
    # "Crops: 1, Brands: 5" instead of a meaningless aggregate "6
    # synced". Shape: [{"entity_type": "crops", "inserted": 1,
    # "updated": 0, "failed": 0}, …]. Nullable for legacy rows.
    entity_summary: Mapped[list] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class CoshCoreItem(Base):
    """One Cosh Core item — a flat reference entity (crop, common_name,
    application_method, dosage_unit, formulation, problem_group,
    specific_problem, brand, etc.).

    `core_type` names the Core. `parent_cosh_id` links hierarchies
    (specific_problem→problem_group, brand→common_name, district→state,
    etc.); null for top-level entities. `translations` carries the
    multilingual display map. `metadata_` carries entity-specific
    extras (e.g. manufacturer_name on brand, scientific_name on crop)
    and stays JSONB so new Cores can ride this table without schema
    changes.

    Replaces today's `cosh_reference_cache` rows for every flat-shape
    entity_type. The companion `cosh_connect_rows` carries the N-ary
    Connects (problem_to_symptom and future Compound/Complex Connects).
    """
    __tablename__ = "cosh_core_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cosh_id: Mapped[str] = mapped_column(String(200), nullable=False)
    core_type: Mapped[str] = mapped_column(String(100), nullable=False)
    parent_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    translations: Mapped[dict] = mapped_column(JSON, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("cosh_id", "core_type", name="uq_cosh_core_id_type"),
        Index("idx_cci_type_status", "core_type", "status"),
        Index("idx_cci_parent_type", "parent_cosh_id", "core_type"),
    )


class CoshConnectRow(Base):
    """One row in a typed Cosh Connect — the N-ary linkage between
    Cores. `endpoints` is a JSONB array of {role, cosh_id} pairs;
    role names the endpoint's purpose ("problem", "plant_part",
    "symptom" on problem_to_symptom; "crop", "parameter", "variable"
    on a parameter-variable Connect).

    Designed for Cosh's three Connect flavours (Simple / Compound /
    Complex) without schema changes — a new connect_type and its
    role vocabulary is all that's needed to onboard a new Connect.
    """
    __tablename__ = "cosh_connect_rows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    connect_id: Mapped[str] = mapped_column(String(200), nullable=False)
    connect_type: Mapped[str] = mapped_column(String(100), nullable=False)
    endpoints: Mapped[list] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("connect_id", "connect_type", name="uq_cosh_connect_id_type"),
        Index("idx_ccr_type_status", "connect_type", "status"),
    )


class VolumeFormula(Base):
    """
    305 input volume calculation formulae. CM-managed.
    formula: expression using variables: Dosage, Total_area, Concentration, etc.
    """
    __tablename__ = "volume_formulas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    measure: Mapped[str] = mapped_column(String(20), nullable=False)
    l2_practice: Mapped[str] = mapped_column(String(200), nullable=False)
    application_method: Mapped[str] = mapped_column(String(200), nullable=False)
    brand_unit: Mapped[str] = mapped_column(String(50), nullable=False)
    dosage_unit: Mapped[str] = mapped_column(String(100), nullable=False)
    formula: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CropHealthCrop(Base):
    """Crops enabled for CHA diagnosis at platform level. CM-managed."""
    __tablename__ = "crop_health_crops"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    enabled_by: Mapped[str] = mapped_column(String(36), nullable=True)
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")


class CropMeasure(Base):
    """Crop → Measure mapping (Area-wise vs Plant-wise).

    Drives BL-06 volume-formula lookup and the SE practice-creation form
    (which fields to show — e.g. Volume_per_plant only for Plant-wise).
    Today populated by SA admin endpoints; future plan is to source from
    Cosh — `synced_from_cosh_at` is the placeholder for that integration.

    Valid `measure` values: 'AREA_WISE' | 'PLANT_WISE'. Validation lives in
    the service layer (`app/services/crop_measure.py`) so the schema can
    grow without an enum migration if other measures are added later.
    """
    __tablename__ = "crop_measures"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    measure: Mapped[str] = mapped_column(String(20), nullable=False)
    updated_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    synced_from_cosh_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow,
    )
