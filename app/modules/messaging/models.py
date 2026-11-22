"""Messaging Module — data model.

See `/Users/ybsrinivasa/Downloads/RootsTalk_Messaging_Spec_Addendum_2026-11-22.md`
for the full design. Summary:

Campaign is the parent record authored in the Client Portal by a CA/RM.
Four `campaign_type` values (ANNOUNCEMENT / INTEREST_CAPTURE / FEEDBACK
/ SURVEY) determine what recipients see and whether a response form
appears. A campaign targets one `recipient_role` (FARMER / DEALER /
FACILITATOR) — multi-role is deferred to v2.

At send time, the system computes the audience from `audience_filters`
and writes one `CampaignRecipient` row per match — the audience is
frozen at send so response-rate denominators stay stable even as live
data drifts. Delivery progresses by stamping `pushed_at` / `opened_at`
/ `responded_at` on each recipient row.

For SURVEY and FEEDBACK follow-ups, questions live in
`campaign_questions`. Responses are in `campaign_responses` (one per
recipient) with per-question answers in `campaign_response_answers`.
The per-answer table is polymorphic — one of five value_* columns is
populated per question-type.

`campaign_send_audits` captures who sent what and when, for compliance
review in the SA Portal. Same append-only pattern as
`report_csv_downloads`.
"""
import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Index, Integer,
    JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


def _new_uuid():
    return str(uuid.uuid4())


# ── Enums (stored as String for forward-compatibility) ─────────────────────

class CampaignType(str, enum.Enum):
    ANNOUNCEMENT = "ANNOUNCEMENT"         # no response form
    INTEREST_CAPTURE = "INTEREST_CAPTURE" # built-in Yes/No/Maybe
    FEEDBACK = "FEEDBACK"                 # 5-star + optional follow-ups
    SURVEY = "SURVEY"                     # multi-question form


class CampaignStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


class RecipientRole(str, enum.Enum):
    FARMER = "FARMER"
    DEALER = "DEALER"
    FACILITATOR = "FACILITATOR"


class QuestionType(str, enum.Enum):
    SINGLE_CHOICE = "SINGLE_CHOICE"
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE"
    YES_NO = "YES_NO"
    RATING_SCALE = "RATING_SCALE"
    NUMBER = "NUMBER"
    SHORT_TEXT = "SHORT_TEXT"
    PARAGRAPH_TEXT = "PARAGRAPH_TEXT"
    DATE = "DATE"
    PHOTO_UPLOAD = "PHOTO_UPLOAD"


class AttachmentKind(str, enum.Enum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    PDF = "PDF"


# Subscription-status audience filter values (farmers only). Stored
# inside `Campaign.audience_filters.subscription_status` — this enum is
# here as the vocabulary check in the API layer.
SUBSCRIPTION_STATUS_FILTER_VALUES = frozenset({
    "ALL",
    "CURRENTLY_ACTIVE",
    "WAITLISTED",
    "PAST",
    "NEVER_SUBSCRIBED",
})


# ── Tables ─────────────────────────────────────────────────────────────────

class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        Index("ix_campaigns_client_status", "client_id", "status"),
        Index("ix_campaigns_scheduled_at", "scheduled_at"),
        Index("ix_campaigns_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False,
    )
    # Internal name for the dashboard list — never shown to recipients.
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    campaign_type: Mapped[str] = mapped_column(String(30), nullable=False)
    recipient_role: Mapped[str] = mapped_column(String(20), nullable=False)
    # Internal note; purpose/context/special instructions.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Content shown to recipient
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    # Audience filter snapshot. Stored as JSON so new filters can be
    # added without a schema change. Known keys (all optional):
    #   state_cosh_id, district_cosh_id, crop_cosh_id,
    #   subscription_status, promoter_user_id
    audience_filters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # For FEEDBACK — scale type. "STAR_5" or "NPS_10" (NPS deferred;
    # v1 ships STAR_5 only but the column accommodates NPS later).
    rating_scale_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Lifecycle
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CampaignStatus.DRAFT.value,
    )
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Denormalised analytics counters for fast list rendering.
    # Authoritative source is the campaign_recipients table; these
    # are maintained in-process by the send/push/open/response flows.
    recipient_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pushed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opened_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    responded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Audit
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )


class CampaignAttachment(Base):
    __tablename__ = "campaign_attachments"
    __table_args__ = (
        Index("ix_campaign_attachments_campaign", "campaign_id", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # GCS object path. Signed URLs are generated at serve time from
    # this value — the raw path is not shipped to recipients.
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(300), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )


class CampaignQuestion(Base):
    """One row per question in a SURVEY (or FEEDBACK follow-up).

    INTEREST_CAPTURE has NO rows here — the Yes/No/Maybe is built-in.
    ANNOUNCEMENT has NO rows here — no response form at all.
    """
    __tablename__ = "campaign_questions"
    __table_args__ = (
        Index("ix_campaign_questions_campaign_sort", "campaign_id", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    question_type: Mapped[str] = mapped_column(String(30), nullable=False)
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    help_text: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # SINGLE_CHOICE / MULTIPLE_CHOICE — list of option strings.
    choice_options: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # NUMBER — unit label (quintals, kg, Rs., etc.)
    number_unit: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # RATING_SCALE — "STAR_5" or "NPS_10"; defaults to STAR_5.
    rating_scale_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )


class CampaignRecipient(Base):
    """Audience snapshot written at send-time. One row per
    (campaign, user). Frozen — never recomputed from live data.

    Delivery progress is tracked by stamping the three *_at timestamps.
    Denormalised recipient_* columns allow analytics tables to render
    the recipient without a join to users (which may have been deleted
    or anonymised by the time a CA opens the list months later).
    """
    __tablename__ = "campaign_recipients"
    __table_args__ = (
        UniqueConstraint("campaign_id", "user_id", name="uq_campaign_recipient"),
        Index("ix_campaign_recipients_campaign", "campaign_id"),
        Index("ix_campaign_recipients_user_pending", "user_id", "campaign_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        # Do NOT cascade delete — if a recipient's user record is later
        # deleted, we keep the row (SET NULL on user_id) for audit
        # continuity. Response history stays attached.
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    recipient_name: Mapped[str] = mapped_column(String(200), nullable=False)
    recipient_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_language_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    # Denormalised at snapshot for later geographic analytics (if ever
    # added) and to render location in the individual-response view.
    recipient_state_cosh_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    recipient_district_cosh_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    pushed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    responded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Test-send marker. Test recipients do NOT count toward campaign
    # analytics (recipient_count, response_rate, etc.). The row still
    # exists so we can show the CA their test went out and which test
    # account saw what.
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )


class CampaignResponse(Base):
    """One row per responding recipient. Per-question answers live in
    campaign_response_answers.

    For ANNOUNCEMENT: no row (no response collected).
    For INTEREST_CAPTURE: one row with interest_answer populated.
    For FEEDBACK: one row with rating populated + optional follow-up
      answers in campaign_response_answers.
    For SURVEY: one row + N answers.
    """
    __tablename__ = "campaign_responses"
    __table_args__ = (
        UniqueConstraint("campaign_id", "user_id", name="uq_campaign_response"),
        Index("ix_campaign_responses_campaign", "campaign_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    recipient_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaign_recipients.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Populated for INTEREST_CAPTURE. Enum string: YES / NO / MAYBE.
    interest_answer: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    # Populated for FEEDBACK — scale defined at campaign level.
    rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )


class CampaignResponseAnswer(Base):
    """One row per (response, question). Polymorphic value storage —
    exactly one of the value_* columns is populated per row depending
    on the question's type.

    Shape chosen over multiple per-type tables for v1 — single table
    is simpler to query for CSV export + aggregate analytics; scale
    is nowhere near where the alternative would pay off.
    """
    __tablename__ = "campaign_response_answers"
    __table_args__ = (
        UniqueConstraint("response_id", "question_id", name="uq_response_question"),
        Index("ix_campaign_response_answers_response", "response_id"),
        Index("ix_campaign_response_answers_question", "question_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    response_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaign_responses.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaign_questions.id", ondelete="CASCADE"),
        nullable=False,
    )

    value_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    value_number: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # SINGLE_CHOICE: the chosen option string.
    # MULTIPLE_CHOICE: JSON array of chosen option strings.
    value_choice: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    value_boolean: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    value_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    # GCS storage path for PHOTO_UPLOAD. Signed URLs built at read time.
    value_media_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )


class CampaignSendAudit(Base):
    """Compliance evidence: one row per campaign send. Append-only.
    Mirror of the report_csv_downloads pattern shipped 2026-11-21.

    Not strictly needed for the product flow — the Campaign row itself
    records sent_at + created_by_user_id. This table makes "show me
    every send event by this user across every client" a one-query
    answer for the SA Portal audit viewer.
    """
    __tablename__ = "campaign_send_audits"
    __table_args__ = (
        Index("ix_campaign_send_audits_sent_at", "sent_at"),
        Index("ix_campaign_send_audits_client", "client_id"),
        Index("ix_campaign_send_audits_sender", "sender_user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        # SET NULL so a campaign hard-delete (shouldn't happen in v1
        # but be defensive) doesn't wipe the audit row.
        ForeignKey("campaigns.id", ondelete="SET NULL"),
        nullable=True,
    )
    client_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("clients.id", ondelete="SET NULL"),
        nullable=True,
    )
    sender_user_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Snapshotted at send so the audit row is readable even after the
    # campaign is renamed / archived.
    campaign_name: Mapped[str] = mapped_column(String(200), nullable=False)
    campaign_type: Mapped[str] = mapped_column(String(30), nullable=False)
    recipient_role: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # True when this was a test-send (small subset to verify before
    # the real send). Audit still captures it.
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
