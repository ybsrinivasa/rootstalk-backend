"""Drop legacy cosh_reference_cache (CCA Step 4 / schema migration close-out)

Revision ID: a3f51e2dc874
Revises: f2d8a9c14e80
Create Date: 2026-05-07 19:00:00.000000

Final step of the schema migration. The legacy single-table cache is
fully replaced by cosh_core_items + cosh_connect_rows; every readsite
in the application has been migrated (commits b881ec4, 755968f,
808505f, 7e373f8, b228767). Removing the table now retires the dual-
write path in the sync handler and the parallel storage cost.

Downgrade re-creates the empty table shape; data is not restored
because the new tables are now the truth-source.
"""
from alembic import op
import sqlalchemy as sa


revision = "a3f51e2dc874"
down_revision = "f2d8a9c14e80"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("idx_crc_status", table_name="cosh_reference_cache")
    op.drop_index("idx_crc_parent", table_name="cosh_reference_cache")
    op.drop_index("idx_crc_entity", table_name="cosh_reference_cache")
    op.drop_table("cosh_reference_cache")


def downgrade():
    op.create_table(
        "cosh_reference_cache",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("cosh_id", sa.String(length=200), nullable=False),
        sa.Column("entity_type", sa.String(length=100), nullable=False),
        sa.Column("parent_cosh_id", sa.String(length=200), nullable=True),
        sa.Column("secondary_parent_cosh_id", sa.String(length=200), nullable=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False,
            server_default="active",
        ),
        sa.Column("translations", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "cosh_id", "entity_type", name="uq_cosh_ref_id_type",
        ),
    )
    op.create_index(
        "idx_crc_entity", "cosh_reference_cache", ["entity_type"],
    )
    op.create_index(
        "idx_crc_parent", "cosh_reference_cache", ["parent_cosh_id"],
    )
    op.create_index(
        "idx_crc_status", "cosh_reference_cache",
        ["entity_type", "status"],
    )
