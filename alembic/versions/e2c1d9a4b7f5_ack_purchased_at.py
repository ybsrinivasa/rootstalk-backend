"""ack_purchased_at

Revision ID: e2c1d9a4b7f5
Revises: b7f4e8c12a03
Create Date: 2026-09-17

Advisory-Only v1.8 — two-stage acknowledgement for INPUT practices.

In advisory-only mode the farmer buys inputs independently, so the ack
now splits into two events:
  1. `purchased_at` — I've bought this input (any date, incl. future
      recommendations pre-bought)
  2. `marked_at` — I've applied/done this (existing column; now gated
      on purchased_at being set AND occurrence_date <= today)

Non-INPUT practices ignore purchased_at and continue using marked_at
only. Traditional (non-advisory-only) flow ignores purchased_at
entirely — it stays NULL there.
"""
from alembic import op
import sqlalchemy as sa


revision = "e2c1d9a4b7f5"
down_revision = "b7f4e8c12a03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "practice_acknowledgements",
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("practice_acknowledgements", "purchased_at")
