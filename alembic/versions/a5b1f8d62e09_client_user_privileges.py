"""Batch X (2026-05-19) — per-client SE single-holder privilege.

Adds `client_user_privileges` table + backfill existing
SEED_DATA_MANAGER ClientUsers into the new shape:
  • Flip their role to SUBJECT_EXPERT.
  • Insert a ClientUserPrivilege(SEED_DATA) row pointing at the
    same (client_id, user_id).

Per-(client, privilege) single-holder enforced via partial unique
index. Per-(client, user, privilege) uniqueness mirrors the SA-side
`cm_privileges` pattern.

Revision ID: a5b1f8d62e09
Revises: d3a7c81e4f02
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa


revision = "a5b1f8d62e09"
down_revision = "d3a7c81e4f02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_user_privileges",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "client_id", sa.String(length=36),
            sa.ForeignKey("clients.id"), nullable=False,
        ),
        sa.Column(
            "user_id", sa.String(length=36),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column("privilege", sa.String(length=30), nullable=False),
        sa.Column(
            "granted_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.UniqueConstraint(
            "client_id", "user_id", "privilege",
            name="uq_client_user_privilege_triple",
        ),
    )
    op.create_index(
        "uq_client_user_privilege_single_holder",
        "client_user_privileges",
        ["client_id", "privilege"],
        unique=True,
    )

    # Backfill: existing SEED_DATA_MANAGER ClientUsers → SUBJECT_EXPERT
    # + ClientUserPrivilege(SEED_DATA). Conflict-safe per the unique
    # index so re-running is a no-op.
    op.execute("""
        INSERT INTO client_user_privileges (id, client_id, user_id, privilege, granted_at)
        SELECT
            gen_random_uuid()::text,
            client_id,
            user_id,
            'SEED_DATA',
            NOW()
        FROM client_users
        WHERE role = 'SEED_DATA_MANAGER'
        ON CONFLICT (client_id, privilege) DO NOTHING;
    """)
    op.execute("""
        UPDATE client_users
        SET role = 'SUBJECT_EXPERT'
        WHERE role = 'SEED_DATA_MANAGER';
    """)


def downgrade() -> None:
    # Revert role flips first so we don't orphan the role.
    op.execute("""
        UPDATE client_users cu
        SET role = 'SEED_DATA_MANAGER'
        FROM client_user_privileges p
        WHERE p.client_id = cu.client_id
          AND p.user_id = cu.user_id
          AND p.privilege = 'SEED_DATA';
    """)
    op.drop_index(
        "uq_client_user_privilege_single_holder",
        table_name="client_user_privileges",
    )
    op.drop_table("client_user_privileges")
