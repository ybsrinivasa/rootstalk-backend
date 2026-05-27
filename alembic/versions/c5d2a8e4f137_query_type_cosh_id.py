"""queries.query_type_cosh_id

Mandatory at the API layer (rejected at submit if absent) but
nullable in DB so existing pre-2026-05-27 queries — submitted
under the old free-text title shape — keep loading. Future
backfill can resolve historical titles back to query_types Cosh
ids; out of scope for this batch.

Revision ID: c5d2a8e4f137
Revises: b3e4f7a52d11
Create Date: 2026-05-27
"""
from alembic import op
import sqlalchemy as sa


revision = "c5d2a8e4f137"
down_revision = "b3e4f7a52d11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "queries",
        sa.Column("query_type_cosh_id", sa.String(100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("queries", "query_type_cosh_id")
