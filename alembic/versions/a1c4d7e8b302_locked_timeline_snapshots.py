"""Locked timeline snapshots for per-subscription content versioning

Revision ID: a1c4d7e8b302
Revises: f5b2c9e7a311
Create Date: 2026-05-02 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a1c4d7e8b302"
down_revision = "f5b2c9e7a311"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "locked_timeline_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.String(36),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # timeline_id is intentionally NOT a FK — it can reference timelines (CCA),
        # pg_timelines, or sp_timelines. The `source` column disambiguates.
        sa.Column("timeline_id", sa.String(36), nullable=False),
        sa.Column("source", sa.String(10), nullable=False, server_default="CCA"),  # "CCA" | "PG" | "SP"
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lock_trigger", sa.String(20), nullable=False),  # "PURCHASE_ORDER" | "VIEWED" | "BACKFILL"
        sa.UniqueConstraint(
            "subscription_id", "timeline_id", "source", name="uq_lts_sub_tl_source"
        ),
    )
    op.create_index(
        "ix_lts_subscription_id", "locked_timeline_snapshots", ["subscription_id"]
    )
    op.create_index(
        "ix_lts_timeline_id", "locked_timeline_snapshots", ["timeline_id"]
    )


def downgrade():
    op.drop_index("ix_lts_timeline_id", table_name="locked_timeline_snapshots")
    op.drop_index("ix_lts_subscription_id", table_name="locked_timeline_snapshots")
    op.drop_table("locked_timeline_snapshots")
