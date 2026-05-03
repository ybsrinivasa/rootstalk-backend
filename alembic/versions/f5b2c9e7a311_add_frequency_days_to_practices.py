"""Add frequency_days to practices

Revision ID: f5b2c9e7a311
Revises: e1a2c4d8b977
Create Date: 2026-05-02 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "f5b2c9e7a311"
down_revision = "e1a2c4d8b977"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("practices", sa.Column("frequency_days", sa.Integer(), nullable=True))
    op.add_column("pg_practices", sa.Column("frequency_days", sa.Integer(), nullable=True))
    op.add_column("sp_practices", sa.Column("frequency_days", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("sp_practices", "frequency_days")
    op.drop_column("pg_practices", "frequency_days")
    op.drop_column("practices", "frequency_days")
