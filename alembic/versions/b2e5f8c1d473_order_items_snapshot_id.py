"""Add snapshot_id to order_items for per-subscription versioning Phase 3.2.

Revision ID: b2e5f8c1d473
Revises: a1c4d7e8b302
Create Date: 2026-05-04 00:00:00.000000

Each order_item now carries a permanent pointer to the locked_timeline_snapshot
that was in force at the moment the order was placed. The dealer's read path
(Phase 3.3) follows this pointer so brand / dosage / application instructions
are sourced from the frozen photograph, not the master tables.

Existing rows stay NULL — pre-Phase-3 orders fall back to master rendering.
The column is nullable so the migration is non-destructive and can be rolled
back.
"""
from alembic import op
import sqlalchemy as sa


revision = "b2e5f8c1d473"
down_revision = "a1c4d7e8b302"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "order_items",
        sa.Column("snapshot_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_order_items_snapshot_id",
        "order_items",
        "locked_timeline_snapshots",
        ["snapshot_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_order_items_snapshot_id", "order_items", ["snapshot_id"]
    )


def downgrade():
    op.drop_index("ix_order_items_snapshot_id", table_name="order_items")
    op.drop_constraint(
        "fk_order_items_snapshot_id", "order_items", type_="foreignkey"
    )
    op.drop_column("order_items", "snapshot_id")
