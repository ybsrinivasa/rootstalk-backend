"""SeedVariety.cultivation_notes — govt-mandated seed-QR write-up.

Revision ID: c9d3f47a2e18
Revises: b7e2f931d54c
Create Date: 2026-07-05

Background:
Government of India mandate: every seed pouch carries a QR that
reveals variety name, company, mfr/exp dates, lot number, AND a
short write-up on cultivation practices. Our verify page already
covers the first five; this migration adds a nullable Text column
on SeedVariety for the write-up.

Nullable so existing varieties don't break — CA fills in per-variety
when they're ready. Additive migration, code-only rollback safe.
"""

from alembic import op
import sqlalchemy as sa


revision = "c9d3f47a2e18"
down_revision = "b7e2f931d54c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "seed_varieties",
        sa.Column("cultivation_notes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("seed_varieties", "cultivation_notes")
