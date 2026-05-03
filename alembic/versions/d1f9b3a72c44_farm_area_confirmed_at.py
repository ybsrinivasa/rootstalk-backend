"""Add farm_area_confirmed_at to subscriptions

Revision ID: d1f9b3a72c44
Revises: c7b3a91e4d22
Create Date: 2026-05-02 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "d1f9b3a72c44"
down_revision = "c7b3a91e4d22"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "subscriptions",
        sa.Column("farm_area_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("subscriptions", "farm_area_confirmed_at")
