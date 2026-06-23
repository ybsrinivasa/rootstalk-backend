import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import String, Text, Boolean, Integer, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)

def new_uuid():
    return str(uuid.uuid4())


class QueryStatus(str, enum.Enum):
    NEW = "NEW"
    FORWARDED = "FORWARDED"
    RETURNED = "RETURNED"
    RESPONDED = "RESPONDED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class QueryRemarkAction(str, enum.Enum):
    RECEIVED = "RECEIVED"
    FORWARDED = "FORWARDED"
    RETURNED = "RETURNED"
    RESPONDED = "RESPONDED"
    REJECTED = "REJECTED"


class PunditRole(str, enum.Enum):
    PRIMARY = "PRIMARY"
    PANEL = "PANEL"
    # Promoter-Pundit (2026-06-23). Distinct role designation — a
    # promoter-pundit cannot also be a regular pundit (PRIMARY / PANEL)
    # at the same client, and a user can only hold this role at one
    # client at a time (because they are a Promoter for exactly one
    # company). Pre-2026-06-23 was modelled as `is_promoter_pundit=True`
    # flag on top of `role=PANEL` — removed in migration b8e4a72f3019.
    PROMOTER_PUNDIT = "PROMOTER_PUNDIT"


class FarmPunditProfile(Base):
    """Regular FarmPundit registration profile.

    Scope: this profile and every related junction (expertise,
    languages, crop_groups, farming_methods, cultivation_types,
    support_areas) only apply to regular FarmPundits. Promoter-
    Pundits are designated through the CA portal (Promoter UI) and
    don't fill any of this — they skip the /pundit/register flow
    entirely. See `client_farm_pundits.role = PROMOTER_PUNDIT`.

    All single-value dropdowns store the selection as a Cosh
    `cosh_core_items.cosh_id`, against these `core_type` slugs:
        education_cosh_id          → pundit_education
        experience_cosh_id         → pundit_experience
        organisation_type_cosh_id  → pundit_organization_types
    The companion multi-select tables key against:
        farm_pundit_farming_methods    → pundit_farming_methods
        farm_pundit_cultivation_types  → pundit_cultivation_types
        farm_pundit_expertise.domain   → pundit_domain_expertise
        farm_pundit_languages.language_code → pundit_languages
        farm_pundit_crop_groups.crop_group_cosh_id → pundit_crop_groups
        farm_pundit_support_areas.state_cosh_id → state_list

    Employment flag (`is_employed_by_organization`):
      True  → `organisation_type_cosh_id` is the selection;
              `non_employed_kind` is NULL.
      False → `organisation_type_cosh_id` is NULL;
              `non_employed_kind` is optionally one of
              RETIRED / EXPERIENCED_FARMER.
    No history is kept on toggle — only the latest answer survives.
    """
    __tablename__ = "farm_pundit_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=True)
    education_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    experience_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    is_employed_by_organization: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    organisation_type_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    non_employed_kind: Mapped[str] = mapped_column(String(30), nullable=True)
    phone_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    declaration_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company_pundits: Mapped[list["ClientFarmPundit"]] = relationship("ClientFarmPundit", back_populates="pundit")
    queries_holding: Mapped[list["Query"]] = relationship("Query", back_populates="current_holder",
                                                           foreign_keys="Query.current_holder_id")


class FarmPunditFarmingMethod(Base):
    __tablename__ = "farm_pundit_farming_methods"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    farming_method_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (UniqueConstraint("pundit_id", "farming_method_cosh_id"),)


class FarmPunditCultivationType(Base):
    __tablename__ = "farm_pundit_cultivation_types"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    cultivation_type_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (UniqueConstraint("pundit_id", "cultivation_type_cosh_id"),)


class FarmPunditExpertise(Base):
    __tablename__ = "farm_pundit_expertise"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    domain: Mapped[str] = mapped_column(String(100), nullable=False)


class FarmPunditSupportArea(Base):
    __tablename__ = "farm_pundit_support_areas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    state_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    district_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)


class FarmPunditLanguage(Base):
    __tablename__ = "farm_pundit_languages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    # Column historically held 2-letter ISO codes ("en"); post-Cosh-reshape
    # (2026-05-26) it stores a Cosh `pundit_languages` UUID. Widened to 100
    # in migration b3e4f7a52d11 to match the sibling cosh_id columns.
    language_code: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (UniqueConstraint("pundit_id", "language_code"),)


class FarmPunditCropGroup(Base):
    __tablename__ = "farm_pundit_crop_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    crop_group_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)


class FarmPunditPreference(Base):
    """Farmer's preferred FarmPundit for a specific subscription (BL-12a priority 1)."""
    __tablename__ = "farm_pundit_preferences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), unique=True, nullable=False)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QueryResponseMedia(Base):
    __tablename__ = "query_response_media"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    response_id: Mapped[str] = mapped_column(String(36), ForeignKey("query_responses.id"), nullable=False)
    media_type: Mapped[str] = mapped_column(String(20), nullable=False)  # IMAGE|VIDEO|AUDIO|HYPERLINK
    url: Mapped[str] = mapped_column(Text, nullable=False)
    caption: Mapped[str] = mapped_column(String(500), nullable=True)


class ClientFarmPundit(Base):
    """Company's onboarded FarmPundits."""
    __tablename__ = "client_farm_pundits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    role: Mapped[PunditRole] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    round_robin_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    # PP V1 (2026-05-30): phantom-pundit Option A. When the CA toggles
    # PP ON for a Facilitator-Promoter without a FarmPundit profile, we
    # auto-provision a row with `searchable=False` so the farmer never
    # sees them in any pundit picker. Real FarmPundit onboardings keep
    # the default True. Per-(client, pundit) flag — a person can be
    # searchable at one company and a phantom-only PP at another.
    searchable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    onboarded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pundit: Mapped["FarmPunditProfile"] = relationship("FarmPunditProfile", back_populates="company_pundits")

    __table_args__ = (UniqueConstraint("client_id", "pundit_id"),)


class PunditInvitation(Base):
    __tablename__ = "pundit_invitations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    role: Mapped[PunditRole] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Query(Base):
    __tablename__ = "queries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    farmer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    crop_age: Mapped[str] = mapped_column(String(100), nullable=True)
    # Mandatory at the API layer (2026-05-27). Nullable in DB so old
    # queries submitted under the pre-Cosh free-text shape still load.
    query_type_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    # Auto-derived from `query_type_cosh_id` (resolved Cosh translation)
    # at submit time so existing list views keep working without
    # rewiring. The farmer no longer types a title.
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    current_holder_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=True)
    status: Mapped[QueryStatus] = mapped_column(String(20), default=QueryStatus.NEW)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    razorpay_payment_id: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 2026-06-20 — Set when the farmer first opens their per-sub
    # queries page on a RESPONDED row. Drives the dashboard-attention
    # badge: a RESPONDED query with viewed_at NULL still counts;
    # once read, it drops off the count. Migration: a1baba6ad6fd.
    viewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    current_holder: Mapped["FarmPunditProfile"] = relationship("FarmPunditProfile", back_populates="queries_holding",
                                                                 foreign_keys=[current_holder_id])
    remarks: Mapped[list["QueryRemark"]] = relationship("QueryRemark", back_populates="query")
    response: Mapped["QueryResponse"] = relationship("QueryResponse", back_populates="query", uselist=False)


class QueryMedia(Base):
    __tablename__ = "query_media"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    query_id: Mapped[str] = mapped_column(String(36), ForeignKey("queries.id"), nullable=False)
    media_type: Mapped[str] = mapped_column(String(20), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)


class QueryRemark(Base):
    __tablename__ = "query_remarks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    query_id: Mapped[str] = mapped_column(String(36), ForeignKey("queries.id"), nullable=False)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=True)
    action: Mapped[QueryRemarkAction] = mapped_column(String(20), nullable=False)
    forwarded_to_pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=True)
    remark: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    query: Mapped["Query"] = relationship("Query", back_populates="remarks")


class QueryResponse(Base):
    __tablename__ = "query_responses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    query_id: Mapped[str] = mapped_column(String(36), ForeignKey("queries.id"), unique=True, nullable=False)
    pundit_id: Mapped[str] = mapped_column(String(36), ForeignKey("farm_pundit_profiles.id"), nullable=False)
    problem_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=True)
    standard_response_id: Mapped[str] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    query: Mapped["Query"] = relationship("Query", back_populates="response")


class StandardResponse(Base):
    """Company Q&A library — spec §14.9 (UCAT third pipe).

    Subject Experts curate a library of question + Timeline-rooted
    advisories for their company. FarmPundits pick the closest-
    matching standard response while responding to farmer queries;
    the response's Timelines (with their full Practice → Element
    structure) merge into the farmer's advisory just like a PG/SP
    CHA recommendation, with a Pundit-origin icon on the cards.

    UCAT (Universal Crop Advisory Template): the Timeline → Practice
    → Element shape is identical across all three advisory pipes
    (CCA / CHA / Q&A). The only thing that differs is the trigger /
    anchor — for Q&A, the trigger is the Pundit's pick and the
    anchor unit is "days after response delivered to farmer".

    `crop_cosh_id` nullable supports both crop-specific and crop-
    agnostic entries per spec.

    Schema reuse: a Q&A timeline is just a row in `pg_timelines`
    with `standard_response_id` set and `pg_recommendation_id=NULL`.
    The CHECK constraint `pg_timelines_one_parent_chk` enforces
    exactly-one parent. Practices and Elements are reused as-is
    (they FK to timeline_id only). Adding QA cost zero new tables.
    """
    __tablename__ = "standard_responses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(36), ForeignKey("clients.id"), nullable=False)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    # DRAFT → ACTIVE (one-time publish, with a CA-side confirmation gate)
    # → INACTIVE ↔ ACTIVE thereafter. Only ACTIVE rows are visible to
    # Pundits. No version history — edits to an ACTIVE row propagate
    # immediately; the Inactive toggle is the curator's hide affordance
    # during rewrites.
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    # Timelines under this Q&A standard response live in the shared
    # `timelines` table (Batch 39O UCAT unification, 2026-05-16).
    timelines: Mapped[list["Timeline"]] = relationship(
        "Timeline", back_populates="standard_response",
        foreign_keys="Timeline.standard_response_id",
    )
