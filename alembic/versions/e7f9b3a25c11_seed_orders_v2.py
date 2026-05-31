"""Order management V2 — seeds get the same vocabulary

Revision ID: e7f9b3a25c11
Revises: d5e8a1f63b27
Create Date: 2026-05-31

Batch 12 — seed orders catch up to the OrderItem lifecycle.

Per the 2026-05-31 narrative (Q10): "Seeds can also be
returned/postponed. Share the same vocabulary."

Two new columns on `seed_orders_full`:
- `postponed_until` — mirrors `order_items.postponed_until` so the
  postpone-expiry sweep can flip seeds the same way it flips
  pesticide / fertiliser items.
- `lineage_id` — same journey-identifier story as `order_items`.
  When the farmer cancels and the order migrates to a fresh DRAFT,
  the new row inherits the lineage so reports can trace the seed
  across dealer hops.

Legacy rows backfill `lineage_id = id`. New enum values
(AVAILABLE, POSTPONED, NOT_AVAILABLE, DRAFT, REROUTED) live in the
SeedOrderStatus Python enum; the DB column is already a free-form
String(30) so no migration of the column itself is needed.
"""
from alembic import op
import sqlalchemy as sa


revision = "e7f9b3a25c11"
down_revision = "d5e8a1f63b27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "seed_orders_full",
        sa.Column("postponed_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "seed_orders_full",
        sa.Column("lineage_id", sa.String(36), nullable=True),
    )
    op.execute("UPDATE seed_orders_full SET lineage_id = id WHERE lineage_id IS NULL")
    op.alter_column("seed_orders_full", "lineage_id", nullable=False)
    op.create_index(
        "ix_seed_orders_full_lineage", "seed_orders_full", ["lineage_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_seed_orders_full_lineage", table_name="seed_orders_full")
    op.drop_column("seed_orders_full", "lineage_id")
    op.drop_column("seed_orders_full", "postponed_until")
