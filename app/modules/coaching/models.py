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
    String, Text, DateTime, ForeignKey, Index, JSON, UniqueConstraint,
    CheckConstraint, text,
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


class CoachingSessionAccessState(str, enum.Enum):
    """Access state for a coaching session, derived at runtime from
    its status + closed_at timestamp. Not a persisted column — the
    auth layer computes this on each login + request so the state
    advances with the clock without needing a cron sweep.

    Lifecycle (hybrid access model, settled 2026-10-15):

    NOT_YET_STARTED: session is DRAFT. Student can't log in yet.
    ACTIVE: session is ACTIVE. Student has full writeable access.
    GRACE: session is CLOSED_* AND we're within 30 days of closed_at.
      Student retains full writeable access to practise what they
      learned. Portal + PWA both allowed.
    ARCHIVED: session is CLOSED_* AND 30+ days past closed_at.
      Portal login allowed but mints a read-only token
      (student_readonly=True claim); PWA login refused with a clear
      "visit the portal archive" message.

    The grace cutoff is `closed_at + GRACE_DURATION_DAYS` at the
    moment of the check. A token minted during GRACE naturally
    expires its writeable window when the next request lands past
    the grace cutoff — the dependency-layer check in get_current_user
    enforces the hard-cut.
    """
    NOT_YET_STARTED = "NOT_YET_STARTED"
    ACTIVE = "ACTIVE"
    GRACE = "GRACE"
    ARCHIVED = "ARCHIVED"


class CoachingInviteStatus(str, enum.Enum):
    """Invite lifecycle: INVITED (coach created, email sent) →
    SUBMITTED (student filled the form) → APPROVED (coach approved,
    CoachingStudent + workspace provisioned) or REJECTED (coach said
    no). Expiry independent of status — an INVITED or SUBMITTED
    invite can also expire without decision.

    VOID (2026-09-12): coach regenerated the link before approval.
    The old invite becomes VOID and a fresh row is created with a new
    token; VOID rows are excluded from anti-dupe checks so the new
    invite for the same email passes.
    """
    INVITED = "INVITED"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    VOID = "VOID"


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

# Hybrid-access grace window after a session closes (2026-10-15).
# Student keeps full writeable access for this many days; after the
# window they transition to ARCHIVED (portal read-only, PWA refused).
GRACE_DURATION_DAYS = 30

# Coaching Sandbox — subscription units auto-granted to a student's
# workspace at approval time (2026-09-03). Lets the student practise
# the CA → F-P/D-P → Farmer assignment flow without ever hitting the
# Razorpay top-up path (which is blocked for coaching workspaces).
COACHING_STARTER_POOL_UNITS = 50


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

    def access_state(
        self, now: Optional[datetime] = None,
    ) -> "CoachingSessionAccessState":
        """Return the current access state (NOT_YET_STARTED / ACTIVE
        / GRACE / ARCHIVED). Pure function of (status, closed_at,
        now). See CoachingSessionAccessState for the lifecycle."""
        if self.status == CoachingSessionStatus.DRAFT.value:
            return CoachingSessionAccessState.NOT_YET_STARTED
        if self.status == CoachingSessionStatus.ACTIVE.value:
            return CoachingSessionAccessState.ACTIVE
        # CLOSED_MANUAL / CLOSED_AUTO
        if self.closed_at is None:
            # Defensive — shouldn't happen (DB CHECK invariant), but
            # treat a missing timestamp as ARCHIVED rather than
            # giving an unbounded free pass.
            return CoachingSessionAccessState.ARCHIVED
        now_ = now or utcnow()
        if now_ < self.closed_at + timedelta(days=GRACE_DURATION_DAYS):
            return CoachingSessionAccessState.GRACE
        return CoachingSessionAccessState.ARCHIVED

    def grace_ends_at(self) -> Optional[datetime]:
        """When grace will end (closed_at + GRACE_DURATION_DAYS).
        None for sessions that haven't closed."""
        if self.closed_at is None:
            return None
        return self.closed_at + timedelta(days=GRACE_DURATION_DAYS)


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
        # Partial-unique via a raw Index rather than UniqueConstraint
        # so the regenerate-link flow (which sets old invite → VOID +
        # creates a fresh row) can carry the same email. See migration
        # d1a8e4b0f9c3.
        Index(
            "uq_coaching_invite_session_email_live",
            "session_id", "email",
            unique=True,
            postgresql_where=text("status <> 'VOID'"),
        ),
        CheckConstraint(
            "status IN ('INVITED', 'SUBMITTED', 'APPROVED', 'REJECTED', 'VOID')",
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

    # 2026-10-16 — Workspace snapshot PDF generated at certification
    # time (hybrid-access design). Independent of future Cosh drift,
    # preserving the student's authored content as a point-in-time
    # record. S3 URL; null until the certification generates it.
    workspace_snapshot_pdf_url: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )

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


# ── Student feedback (2026-10-16, settled with user) ──────────────────────

class CoachingStudentMoment1Response(Base):
    """Moment 1 — Expectations captured as part of the student's
    onboarding form (and updateable during ACTIVE coaching).

    One row per student. `answers_json` holds the student's text
    answers keyed by question identifier. Question identifiers are
    frontend-defined; the backend treats answers as opaque.

    All questions are optional (the student can submit the onboarding
    form without filling any) — per design 2026-10-16: "people can
    come without any expectation!"
    """
    __tablename__ = "coaching_student_moment1_responses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    student_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("coaching_students.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    answers_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False,
    )


class CoachingStudentMoment2Feedback(Base):
    """Moment 2 — Feedback captured post-session via a coach-triggered
    one-click email flow.

    Lifecycle:
      - Coach clicks "Capture Feedback" → row created with
        requested_at + feedback_token (unique UUID for email auth) +
        content_json=NULL (not yet submitted).
      - System sends email with /f/{feedback_token} link.
      - Student submits → content_json populated, submitted_at stamped.
      - After cert issued, coach can view content via post-cert
        endpoint. Neytiri (SA+CM) sees aggregated feed.

    reminder_sent_at + coach_nudge_sent_at track the 48h/96h cadence:
      - 48h past requested_at + still submitted_at IS NULL →
        auto-reminder email, stamp reminder_sent_at.
      - 96h past requested_at + still NULL → tag coach dashboard,
        stamp coach_nudge_sent_at.

    Certification gate: `/coaching/sessions/{sid}/students/{stid}/certify`
    refuses with `moment2_feedback_pending` when submitted_at IS NULL.
    """
    __tablename__ = "coaching_student_moment2_feedbacks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    student_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("coaching_students.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    # One-click email auth token. Secret, random, unique. Short URL
    # exposure is intentional — leak-containment relies on expiry at
    # submit time (token is single-use post-submit) + unique random.
    feedback_token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    requested_by_coach_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    content_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    coach_nudge_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False,
    )
