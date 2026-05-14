"""Add status column to timelines

Revision ID: d4f8a2c1e7b3
Revises: c3e1a8f4b277
Create Date: 2026-05-14 00:00:00.000000

Adds `timelines.status` (ACTIVE / INACTIVE), per user 2026-05-14 —
SE wants to mark a Timeline inactive to retire it from farmer
advisory without losing its history. Default ACTIVE so existing
rows keep their behaviour.
"""
from alembic import op
import sqlalchemy as sa


revision = "d4f8a2c1e7b3"
down_revision = "c3e1a8f4b277"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "timelines",
        sa.Column(
            "status", sa.String(length=20), nullable=False,
            server_default="ACTIVE",
        ),
    )


def downgrade():
    op.drop_column("timelines", "status")
