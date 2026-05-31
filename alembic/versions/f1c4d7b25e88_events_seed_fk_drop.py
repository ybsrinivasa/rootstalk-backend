"""Order management V2 — drop the events.seed_order_id FK

Revision ID: f1c4d7b25e88
Revises: e7f9b3a25c11
Create Date: 2026-05-31

Batch 12 fix. The Batch 1 schema put `order_item_events.seed_order_id`
as a FK to `seed_orders` (the basic SeedOrder model under
`orders/models.py`). The LIVE seed flow uses `seed_orders_full`
(SeedOrderFull under `seed_mgmt/models.py`) — they're separate
tables. Writes from the seed lifecycle endpoints tripped the FK
constraint.

The audit table's primary lookup is `lineage_id` anyway; the FK
was a nicety that doesn't survive the two-seed-tables reality.
Drop the constraint and keep the column as a free-form UUID.
"""
from alembic import op


revision = "f1c4d7b25e88"
down_revision = "e7f9b3a25c11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "order_item_events_seed_order_id_fkey",
        "order_item_events",
        type_="foreignkey",
    )


def downgrade() -> None:
    op.create_foreign_key(
        "order_item_events_seed_order_id_fkey",
        "order_item_events",
        "seed_orders",
        ["seed_order_id"], ["id"],
        ondelete="SET NULL",
    )
