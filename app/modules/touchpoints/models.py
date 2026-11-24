"""Subscription Touchpoints — data model.

Companion to the Messaging Module (`app/modules/messaging/`). A
Touchpoint is a piece of content (same 4 types as Messaging:
Announcement / Interest Capture / Feedback / Survey) + one or more
PoP Assignments that bind it to a (Crop → Package → Timeline →
Beginning/End) milestone. The system fires the touchpoint to every
farmer whose subscription reaches that milestone on or after the
assignment's activation date (non-retroactive).

Why parallel `touchpoint_*` tables instead of extending `campaigns`
with a type column:
- Audience semantics differ fundamentally. Campaigns have a frozen
  recipient snapshot computed at send time; Touchpoints have an
  implicit audience defined by live subscription state, with one
  firing per (assignment, subscription) crossing.
- Lifecycle differs. Campaigns move Draft → Active → Expired.
  Touchpoints move Draft → Active → Paused → Archived (resumable).
  Active keeps firing indefinitely until paused or archived.
- The two features can evolve question types, response shapes, and
  firing semantics independently. Shared parent would mean every
  Messaging change needs a Touchpoint audit and vice versa.

UI widgets (composer form, question editor, PWA response widgets)
are reused across both features — only the data layer is parallel.

Recipient firings (`touchpoint_recipients`) are themselves the
audit trail — fired_at + context columns (subscription_id,
assignment_id, crop_cosh_id, timeline_id, fire_at) capture
everything needed for compliance review. No separate audit table.
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


# ── Enums ──────────────────────────────────────────────────────────────

class TouchpointStatus(str, enum.Enum):
    DRAFT = "DRAFT"          # content being authored; no firings
    ACTIVE = "ACTIVE"        # ≥1 active assignment; firings happen
    PAUSED = "PAUSED"        # globally paused; no firings until resumed
    ARCHIVED = "ARCHIVED"    # terminal; hidden from default lists


class TouchpointContentType(str, enum.Enum):
    """Same string values as messaging.CampaignType so composer and
    PWA widgets can be reused. Re-declared here (not imported) to
    keep the touchpoint module self-contained."""
    ANNOUNCEMENT = "ANNOUNCEMENT"
    INTEREST_CAPTURE = "INTEREST_CAPTURE"
    FEEDBACK = "FEEDBACK"
    SURVEY = "SURVEY"


class TouchpointFireAt(str, enum.Enum):
    BEGINNING = "BEGINNING"  # fires when a sub enters this Timeline
    END = "END"              # fires when a sub exits this Timeline


class TouchpointAssignmentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


class TouchpointQuestionType(str, enum.Enum):
    """9 question types — same values as messaging.QuestionType."""
    SINGLE_CHOICE = "SINGLE_CHOICE"
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE"
    YES_NO = "YES_NO"
    RATING_SCALE = "RATING_SCALE"
    NUMBER = "NUMBER"
    SHORT_TEXT = "SHORT_TEXT"
    PARAGRAPH_TEXT = "PARAGRAPH_TEXT"
    DATE = "DATE"
    PHOTO_UPLOAD = "PHOTO_UPLOAD"


class TouchpointAttachmentKind(str, enum.Enum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    PDF = "PDF"


# ── Tables ─────────────────────────────────────────────────────────────

class Touchpoint(Base):
    __tablename__ = "touchpoints"
    __table_args__ = (
        Index("ix_touchpoints_client_status", "client_id", "status"),
        Index("ix_touchpoints_updated_at", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False,
    )

    # Internal name shown in the CP management list; never shown to recipients.
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Shown to recipient
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    language_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    # FEEDBACK only; "STAR_5" or "NPS_10" (NPS deferred, v1 ships STAR_5).
    rating_scale_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Expiry window applied to each individual firing (not the touchpoint
    # itself). Minimum 1 day per spec, maximum 180 days. A fired
    # recipient has `fired_at + per_firing_expiry_days` to respond
    # before their copy expires from their inbox.
    per_firing_expiry_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7,
    )

    # Lifecycle
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TouchpointStatus.DRAFT.value,
    )
    activated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    paused_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Set on first recipient's open_at. Locks content edits per spec
    # §4.2 (lock-on-first-open settled during the Messaging audit).
    # Composer PATCH rejects content updates when this is non-null.
    content_locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

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


class TouchpointAssignment(Base):
    """One row per (touchpoint × package × timeline × fire_at).
    activated_at is the critical non-retroactive gate — the firing
    engine only fires for subscriptions reaching the milestone on or
    after this timestamp.
    """
    __tablename__ = "touchpoint_assignments"
    __table_args__ = (
        UniqueConstraint(
            "touchpoint_id", "package_id", "timeline_id", "fire_at",
            name="uq_touchpoint_assignment",
        ),
        # The sweep walks assignments by (package, timeline, fire_at)
        # to match subscription milestones. This index is the hot path.
        Index(
            "ix_touchpoint_assignments_lookup",
            "package_id", "timeline_id", "fire_at", "status",
        ),
        Index("ix_touchpoint_assignments_touchpoint", "touchpoint_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    touchpoint_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("touchpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    package_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("packages.id", ondelete="CASCADE"),
        nullable=False,
    )
    timeline_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("timelines.id", ondelete="CASCADE"),
        nullable=False,
    )
    fire_at: Mapped[str] = mapped_column(String(20), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False,
        default=TouchpointAssignmentStatus.ACTIVE.value,
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    paused_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )


class TouchpointQuestion(Base):
    """Same shape as campaign_questions. Separate table so
    touchpoint question types can evolve independently."""
    __tablename__ = "touchpoint_questions"
    __table_args__ = (
        Index(
            "ix_touchpoint_questions_touchpoint_sort",
            "touchpoint_id", "sort_order",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    touchpoint_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("touchpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    question_type: Mapped[str] = mapped_column(String(30), nullable=False)
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    help_text: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    choice_options: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    number_unit: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    rating_scale_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )


class TouchpointAttachment(Base):
    """Same shape as campaign_attachments. S3 public URLs per the
    Messaging convention — storage_path IS the full URL."""
    __tablename__ = "touchpoint_attachments"
    __table_args__ = (
        Index("ix_touchpoint_attachments_touchpoint", "touchpoint_id", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    touchpoint_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("touchpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(300), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )


class TouchpointRecipient(Base):
    """One row per firing — the firing engine writes one every time
    a subscription crosses a milestone that matches an active
    assignment. Doubles as the audit trail (fired_at + context
    columns record the full provenance).

    The UNIQUE on (assignment_id, subscription_id) ensures the same
    (assignment, subscription) pair never fires twice — if the beat
    task runs more than once on the same day, no duplicate row is
    created. A farmer subscribed to multiple PoPs still gets one
    firing per (assignment × subscription) — same touchpoint
    assigned to Chilli and Tomato PoPs fires twice.

    Denormalised context columns (crop_cosh_id, timeline_id, fire_at,
    recipient_*) avoid joins in the hot analytics path and preserve
    audit continuity even after a subscription or user is anonymised.
    """
    __tablename__ = "touchpoint_recipients"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "subscription_id",
            name="uq_touchpoint_recipient",
        ),
        Index("ix_touchpoint_recipients_touchpoint", "touchpoint_id"),
        Index("ix_touchpoint_recipients_user", "user_id", "touchpoint_id"),
        Index("ix_touchpoint_recipients_fired_at", "fired_at"),
        Index("ix_touchpoint_recipients_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    touchpoint_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("touchpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    assignment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("touchpoint_assignments.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        # SET NULL so audit chain survives subscription hard-delete
        # (rare — Subscriptions use soft-delete by default).
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Denorm context — captured at firing time so analytics + CSV
    # export + inbox rendering don't need to re-join.
    crop_cosh_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    timeline_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    timeline_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    fire_at: Mapped[str] = mapped_column(String(20), nullable=False)

    # Denorm recipient — survives user deletion/anonymisation for audit.
    recipient_name: Mapped[str] = mapped_column(String(200), nullable=False)
    recipient_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_language_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    recipient_state_cosh_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    recipient_district_cosh_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Delivery tracking
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    pushed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    responded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class TouchpointResponse(Base):
    """One row per recipient response. Shape-identical to
    campaign_responses for widget reuse."""
    __tablename__ = "touchpoint_responses"
    __table_args__ = (
        # One response per firing. recipient_id is already unique per
        # firing (via touchpoint_recipients' own UNIQUE), so this just
        # enforces no-duplicate-response-per-firing.
        UniqueConstraint("recipient_id", name="uq_touchpoint_response"),
        Index("ix_touchpoint_responses_touchpoint", "touchpoint_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    touchpoint_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("touchpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    recipient_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("touchpoint_recipients.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Populated for INTEREST_CAPTURE
    interest_answer: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    # Populated for FEEDBACK
    rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )


class TouchpointResponseAnswer(Base):
    """Per-question answer; polymorphic value_* columns.
    Shape-identical to campaign_response_answers."""
    __tablename__ = "touchpoint_response_answers"
    __table_args__ = (
        UniqueConstraint(
            "response_id", "question_id",
            name="uq_touchpoint_response_question",
        ),
        Index("ix_touchpoint_response_answers_response", "response_id"),
        Index("ix_touchpoint_response_answers_question", "question_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    response_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("touchpoint_responses.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("touchpoint_questions.id", ondelete="CASCADE"),
        nullable=False,
    )
    value_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    value_number: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    value_choice: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    value_boolean: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    value_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    value_media_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
