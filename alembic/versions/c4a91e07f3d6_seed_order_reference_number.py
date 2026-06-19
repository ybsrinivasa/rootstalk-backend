"""Seed orders: add reference_number column + backfill existing rows

Parity with `orders.reference_number` (RT-YY-NNNNNN human-readable
Order ID). Pre-fix the dealer's order cards rendered seed-order
UUIDs as the "Order ID" because no reference_number was ever
generated for seed orders — flagged by the user 2026-06-19.

Backfill strategy:
  - Lineage-shared: rows on the same `lineage_id` get the same
    reference_number (mirrors the regular Order behaviour where
    cancel-and-migrate keeps one Order ID across the chain).
  - Format: RT-YY-NNNNNN where YY is the year of `created_at` and
    NNNNNN is a 6-digit sequence per (year). The migration walks
    the existing rows in created_at order so the sequence is
    chronological.

Revision ID: c4a91e07f3d6
Revises: b75f3e9c1a82
Create Date: 2026-06-19
"""
from alembic import op
import sqlalchemy as sa


revision = "c4a91e07f3d6"
down_revision = "b75f3e9c1a82"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "seed_orders_full",
        sa.Column("reference_number", sa.String(20), nullable=True),
    )

    # Lineage-aware backfill. Distinct lineage_id sets get distinct
    # reference_numbers; rows that share lineage_id (cancel-and-
    # migrate husks) inherit the same one. Sequence is chronological
    # within each year — derived from `MIN(created_at)` per lineage.
    op.execute(
        """
        WITH lineage_rank AS (
            SELECT
                lineage_id,
                EXTRACT(year FROM MIN(created_at))::int AS yr,
                ROW_NUMBER() OVER (
                    PARTITION BY EXTRACT(year FROM MIN(created_at))::int
                    ORDER BY MIN(created_at)
                ) AS seq
            FROM seed_orders_full
            WHERE reference_number IS NULL
            GROUP BY lineage_id
        )
        UPDATE seed_orders_full sof
        SET reference_number =
            'RT-' || lpad(SUBSTRING(lineage_rank.yr::text FROM 3 FOR 2), 2, '0')
            || '-' || lpad(lineage_rank.seq::text, 6, '0')
        FROM lineage_rank
        WHERE sof.lineage_id = lineage_rank.lineage_id
          AND sof.reference_number IS NULL
        """
    )

    op.create_index(
        "ix_seed_orders_full_reference_number",
        "seed_orders_full",
        ["reference_number"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_seed_orders_full_reference_number",
        table_name="seed_orders_full",
    )
    op.drop_column("seed_orders_full", "reference_number")
