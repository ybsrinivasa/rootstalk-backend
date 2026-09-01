"""Coaching Sandbox — sessions, invites, students, workspace client type.

Revision ID: e7b1c4a09d52
Revises: c8d2f3a71e94
Create Date: 2026-09-01

Powers the Coaching Sandbox v1 feature (plan aligned with user 2026-08-31).

Concepts introduced:
  - **CoachingSession** — bounded time envelope (30 days), created by a
    user with COACH role in the SA portal against a real ("reference")
    client. Constraint: at most one non-CLOSED session per reference
    client (partial unique index).
  - **CoachingStudentInvite** — email invite → student self-registration
    form (name, YOB, address, org, phone) → coach approves or rejects.
  - **CoachingStudent** — approved student, tied to (a) their own
    isolated coaching workspace, (b) their approved phone (only phone
    the student can use), (c) assigned PWA roles for cross-role
    interaction.
  - **Client.is_coaching** — new client flavour alongside the existing
    is_training. Coaching workspaces are Client rows with
    is_coaching=true + parent_client_id=<reference> + parent_session_id.

Design decisions locked with user 2026-08-31:
  - Empty workspaces for the first cohort (no pre-seed of reference
    client's setup) — students learn by building.
  - DEALER/FACILITATOR PWA role exclusion RELAXED inside coaching
    workspaces (enforced at the app layer, not this migration).
  - Approved phone is exclusively bound to the student while the
    session is not-CLOSED — reject student self-registration if the
    phone already belongs to any real user.
  - v_real_clients VIEW extended to hide coaching workspaces too, so
    every read path that used it inherits the filter for free.

Also worth noting: the pre-existing chk_training_client_shape CHECK
assumed non-training clients have `parent_client_id IS NULL`. Coaching
workspaces break that assumption (they're non-training but DO set
parent_client_id). The check is dropped and re-created to cover all
three shapes: real, training-child, coaching-workspace.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7b1c4a09d52"
down_revision: Union[str, Sequence[str], None] = "c8d2f3a71e94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Extend RoleType enum with COACH ──────────────────────────────────
    # SA is implicit coach and doesn't need this role granted; every
    # other user must have this role assigned via SA portal before they
    # can create a CoachingSession. Non-exclusive with any other role.
    # IF NOT EXISTS guards a re-run (defensive; alembic itself won't
    # re-run a migration but a manual psql invocation might).
    op.execute("ALTER TYPE roletype ADD VALUE IF NOT EXISTS 'COACH'")

    # ── coaching_sessions ────────────────────────────────────────────────
    op.create_table(
        "coaching_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "coach_user_id", sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reference_client_id", sa.String(length=36),
            sa.ForeignKey("clients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # DRAFT: coach still adding students. ACTIVE: started, students
        # can log in, 30-day clock ticks. CLOSED_MANUAL: coach ended
        # early. CLOSED_AUTO: 30-day clock elapsed.
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        # started_at set on transition DRAFT → ACTIVE (button click).
        # The 30-day auto-close clock counts from here, not created_at.
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "closed_by_user_id", sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Status-shape invariant: DRAFT has no start/close timestamps,
        # ACTIVE has started_at but no closed_at, CLOSED variants have
        # both. Prevents a stray DRAFT with a started_at, etc.
        sa.CheckConstraint(
            "(status = 'DRAFT' AND started_at IS NULL AND closed_at IS NULL) "
            "OR (status = 'ACTIVE' AND started_at IS NOT NULL AND closed_at IS NULL) "
            "OR (status IN ('CLOSED_MANUAL', 'CLOSED_AUTO') AND started_at IS NOT NULL AND closed_at IS NOT NULL)",
            name="chk_coaching_session_status_shape",
        ),
    )

    # One non-CLOSED session per reference client — no two coaches can
    # simultaneously run coaching for the same real client, and even
    # the same coach can't have two DRAFT/ACTIVE sessions on it. A
    # freshly closed session immediately allows a new one.
    op.create_index(
        "uq_one_open_coaching_session_per_client",
        "coaching_sessions", ["reference_client_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('DRAFT', 'ACTIVE')"),
    )

    # Lookup index for the hourly auto-close sweep + coach's dashboard.
    op.create_index(
        "ix_coaching_sessions_status",
        "coaching_sessions", ["status"],
    )
    op.create_index(
        "ix_coaching_sessions_coach_user_id",
        "coaching_sessions", ["coach_user_id"],
    )

    # ── coaching_student_invites ─────────────────────────────────────────
    # Coach creates an invite (INVITED) → student clicks link, fills form,
    # submits (SUBMITTED) → coach approves (APPROVED, provisions the
    # CoachingStudent + workspace) or rejects (REJECTED). Expires after
    # the invite window if untouched.
    op.create_table(
        "coaching_student_invites",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id", sa.String(length=36),
            sa.ForeignKey("coaching_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("invite_token", sa.String(length=64), nullable=False, unique=True),
        # submitted_form: JSON with name, year_of_birth, address, organization, phone.
        # Kept as JSON not columns so the form schema can evolve without
        # a migration; the coach's approval endpoint pulls it out at
        # provisioning time.
        sa.Column("submitted_form", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "approved_by_user_id", sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('INVITED', 'SUBMITTED', 'APPROVED', 'REJECTED')",
            name="chk_coaching_invite_status",
        ),
        # Prevent the coach from double-inviting the same email in one session.
        sa.UniqueConstraint("session_id", "email", name="uq_coaching_invite_session_email"),
    )
    op.create_index(
        "ix_coaching_invites_status",
        "coaching_student_invites", ["session_id", "status"],
    )

    # ── coaching_students ────────────────────────────────────────────────
    op.create_table(
        "coaching_students",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id", sa.String(length=36),
            sa.ForeignKey("coaching_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # The student's own isolated workspace. Created at approval time.
        # RESTRICT so a workspace can't be orphan-deleted while the
        # student row references it.
        sa.Column(
            "workspace_client_id", sa.String(length=36),
            sa.ForeignKey("clients.id", ondelete="RESTRICT"),
            nullable=False, unique=True,
        ),
        # The one and only phone the student can log into the PWA with,
        # captured at self-registration time. Enforced at OTP-request
        # time in the auth service.
        sa.Column("approved_phone", sa.String(length=15), nullable=False),
        # JSON array of RoleType strings — which PWA roles the coach
        # granted the student (FARMER, DEALER, FACILITATOR, FARM_PUNDIT).
        # A student can hold multiple PWA roles simultaneously in the
        # coaching context (DEALER/FACILITATOR exclusion relaxed).
        sa.Column("assigned_pwa_roles", sa.JSON(), nullable=True),
        sa.Column("certified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "certified_by_user_id", sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        # One student row per (session, user) — belt-and-braces against
        # a duplicate approval creating two students for the same person.
        sa.UniqueConstraint("session_id", "user_id", name="uq_coaching_student_session_user"),
    )
    op.create_index(
        "ix_coaching_students_session_id",
        "coaching_students", ["session_id"],
    )

    # ── Client flavour: is_coaching + parent_session_id ──────────────────
    op.add_column(
        "clients",
        sa.Column(
            "is_coaching", sa.Boolean(),
            server_default=sa.text("false"), nullable=False,
        ),
    )
    op.add_column(
        "clients",
        sa.Column("parent_session_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_clients_parent_session_id",
        "clients", "coaching_sessions",
        ["parent_session_id"], ["id"],
        ondelete="RESTRICT",
    )

    # ── Reshape the client-shape CHECK to accommodate coaching ───────────
    # The old check assumed non-training clients have parent_client_id
    # NULL. Coaching workspaces break that assumption (they're
    # non-training but DO set parent_client_id → reference client).
    op.drop_constraint(
        "chk_training_client_shape", "clients", type_="check",
    )
    op.create_check_constraint(
        "chk_client_shape",
        "clients",
        # Real client — no training or coaching fields set.
        "(is_training = false AND is_coaching = false "
        "  AND parent_client_id IS NULL "
        "  AND training_started_at IS NULL "
        "  AND training_ends_at IS NULL "
        "  AND training_status IS NULL "
        "  AND parent_session_id IS NULL) "
        "OR "
        # Training child — training fields all set, coaching all null.
        "(is_training = true AND is_coaching = false "
        "  AND parent_client_id IS NOT NULL "
        "  AND training_started_at IS NOT NULL "
        "  AND training_ends_at IS NOT NULL "
        "  AND training_status IS NOT NULL "
        "  AND parent_session_id IS NULL) "
        "OR "
        # Coaching workspace — coaching fields set, training all null.
        # parent_client_id → the reference client the workspace belongs to.
        "(is_training = false AND is_coaching = true "
        "  AND parent_client_id IS NOT NULL "
        "  AND parent_session_id IS NOT NULL "
        "  AND training_started_at IS NULL "
        "  AND training_ends_at IS NULL "
        "  AND training_status IS NULL)",
    )

    # ── Extend v_real_clients to hide coaching workspaces too ────────────
    # Every read path currently querying this view for "real clients only"
    # inherits the coaching filter without a code change.
    op.execute("DROP VIEW IF EXISTS v_real_clients")
    op.execute(
        "CREATE VIEW v_real_clients AS "
        "SELECT * FROM clients "
        "WHERE is_training = false AND is_coaching = false"
    )

    op.create_index(
        "ix_clients_is_coaching",
        "clients", ["is_coaching"],
        postgresql_where=sa.text("is_coaching = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_clients_is_coaching", table_name="clients")

    op.execute("DROP VIEW IF EXISTS v_real_clients")
    op.execute(
        "CREATE VIEW v_real_clients AS "
        "SELECT * FROM clients WHERE is_training = false"
    )

    op.drop_constraint("chk_client_shape", "clients", type_="check")
    op.create_check_constraint(
        "chk_training_client_shape",
        "clients",
        "(is_training = false "
        "  AND parent_client_id IS NULL "
        "  AND training_started_at IS NULL "
        "  AND training_ends_at IS NULL "
        "  AND training_status IS NULL) "
        "OR "
        "(is_training = true "
        "  AND parent_client_id IS NOT NULL "
        "  AND training_started_at IS NOT NULL "
        "  AND training_ends_at IS NOT NULL "
        "  AND training_status IS NOT NULL)",
    )

    op.drop_constraint(
        "fk_clients_parent_session_id", "clients", type_="foreignkey",
    )
    op.drop_column("clients", "parent_session_id")
    op.drop_column("clients", "is_coaching")

    op.drop_index("ix_coaching_students_session_id", table_name="coaching_students")
    op.drop_table("coaching_students")

    op.drop_index("ix_coaching_invites_status", table_name="coaching_student_invites")
    op.drop_table("coaching_student_invites")

    op.drop_index("ix_coaching_sessions_coach_user_id", table_name="coaching_sessions")
    op.drop_index("ix_coaching_sessions_status", table_name="coaching_sessions")
    op.drop_index("uq_one_open_coaching_session_per_client", table_name="coaching_sessions")
    op.drop_table("coaching_sessions")

    # Postgres does NOT support removing an enum value cleanly. If a real
    # rollback is needed, the operator will need to manually rebuild the
    # enum type (dump users, drop table, recreate enum without COACH,
    # restore). Leaving the value in place is the safe/pragmatic
    # downgrade; UserRole rows with COACH would need to be deleted first.


# NOTE for prod rollout: this migration mixes DDL that Postgres normally
# runs in a single transaction with `ALTER TYPE ADD VALUE`. Postgres
# 12+ allows the latter inside a transaction; we're on 15+ everywhere.
# If a rare older-Postgres path arises, split the ALTER TYPE into its
# own migration with `# transactional_ddl = False` at the top.
