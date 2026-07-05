"""Unified content translations for SE-authored fields.

Cross-cuts advisory (Package, Element), farmpundit (StandardResponse),
and seed_mgmt (SeedVariety). Kept in its own module so no single
domain owns it — reads more like infrastructure.

Existing per-domain tables (parameter_translations,
variable_translations, conditional_question_translations) stay put.
See migration `d0e4a51b9c72` for the rationale.
"""
import uuid
import hashlib
from datetime import datetime, timezone
from sqlalchemy import String, Text, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class TranslationStatus:
    """String constants — kept as a plain class (not Enum) so callers
    can add values (STALE, FAILED, REVIEW_REQUESTED) without schema
    migration. The column is a plain String(20)."""
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    STALE = "STALE"
    FAILED = "FAILED"


# Canonical entity_type strings. Callers must use these constants so
# a typo in one call site doesn't silently break lookups elsewhere.
class EntityType:
    PACKAGE_DESCRIPTION = "package.description"
    ELEMENT_VALUE = "element.value"
    STANDARD_RESPONSE_QUESTION = "standard_response.question_text"
    SEED_VARIETY_DESCRIPTION_POINTS = "seed_variety.description_points"


def hash_source(text: str | None) -> str:
    """Stable hex hash of an English source string. Same content →
    same hash, so we can detect whether a translation is stale by
    comparing current source hash to stored source_hash.

    Empty / None input hashes as SHA256 of empty string; keeps the
    NOT NULL constraint honest without special-casing empty rows."""
    payload = (text or "").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ContentTranslation(Base):
    __tablename__ = "content_translations"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_uuid,
    )
    # (entity_type, entity_id, field_path) points at what got translated.
    # See EntityType constants above.
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Mostly empty string for scalar fields. Reserved for JSONB /
    # indexed sub-field addressing in future.
    field_path: Mapped[str] = mapped_column(
        String(128), nullable=False, default="",
    )
    language_code: Mapped[str] = mapped_column(String(10), nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=True)
    # SHA256 hex of the English source when this translation was made.
    # Compare against current hash to detect drift.
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    translation_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TranslationStatus.PENDING,
    )
    translated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    approved_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True,
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "entity_type", "entity_id", "field_path", "language_code",
            name="uq_content_translations_key",
        ),
        Index(
            "ix_content_translations_lookup",
            "entity_type", "entity_id", "language_code",
        ),
    )
