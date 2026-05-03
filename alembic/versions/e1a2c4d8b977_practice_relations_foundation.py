"""Add common_name_cosh_id to practices and relation_role to order_items

Revision ID: e1a2c4d8b977
Revises: d1f9b3a72c44
Create Date: 2026-05-02 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "e1a2c4d8b977"
down_revision = "d1f9b3a72c44"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "practices",
        sa.Column("common_name_cosh_id", sa.String(100), nullable=True),
    )
    op.add_column(
        "order_items",
        sa.Column("relation_role", sa.String(50), nullable=True),
    )


def downgrade():
    op.drop_column("order_items", "relation_role")
    op.drop_column("practices", "common_name_cosh_id")
