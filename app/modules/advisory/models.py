import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import (
    String, Text, Boolean, Integer, DateTime, ForeignKey,
    Enum as SAEnum, UniqueConstraint, Index, CheckConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


def new_uuid():
    return str(uuid.uuid4())


# ── Enums ──────────────────────────────────────────────────────────────────────

class PackageType(str, enum.Enum):
    ANNUAL = "ANNUAL"
    PERENNIAL = "PERENNIAL"


class PackageStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class PackageCreatedVia(str, enum.Enum):
    """Audit/lineage marker for how a Local Package row came into
    being. Locked 2026-05-11; see
    project_rootstalk_global_to_local_pipe.md for the multi-row
    versioning model.

    CM_PUSH              First-contact push from a CM (SA portal).
    SE_PULL_DRAFT        SE pulled a refreshed version from Global.
    SE_EDIT_DRAFT        SE cloned the current PUBLISHED to a new
                         DRAFT to make self-edits.
    SE_ROLLBACK_PUBLISH  SE published a historical row's content as
                         a new PUBLISHED version (rollback).
    """
    CM_PUSH = "CM_PUSH"
    SE_PULL_DRAFT = "SE_PULL_DRAFT"
    SE_EDIT_DRAFT = "SE_EDIT_DRAFT"
    SE_ROLLBACK_PUBLISH = "SE_ROLLBACK_PUBLISH"


class ParameterSource(str, enum.Enum):
    COSH = "COSH"
    CUSTOM = "CUSTOM"


class TranslationStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"


class TimelineFromType(str, enum.Enum):
    DBS = "DBS"
    DAS = "DAS"
    CALENDAR = "CALENDAR"
    # Batch 39O (UCAT unification, 2026-05-16): CHA / Q&A pipes plug
    # into the same Timeline table and need their own anchor units.
    # `DAYS_AFTER_DETECTION` is the CHA-PG / CHA-SP trigger; the
    # farmer spots the problem and Day-1 starts then. `DAYS_AFTER_
    # RESPONSE` is the Q&A trigger; Day-1 is when the FarmPundit
    # delivered the standard response.
    DAYS_AFTER_DETECTION = "DAYS_AFTER_DETECTION"
    DAYS_AFTER_RESPONSE = "DAYS_AFTER_RESPONSE"


class TimelineStatus(str, enum.Enum):
    """Batch 28 (2026-05-14): SE-controlled active/inactive state.
    INACTIVE timelines stay visible on the SA portal with a badge
    but are excluded from farmer advisory and BL-03 deduplication."""
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class PracticeL0(str, enum.Enum):
    INPUT = "INPUT"
    NON_INPUT = "NON_INPUT"
    INSTRUCTION = "INSTRUCTION"
    MEDIA = "MEDIA"


class RelationType(str, enum.Enum):
    AND = "AND"
    OR = "OR"
    IF = "IF"


class ConditionalAnswer(str, enum.Enum):
    YES = "YES"
    NO = "NO"
    BOTH = "BOTH"


# ── Domain 3: Packages of Practices ───────────────────────────────────────────

class Package(Base):
    __tablename__ = "packages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=True)
    parent_global_id: Mapped[str] = mapped_column(String(36), ForeignKey("packages.id"), nullable=True)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    package_type: Mapped[PackageType] = mapped_column(SAEnum(PackageType), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date_label_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[PackageStatus] = mapped_column(SAEnum(PackageStatus), default=PackageStatus.DRAFT)
    version: Mapped[int] = mapped_column(Integer, default=1)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    cascade_inactivated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    # Multi-row versioning (2026-05-11). NULL on pre-migration rows;
    # populated on every newly created row from this point on.
    created_via: Mapped[PackageCreatedVia] = mapped_column(
        SAEnum(PackageCreatedVia, name="packagecreatedvia"), nullable=True,
    )
    # Lineage pointer for SE_ROLLBACK_PUBLISH — the historical row
    # whose content this row was published from. NULL otherwise.
    source_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("packages.id"), nullable=True,
    )

    locations: Mapped[list["PackageLocation"]] = relationship("PackageLocation", back_populates="package")
    authors: Mapped[list["PackageAuthor"]] = relationship("PackageAuthor", back_populates="package")
    package_variables: Mapped[list["PackageVariable"]] = relationship("PackageVariable", back_populates="package")
    timelines: Mapped[list["Timeline"]] = relationship("Timeline", back_populates="package")

    __table_args__ = (
        # Partial unique: name uniqueness fires only on ACTIVE
        # rows so the SE-pull flow can hold v1 PUBLISHED + v2 DRAFT
        # with the same inherited Global name without colliding.
        # Single-DRAFT invariant is enforced in app code, not here.
        Index(
            "uq_package_client_crop_name_active",
            "client_id", "crop_cosh_id", "name",
            unique=True,
            postgresql_where="status = 'ACTIVE'",
        ),
        Index(
            "ix_packages_client_parent_status",
            "client_id", "parent_global_id", "status",
        ),
    )


class PackageLocation(Base):
    __tablename__ = "package_locations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    package_id: Mapped[str] = mapped_column(String(36), ForeignKey("packages.id"), nullable=False)
    state_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    district_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)

    package: Mapped["Package"] = relationship("Package", back_populates="locations")


class PackageAuthor(Base):
    __tablename__ = "package_authors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    package_id: Mapped[str] = mapped_column(String(36), ForeignKey("packages.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    designation: Mapped[str] = mapped_column(String(255), nullable=True)
    professional_profile: Mapped[str] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    package: Mapped["Package"] = relationship("Package", back_populates="authors")


# ── Parameters and Variables ───────────────────────────────────────────────────

class Parameter(Base):
    __tablename__ = "parameters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=True)
    # Mirror of a Cosh `package_parameters` entity. NULL on CUSTOM
    # rows authored locally by a CM/SE. Set on Cosh-mirrored rows
    # so we can dedup mirrored entries on subsequent syncs.
    cosh_id: Mapped[str] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[ParameterSource] = mapped_column(SAEnum(ParameterSource), default=ParameterSource.COSH)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    translations: Mapped[list["ParameterTranslation"]] = relationship("ParameterTranslation", back_populates="parameter")
    variables: Mapped[list["Variable"]] = relationship("Variable", back_populates="parameter")

    __table_args__ = (
        # Partial unique: a Cosh parameter UUID may appear at most
        # once per crop in the local mirror. CUSTOM rows (cosh_id
        # NULL) are not constrained.
        Index(
            "uq_parameters_crop_cosh_id",
            "crop_cosh_id", "cosh_id",
            unique=True,
            postgresql_where="cosh_id IS NOT NULL",
        ),
    )


class ParameterTranslation(Base):
    __tablename__ = "parameter_translations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    parameter_id: Mapped[str] = mapped_column(String(36), ForeignKey("parameters.id"), nullable=False)
    language_code: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    translation_status: Mapped[TranslationStatus] = mapped_column(SAEnum(TranslationStatus), default=TranslationStatus.PENDING)
    approved_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    parameter: Mapped["Parameter"] = relationship("Parameter", back_populates="translations")

    __table_args__ = (UniqueConstraint("parameter_id", "language_code"),)


class Variable(Base):
    __tablename__ = "variables"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    parameter_id: Mapped[str] = mapped_column(String(36), ForeignKey("parameters.id"), nullable=False)
    # Mirror of a Cosh `package_variables` entity. NULL on CUSTOM
    # rows.
    cosh_id: Mapped[str] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    parameter: Mapped["Parameter"] = relationship("Parameter", back_populates="variables")
    translations: Mapped[list["VariableTranslation"]] = relationship("VariableTranslation", back_populates="variable")
    package_variables: Mapped[list["PackageVariable"]] = relationship("PackageVariable", back_populates="variable")

    __table_args__ = (
        UniqueConstraint("parameter_id", "name"),
        Index(
            "uq_variables_parameter_cosh_id",
            "parameter_id", "cosh_id",
            unique=True,
            postgresql_where="cosh_id IS NOT NULL",
        ),
    )


class VariableTranslation(Base):
    __tablename__ = "variable_translations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    variable_id: Mapped[str] = mapped_column(String(36), ForeignKey("variables.id"), nullable=False)
    language_code: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    translation_status: Mapped[TranslationStatus] = mapped_column(SAEnum(TranslationStatus), default=TranslationStatus.PENDING)
    approved_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    variable: Mapped["Variable"] = relationship("Variable", back_populates="translations")

    __table_args__ = (UniqueConstraint("variable_id", "language_code"),)


class PackageVariable(Base):
    """The fingerprint of a Package — one row per Parameter it answers."""
    __tablename__ = "package_variables"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    package_id: Mapped[str] = mapped_column(String(36), ForeignKey("packages.id"), nullable=False)
    parameter_id: Mapped[str] = mapped_column(String(36), ForeignKey("parameters.id"), nullable=False)
    variable_id: Mapped[str] = mapped_column(String(36), ForeignKey("variables.id"), nullable=False)

    package: Mapped["Package"] = relationship("Package", back_populates="package_variables")
    variable: Mapped["Variable"] = relationship("Variable", back_populates="package_variables")

    __table_args__ = (UniqueConstraint("package_id", "parameter_id"),)


# ── Timelines, Practices, Elements ────────────────────────────────────────────

class Timeline(Base):
    """UCAT-shared Timeline (Batch 39O, 2026-05-16).

    Polymorphic across the four advisory pipes:
      • CCA Package — `package_id` set, other 3 FKs NULL
      • CHA Problem-Group — `pg_recommendation_id` set, other 3 NULL
      • CHA Specific-Problem — `sp_recommendation_id` set, other 3 NULL
      • Q&A Standard Response — `standard_response_id` set, other 3 NULL

    The CHECK constraint `timelines_one_parent_chk` enforces exactly
    one parent per row. Practices / Elements / Relations / Conditional
    Questions hang off `timeline_id` and are pipe-agnostic — every
    authoring feature (Brand Lock, frequency, common_name, relations,
    CQs) lights up uniformly across pipes.

    Before unification each pipe had its own timeline+practice+element
    tables (CCA: `timelines`; PG+QA: `pg_timelines`; SP: `sp_timelines`).
    The old tables were dropped in the same migration and their content
    wiped per user 2026-05-16 (pre-launch testing data).
    """
    __tablename__ = "timelines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # Exactly one of the four parent FKs is set per row.
    package_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("packages.id"), nullable=True,
    )
    pg_recommendation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pg_recommendations.id"), nullable=True,
    )
    sp_recommendation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sp_recommendations.id"), nullable=True,
    )
    standard_response_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("standard_responses.id"), nullable=True,
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    # `from_type` is stored as VARCHAR(30) (we dropped the postgres
    # enum in the Batch 39O migration so CHA/QA anchor units coexist
    # with CCA's DAS/DBS/CALENDAR), but Python-side it's still a
    # `TimelineFromType` enum so the dozens of call sites doing
    # `tl.from_type.value` / `tl.from_type == "DAS"` keep working.
    # Default matches the legacy `pg_timelines / sp_timelines` shape
    # so PG/SP/QA callers that omit `from_type` land with the right
    # anchor; CCA callers always pass an explicit DAS / DBS / CALENDAR.
    from_type: Mapped[TimelineFromType] = mapped_column(
        SAEnum(TimelineFromType, native_enum=False, length=30,
               values_callable=lambda e: [m.value for m in e]),
        nullable=False, default=TimelineFromType.DAYS_AFTER_DETECTION,
    )
    from_value: Mapped[int] = mapped_column(Integer, nullable=False)
    to_value: Mapped[int] = mapped_column(Integer, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    # Batch 28: SE marks INACTIVE to retire a timeline without losing
    # its history. INACTIVE timelines stay visible on the SA portal
    # with a badge but are excluded from farmer advisory.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="ACTIVE",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    package: Mapped["Package"] = relationship("Package", back_populates="timelines")
    pg_recommendation: Mapped["PGRecommendation"] = relationship(
        "PGRecommendation", back_populates="timelines",
    )
    sp_recommendation: Mapped["SPRecommendation"] = relationship(
        "SPRecommendation", back_populates="timelines",
    )
    standard_response: Mapped["StandardResponse"] = relationship(
        "StandardResponse", back_populates="timelines",
    )
    practices: Mapped[list["Practice"]] = relationship("Practice", back_populates="timeline")
    relations: Mapped[list["Relation"]] = relationship("Relation", back_populates="timeline")
    conditional_questions: Mapped[list["ConditionalQuestion"]] = relationship("ConditionalQuestion", back_populates="timeline")

    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN package_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN pg_recommendation_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN sp_recommendation_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN standard_response_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="timelines_one_parent_chk",
        ),
        Index(
            "uq_timelines_package_name", "package_id", "name",
            unique=True, postgresql_where="package_id IS NOT NULL",
        ),
        Index(
            "uq_timelines_pg_rec_name", "pg_recommendation_id", "name",
            unique=True, postgresql_where="pg_recommendation_id IS NOT NULL",
        ),
        Index(
            "uq_timelines_sp_rec_name", "sp_recommendation_id", "name",
            unique=True, postgresql_where="sp_recommendation_id IS NOT NULL",
        ),
        Index(
            "uq_timelines_sr_name", "standard_response_id", "name",
            unique=True, postgresql_where="standard_response_id IS NOT NULL",
        ),
    )

    @property
    def parent_kind(self) -> str:
        """'CCA' / 'PG' / 'SP' / 'QA' — derived from which parent FK is
        set. Useful for the unified advisory-render service that walks
        timelines across pipes."""
        if self.package_id is not None:
            return "CCA"
        if self.pg_recommendation_id is not None:
            return "PG"
        if self.sp_recommendation_id is not None:
            return "SP"
        if self.standard_response_id is not None:
            return "QA"
        return "UNKNOWN"


class Practice(Base):
    __tablename__ = "practices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    timeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("timelines.id"), nullable=False)
    l0_type: Mapped[PracticeL0] = mapped_column(SAEnum(PracticeL0), nullable=False)
    l1_type: Mapped[str] = mapped_column(String(100), nullable=True)
    l2_type: Mapped[str] = mapped_column(String(100), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    relation_id: Mapped[str] = mapped_column(String(36), ForeignKey("relations.id"), nullable=True)
    relation_role: Mapped[str] = mapped_column(String(50), nullable=True)
    is_special_input: Mapped[bool] = mapped_column(Boolean, default=False)
    common_name_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    frequency_days: Mapped[int] = mapped_column(Integer, nullable=True)
    # Batch 39I-a (2026-05-16) — explicit per-Practice Brand Lock flag.
    # Only meaningful when a BRAND_NAME element is set. Drives the
    # downstream order-routing rule ("locked-brand items can only be
    # fulfilled by the company's onboarded dealers") and the dealer
    # brand picker ("disabled for locked-brand items"). The legacy
    # BL-07 "lock by element presence" inference is superseded by this
    # explicit flag.
    is_brand_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    timeline: Mapped["Timeline"] = relationship("Timeline", back_populates="practices")
    elements: Mapped[list["Element"]] = relationship("Element", back_populates="practice")
    conditionals: Mapped[list["PracticeConditional"]] = relationship("PracticeConditional", back_populates="practice")


class Element(Base):
    __tablename__ = "elements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    practice_id: Mapped[str] = mapped_column(String(36), ForeignKey("practices.id"), nullable=False)
    element_type: Mapped[str] = mapped_column(String(100), nullable=False)
    cosh_ref: Mapped[str] = mapped_column(String(200), nullable=True)
    value: Mapped[str] = mapped_column(Text, nullable=True)
    unit_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    practice: Mapped["Practice"] = relationship("Practice", back_populates="elements")


class Relation(Base):
    __tablename__ = "relations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    timeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("timelines.id"), nullable=False)
    relation_type: Mapped[RelationType] = mapped_column(SAEnum(RelationType), nullable=False)
    expression: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    timeline: Mapped["Timeline"] = relationship("Timeline", back_populates="relations")
    practices: Mapped[list["Practice"]] = relationship("Practice",
                                                        foreign_keys="Practice.relation_id",
                                                        primaryjoin="Relation.id == Practice.relation_id")


class ConditionalQuestion(Base):
    __tablename__ = "conditional_questions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    timeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("timelines.id"), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    timeline: Mapped["Timeline"] = relationship("Timeline", back_populates="conditional_questions")
    translations: Mapped[list["ConditionalQuestionTranslation"]] = relationship(
        "ConditionalQuestionTranslation", back_populates="question")
    practice_conditionals: Mapped[list["PracticeConditional"]] = relationship(
        "PracticeConditional", back_populates="question")


class ConditionalQuestionTranslation(Base):
    __tablename__ = "conditional_question_translations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    question_id: Mapped[str] = mapped_column(String(36), ForeignKey("conditional_questions.id"), nullable=False)
    language_code: Mapped[str] = mapped_column(String(10), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    translation_status: Mapped[TranslationStatus] = mapped_column(SAEnum(TranslationStatus), default=TranslationStatus.PENDING)

    question: Mapped["ConditionalQuestion"] = relationship("ConditionalQuestion", back_populates="translations")

    __table_args__ = (UniqueConstraint("question_id", "language_code"),)


class PracticeConditional(Base):
    __tablename__ = "practice_conditionals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    practice_id: Mapped[str] = mapped_column(String(36), ForeignKey("practices.id"), nullable=False)
    question_id: Mapped[str] = mapped_column(String(36), ForeignKey("conditional_questions.id"), nullable=False)
    answer: Mapped[ConditionalAnswer] = mapped_column(SAEnum(ConditionalAnswer), nullable=False)

    practice: Mapped["Practice"] = relationship("Practice", back_populates="conditionals")
    question: Mapped["ConditionalQuestion"] = relationship("ConditionalQuestion", back_populates="practice_conditionals")


class RelationConditional(Base):
    """CCA Step 4 / Batch 4B — Path A.

    When a Practice is part of a saved Relation, the conditional link
    binds to the Relation rather than the individual Practice (spec
    §6.4 + user clarification 2026-05-07: 'the whole relation needs
    to be associated with a conditional'). PracticeConditional remains
    in use for independent practices.
    """
    __tablename__ = "relation_conditionals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    relation_id: Mapped[str] = mapped_column(String(36), ForeignKey("relations.id"), nullable=False)
    question_id: Mapped[str] = mapped_column(String(36), ForeignKey("conditional_questions.id"), nullable=False)
    answer: Mapped[ConditionalAnswer] = mapped_column(SAEnum(ConditionalAnswer), nullable=False)

    __table_args__ = (UniqueConstraint("relation_id", "question_id"),)


# ── Domain 4: CHA — Problem Groups and Specific Problems ──────────────────────

class PGRecommendation(Base):
    """Problem Group recommendations — global (client_id=NULL) or client-local.

    A PG is crop-agnostic; the discriminator that splits bundles is
    `area_or_plant` ('AREA_WISE' / 'PLANT_WISE') — user clarified
    2026-05-10: each (client, problem_group, area_or_plant) is its
    own bundle with its own publish lifecycle. Imports map one-to-one
    (global's area-wise → local's area-wise).
    """
    __tablename__ = "pg_recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    problem_group_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=True)
    area_or_plant: Mapped[str] = mapped_column(String(20), nullable=True)
    parent_id: Mapped[str] = mapped_column(String(36), ForeignKey("pg_recommendations.id"), nullable=True)
    imported_from_global_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Timelines under this PG live in the shared `timelines` table
    # (Batch 39O UCAT unification, 2026-05-16).
    timelines: Mapped[list["Timeline"]] = relationship(
        "Timeline", back_populates="pg_recommendation",
        foreign_keys="Timeline.pg_recommendation_id",
    )


class SPRecommendation(Base):
    """Specific Problem recommendations — always client-level.

    A specific problem is crop-bound by definition (e.g. "Tomato Late
    Blight"). The SE picks a crop first, then a specific problem from
    that crop's list. `crop_cosh_id` snapshots the crop at create
    time — user clarified 2026-05-10. No bundle dimension (PG's area/
    plant split doesn't apply: the crop already implies the typing).
    """
    __tablename__ = "sp_recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    specific_problem_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    timelines: Mapped[list["Timeline"]] = relationship(
        "Timeline", back_populates="sp_recommendation",
        foreign_keys="Timeline.sp_recommendation_id",
    )
