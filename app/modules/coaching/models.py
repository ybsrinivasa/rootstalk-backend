"""SQLAlchemy models for the Coaching Sandbox — sessions, invites,
students. See `alembic/versions/e7b1c4a09d52_coaching_sandbox.py` for
the DB shape and constraint rationale.
"""
import enum
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    String, DateTime, ForeignKey, Index, JSON, UniqueConstraint,
    CheckConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


def new_invite_token() -> str:
    # 32 hex chars = 128 bits — plenty for an emailed one-time link.
    return secrets.token_hex(32)


# ── Enums ─────────────────────────────────────────────────────────────────

class CoachingSessionStatus(str, enum.Enum):
    """Session lifecycle:

    DRAFT: coach still adding students, session not yet startable
        by students. Can transition to ACTIVE (start button) or be
        deleted outright while empty.
    ACTIVE: coach clicked Start. Roster is frozen (no new students).
        Students can log in. 30-day auto-close clock counts from
        `started_at`.
    CLOSED_MANUAL: coach ended the session early.
    CLOSED_AUTO: 30 days elapsed since started_at.

    See DB-side chk_coaching_session_status_shape for the timestamp
    invariants each status implies.
    """
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    CLOSED_MANUAL = "CLOSED_MANUAL"
    CLOSED_AUTO = "CLOSED_AUTO"


class CoachingGrade(str, enum.Enum):
    """Certification grade — set alongside `certified_at` on the
    CoachingStudent row. Only rows with both `grade` non-NULL AND
    `certified_at` non-NULL are considered certified. Uncertifying
    clears both.

    Locked with user 2026-09-01:
      SATISFACTORY — met the bar
      GOOD         — exceeded expectations
      EXCELLENT    — outstanding
    Absence of a grade (NULL) means Not Certified.
    """
    SATISFACTORY = "SATISFACTORY"
    GOOD = "GOOD"
    EXCELLENT = "EXCELLENT"


class CoachingInviteStatus(str, enum.Enum):
    """Invite lifecycle: INVITED (coach created, email sent) →
    SUBMITTED (student filled the form) → APPROVED (coach approved,
    CoachingStudent + workspace provisioned) or REJECTED (coach said
    no). Expiry independent of status — an INVITED or SUBMITTED
    invite can also expire without decision."""
    INVITED = "INVITED"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# Sessions that are still in play (not closed). Used all over the
# app-layer gates ("this session is over → can't add students / student
# can't log in / etc.") and by the auto-close celery task.
OPEN_SESSION_STATUSES = frozenset({
    CoachingSessionStatus.DRAFT,
    CoachingSessionStatus.ACTIVE,
})

# Default invite validity window before the student's link stops working.
INVITE_EXPIRY_DAYS = 14

# Auto-close window after a session moves to ACTIVE.
SESSION_DURATION_DAYS = 30


# ── Tables ────────────────────────────────────────────────────────────────

class CoachingSession(Base):
    __tablename__ = "coaching_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # The user who created the session. Must have COACH role (or be SA
    # — implicit coach). Enforced at the endpoint layer, not the DB.
    coach_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # The real client this session is "for". Students in this session
    # are being groomed to work with this client (though the coach can
    # place a certified student elsewhere too — this is just the
    # primary target). Coaching workspaces set `parent_client_id` to
    # this same reference client.
    reference_client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clients.id", ondelete="RESTRICT"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    closed_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    invites: Mapped[list["CoachingStudentInvite"]] = relationship(
        "CoachingStudentInvite", back_populates="session",
        cascade="all, delete-orphan",
    )
    students: Mapped[list["CoachingStudent"]] = relationship(
        "CoachingStudent", back_populates="session",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # Mirror of the DB-side CHECK — kept for ORM-level validation
        # (raised as IntegrityError before the round-trip).
        CheckConstraint(
            "(status = 'DRAFT' AND started_at IS NULL AND closed_at IS NULL) "
            "OR (status = 'ACTIVE' AND started_at IS NOT NULL AND closed_at IS NULL) "
            "OR (status IN ('CLOSED_MANUAL', 'CLOSED_AUTO') AND started_at IS NOT NULL AND closed_at IS NOT NULL)",
            name="chk_coaching_session_status_shape",
        ),
    )

    def is_open(self) -> bool:
        return self.status in {s.value for s in OPEN_SESSION_STATUSES}

    def auto_close_due(self, now: Optional[datetime] = None) -> bool:
        """True when an ACTIVE session has crossed its 30-day mark."""
        if self.status != CoachingSessionStatus.ACTIVE.value:
            return False
        if self.started_at is None:
            return False
        return (now or utcnow()) >= self.started_at + timedelta(days=SESSION_DURATION_DAYS)


class CoachingStudentInvite(Base):
    __tablename__ = "coaching_student_invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("coaching_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    invite_token: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, default=new_invite_token,
    )

    # Populated when the student submits the self-registration form.
    # JSON schema (kept flexible so the form can evolve without a
    # migration): {"name": str, "year_of_birth": int, "address": str,
    # "organization": str, "phone": str}
    submitted_form: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    approved_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )

    session: Mapped["CoachingSession"] = relationship(
        "CoachingSession", back_populates="invites",
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id", "email", name="uq_coaching_invite_session_email",
        ),
        CheckConstraint(
            "status IN ('INVITED', 'SUBMITTED', 'APPROVED', 'REJECTED')",
            name="chk_coaching_invite_status",
        ),
        Index(
            "ix_coaching_invites_status", "session_id", "status",
        ),
    )

    def is_actionable(self) -> bool:
        """True when the coach can still approve/reject this invite."""
        return self.status == CoachingInviteStatus.SUBMITTED.value

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return (now or utcnow()) >= self.expires_at


class CoachingStudent(Base):
    __tablename__ = "coaching_students"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("coaching_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The student's user identity. Created at approval time — fresh
    # user row so no accidental collision with a real user's account.
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # The student's isolated coaching workspace, provisioned at
    # approval. `Client.is_coaching=true`, `Client.parent_client_id`
    # points at the session's reference client, `parent_session_id`
    # points at the session. Nobody else can log into this workspace —
    # the student is its sole CA.
    workspace_client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clients.id", ondelete="RESTRICT"),
        nullable=False, unique=True,
    )

    # The one phone the student may use to log into the PWA. Set at
    # self-registration; auth service refuses OTP requests for any
    # other number this student user submits.
    approved_phone: Mapped[str] = mapped_column(String(15), nullable=False)

    # PWA roles the coach granted this student (JSON list of RoleType
    # value strings — FARMER, DEALER, FACILITATOR, FARM_PUNDIT). A
    # student can hold DEALER + FACILITATOR simultaneously in the
    # coaching context — the usual exclusion is relaxed for
    # is_coaching=true workspaces.
    assigned_pwa_roles: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    certified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    certified_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Certification grade — SATISFACTORY / GOOD / EXCELLENT. Set
    # alongside certified_at; NULL = not certified. See CoachingGrade.
    grade: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Digital certificate (2026-09-01). Only meaningful when
    # certified_at + grade are set. certificate_number doubles as the
    # public verification slug at /verify/<cert_number>. Regeneration
    # (e.g. grade updated) refreshes generated_at + pdf_url and keeps
    # the same certificate_number so verification URLs stay stable.
    certificate_number: Mapped[Optional[str]] = mapped_column(
        String(36), unique=True, nullable=True,
    )
    certificate_generated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    certificate_pdf_url: Mapped[Optional[str]] = mapped_column(
        String(2048), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )

    session: Mapped["CoachingSession"] = relationship(
        "CoachingSession", back_populates="students",
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id", "user_id", name="uq_coaching_student_session_user",
        ),
        Index("ix_coaching_students_session_id", "session_id"),
    )

    def is_certified(self) -> bool:
        return self.certified_at is not None
