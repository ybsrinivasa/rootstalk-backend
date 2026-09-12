"""Coaching invite VOID status + partial-unique (session_id, email)

Adds VOID to the coaching_student_invites CHECK constraint so the
regenerate-link flow can retire an existing invite without a hard
delete (preserves audit history). Also swaps the (session_id, email)
UNIQUE constraint for a partial UNIQUE INDEX that excludes VOID
rows — so the fresh invite created by regenerate can carry the same
email as the voided one it replaces.

Revision ID: d1a8e4b0f9c3
Revises: e5f3a1b7c802
Create Date: 2026-09-12
"""
from alembic import op


revision = "d1a8e4b0f9c3"
down_revision = "e5f3a1b7c802"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Widen the CHECK constraint to allow the new VOID value.
    op.drop_constraint(
        "chk_coaching_invite_status", "coaching_student_invites", type_="check",
    )
    op.create_check_constraint(
        "chk_coaching_invite_status",
        "coaching_student_invites",
        "status IN ('INVITED', 'SUBMITTED', 'APPROVED', 'REJECTED', 'VOID')",
    )

    # 2. Swap the plain UNIQUE (session_id, email) for a partial
    # UNIQUE INDEX that skips VOID rows, so regenerate can create a
    # fresh invite for the same email in the same session.
    op.drop_constraint(
        "uq_coaching_invite_session_email",
        "coaching_student_invites",
        type_="unique",
    )
    op.create_index(
        "uq_coaching_invite_session_email_live",
        "coaching_student_invites",
        ["session_id", "email"],
        unique=True,
        postgresql_where="status <> 'VOID'",
    )


def downgrade() -> None:
    op.drop_index(
        "uq_coaching_invite_session_email_live",
        table_name="coaching_student_invites",
    )
    op.create_unique_constraint(
        "uq_coaching_invite_session_email",
        "coaching_student_invites",
        ["session_id", "email"],
    )
    op.drop_constraint(
        "chk_coaching_invite_status", "coaching_student_invites", type_="check",
    )
    op.create_check_constraint(
        "chk_coaching_invite_status",
        "coaching_student_invites",
        "status IN ('INVITED', 'SUBMITTED', 'APPROVED', 'REJECTED')",
    )
