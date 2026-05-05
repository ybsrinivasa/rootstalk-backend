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
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class CoshReferenceCache(Base):
    """
    Single table for all Cosh entities (Cores and Connects).
    entity_type identifies the category (e.g. 'crop', 'brand', 'problem_to_symptom').
    translations JSONB: {"en": "Paddy", "kn": "ಭತ್ತ", ...}
    metadata JSONB: entity-specific extra fields (e.g. manufacturer_cosh_id for brand)
    """
    __tablename__ = "cosh_reference_cache"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cosh_id: Mapped[str] = mapped_column(String(200), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    parent_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)
    secondary_parent_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    translations: Mapped[dict] = mapped_column(JSON, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("cosh_id", "entity_type", name="uq_cosh_ref_id_type"),
        Index("idx_crc_entity", "entity_type"),
        Index("idx_crc_parent", "parent_cosh_id"),
        Index("idx_crc_status", "entity_type", "status"),
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
