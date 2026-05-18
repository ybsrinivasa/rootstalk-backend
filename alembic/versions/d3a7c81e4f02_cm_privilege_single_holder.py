"""Batch U (2026-05-18) — CM privilege single-holder invariant.

Adds a partial unique index on cm_privileges.privilege so at most
one Content Manager holds each privilege at any time. Belt-and-
braces with the new PUT /admin/cm-privileges/{privilege} endpoint
which demotes other holders atomically.

Existing data: dedupe before creating the index. For each
privilege with multiple holders, keep the most recently granted
and delete the rest. The SA can re-assign after the migration
if their intent was different.

Revision ID: d3a7c81e4f02
Revises: c4f6e2a91d3b
Create Date: 2026-05-18
"""
from alembic import op


revision = "d3a7c81e4f02"
down_revision = "c4f6e2a91d3b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dedupe — for each privilege, keep the latest grant per privilege
    # and drop older rows so the unique index can land.
    op.execute("""
        DELETE FROM cm_privileges
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY privilege
                    ORDER BY granted_at DESC, id DESC
                ) AS rn
                FROM cm_privileges
            ) ranked
            WHERE ranked.rn > 1
        );
    """)
    op.create_index(
        "uq_cm_privilege_single_holder",
        "cm_privileges",
        ["privilege"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_cm_privilege_single_holder", table_name="cm_privileges")
