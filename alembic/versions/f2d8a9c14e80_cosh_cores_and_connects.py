"""Add cosh_core_items + cosh_connect_rows (CCA Step 4 / schema migration)

Revision ID: f2d8a9c14e80
Revises: e7a2b8d4f193
Create Date: 2026-05-07 18:00:00.000000

Replaces the legacy single-table `cosh_reference_cache` with a typed
two-table model:

  cosh_core_items   — flat Cosh entities (Cores). Every entity_type
                      that's structurally a row in a list — crops,
                      common names, application methods, dosage units,
                      formulations, problem groups, specific problems,
                      brands, etc. — lives here. `core_type` names
                      the Core; `parent_cosh_id` links hierarchies
                      (SP→PG, brand→CNI, district→state).
  cosh_connect_rows — N-ary Connects between Cores. `connect_type`
                      names the Connect; `endpoints` is a JSONB array
                      of {role, cosh_id} pairs. Designed for Cosh's
                      Simple/Compound/Complex Connect flavours without
                      schema change — new connect_type values onboard
                      via data only.

Migration is creation-only at this stage; `cosh_reference_cache` is
left alive in parallel until backfill + readsite refactors are done.
The legacy table will be dropped in a follow-up migration once every
readsite is verified migrated.
"""
from alembic import op
import sqlalchemy as sa


revision = "f2d8a9c14e80"
down_revision = "e7a2b8d4f193"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cosh_core_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("cosh_id", sa.String(length=200), nullable=False),
        sa.Column("core_type", sa.String(length=100), nullable=False),
        sa.Column("parent_cosh_id", sa.String(length=200), nullable=True),
        sa.Column(
            "status", sa.String(length=20),
            nullable=False, server_default="active",
        ),
        sa.Column("translations", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "cosh_id", "core_type", name="uq_cosh_core_id_type",
        ),
    )
    op.create_index(
        "idx_cci_type_status", "cosh_core_items",
        ["core_type", "status"],
    )
    op.create_index(
        "idx_cci_parent_type", "cosh_core_items",
        ["parent_cosh_id", "core_type"],
    )

    op.create_table(
        "cosh_connect_rows",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("connect_id", sa.String(length=200), nullable=False),
        sa.Column("connect_type", sa.String(length=100), nullable=False),
        sa.Column("endpoints", sa.JSON(), nullable=False),
        sa.Column(
            "status", sa.String(length=20),
            nullable=False, server_default="active",
        ),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "connect_id", "connect_type", name="uq_cosh_connect_id_type",
        ),
    )
    op.create_index(
        "idx_ccr_type_status", "cosh_connect_rows",
        ["connect_type", "status"],
    )


def downgrade():
    op.drop_index("idx_ccr_type_status", table_name="cosh_connect_rows")
    op.drop_table("cosh_connect_rows")
    op.drop_index("idx_cci_parent_type", table_name="cosh_core_items")
    op.drop_index("idx_cci_type_status", table_name="cosh_core_items")
    op.drop_table("cosh_core_items")
