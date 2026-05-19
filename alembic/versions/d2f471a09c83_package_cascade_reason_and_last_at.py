"""Batch FF (2026-05-19) — package cascade-reason + last-cascade-at columns.

Distinguishes a crop-cascade INACTIVE from a locations-cascade INACTIVE
(both set `cascade_inactivated_at`, but the recovery story differs).
`last_cascade_at` fires on every cascade event — including a SHRINK
that left other locations standing — so the package detail page can
render a "footprint changed; review locations below" banner.

Two columns; both nullable so the migration is safe on populated DBs.

Revision ID: d2f471a09c83
Revises: c8e94a3b21f7
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa


revision = "d2f471a09c83"
down_revision = "c8e94a3b21f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "packages",
        sa.Column("cascade_inactivated_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "packages",
        sa.Column("last_cascade_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("packages", "last_cascade_at")
    op.drop_column("packages", "cascade_inactivated_reason")
