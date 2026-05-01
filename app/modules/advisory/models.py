import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import (
    String, Text, Boolean, Integer, DateTime, ForeignKey,
    Enum as SAEnum, UniqueConstraint, Index
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
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
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

    locations: Mapped[list["PackageLocation"]] = relationship("PackageLocation", back_populates="package")
    authors: Mapped[list["PackageAuthor"]] = relationship("PackageAuthor", back_populates="package")
    package_variables: Mapped[list["PackageVariable"]] = relationship("PackageVariable", back_populates="package")
    timelines: Mapped[list["Timeline"]] = relationship("Timeline", back_populates="package")

    __table_args__ = (
        UniqueConstraint("client_id", "crop_cosh_id", "name", name="uq_package_client_crop_name"),
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
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[ParameterSource] = mapped_column(SAEnum(ParameterSource), default=ParameterSource.COSH)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    translations: Mapped[list["ParameterTranslation"]] = relationship("ParameterTranslation", back_populates="parameter")
    variables: Mapped[list["Variable"]] = relationship("Variable", back_populates="parameter")


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
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    parameter: Mapped["Parameter"] = relationship("Parameter", back_populates="variables")
    translations: Mapped[list["VariableTranslation"]] = relationship("VariableTranslation", back_populates="variable")
    package_variables: Mapped[list["PackageVariable"]] = relationship("PackageVariable", back_populates="variable")

    __table_args__ = (UniqueConstraint("parameter_id", "name"),)


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
    __tablename__ = "timelines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    package_id: Mapped[str] = mapped_column(String(36), ForeignKey("packages.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    from_type: Mapped[TimelineFromType] = mapped_column(SAEnum(TimelineFromType), nullable=False)
    from_value: Mapped[int] = mapped_column(Integer, nullable=False)
    to_value: Mapped[int] = mapped_column(Integer, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    package: Mapped["Package"] = relationship("Package", back_populates="timelines")
    practices: Mapped[list["Practice"]] = relationship("Practice", back_populates="timeline")
    relations: Mapped[list["Relation"]] = relationship("Relation", back_populates="timeline")
    conditional_questions: Mapped[list["ConditionalQuestion"]] = relationship("ConditionalQuestion", back_populates="timeline")

    __table_args__ = (UniqueConstraint("package_id", "name"),)


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


# ── Domain 4: CHA — Problem Groups and Specific Problems ──────────────────────

class PGRecommendation(Base):
    """Problem Group recommendations — global (client_id=NULL) or client-local."""
    __tablename__ = "pg_recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    problem_group_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=True)
    application_type: Mapped[str] = mapped_column(String(20), nullable=False)
    parent_id: Mapped[str] = mapped_column(String(36), ForeignKey("pg_recommendations.id"), nullable=True)
    imported_from_global_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    timelines: Mapped[list["PGTimeline"]] = relationship("PGTimeline", back_populates="pg_recommendation")


class PGTimeline(Base):
    __tablename__ = "pg_timelines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    pg_recommendation_id: Mapped[str] = mapped_column(String(36), ForeignKey("pg_recommendations.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    from_type: Mapped[str] = mapped_column(String(30), default="DAYS_AFTER_DETECTION")
    from_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    to_value: Mapped[int] = mapped_column(Integer, nullable=False)

    pg_recommendation: Mapped["PGRecommendation"] = relationship("PGRecommendation", back_populates="timelines")
    practices: Mapped[list["PGPractice"]] = relationship("PGPractice", back_populates="timeline")


class PGPractice(Base):
    __tablename__ = "pg_practices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    timeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("pg_timelines.id"), nullable=False)
    l0_type: Mapped[str] = mapped_column(String(20), nullable=False)
    l1_type: Mapped[str] = mapped_column(String(100), nullable=True)
    l2_type: Mapped[str] = mapped_column(String(100), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    is_special_input: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    timeline: Mapped["PGTimeline"] = relationship("PGTimeline", back_populates="practices")
    elements: Mapped[list["PGElement"]] = relationship("PGElement", back_populates="practice")


class PGElement(Base):
    __tablename__ = "pg_elements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    practice_id: Mapped[str] = mapped_column(String(36), ForeignKey("pg_practices.id"), nullable=False)
    element_type: Mapped[str] = mapped_column(String(100), nullable=False)
    cosh_ref: Mapped[str] = mapped_column(String(200), nullable=True)
    value: Mapped[str] = mapped_column(Text, nullable=True)
    unit_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    practice: Mapped["PGPractice"] = relationship("PGPractice", back_populates="elements")


class SPRecommendation(Base):
    """Specific Problem recommendations — always client-level."""
    __tablename__ = "sp_recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    specific_problem_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    application_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    timelines: Mapped[list["SPTimeline"]] = relationship("SPTimeline", back_populates="sp_recommendation")


class SPTimeline(Base):
    __tablename__ = "sp_timelines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sp_recommendation_id: Mapped[str] = mapped_column(String(36), ForeignKey("sp_recommendations.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    from_type: Mapped[str] = mapped_column(String(30), default="DAYS_AFTER_DETECTION")
    from_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    to_value: Mapped[int] = mapped_column(Integer, nullable=False)

    sp_recommendation: Mapped["SPRecommendation"] = relationship("SPRecommendation", back_populates="timelines")
    practices: Mapped[list["SPPractice"]] = relationship("SPPractice", back_populates="timeline")


class SPPractice(Base):
    __tablename__ = "sp_practices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    timeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("sp_timelines.id"), nullable=False)
    l0_type: Mapped[str] = mapped_column(String(20), nullable=False)
    l1_type: Mapped[str] = mapped_column(String(100), nullable=True)
    l2_type: Mapped[str] = mapped_column(String(100), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    is_special_input: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    timeline: Mapped["SPTimeline"] = relationship("SPTimeline", back_populates="practices")
    elements: Mapped[list["SPElement"]] = relationship("SPElement", back_populates="practice")


class SPElement(Base):
    __tablename__ = "sp_elements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    practice_id: Mapped[str] = mapped_column(String(36), ForeignKey("sp_practices.id"), nullable=False)
    element_type: Mapped[str] = mapped_column(String(100), nullable=False)
    cosh_ref: Mapped[str] = mapped_column(String(200), nullable=True)
    value: Mapped[str] = mapped_column(Text, nullable=True)
    unit_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    practice: Mapped["SPPractice"] = relationship("SPPractice", back_populates="elements")
