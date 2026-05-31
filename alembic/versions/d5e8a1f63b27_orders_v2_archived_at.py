"""Order management V2 — soft-archive flag for timeline-expired items

Revision ID: d5e8a1f63b27
Revises: c9d3e5f72a08
Create Date: 2026-05-31

Batch 8 of the Orders V2 redesign — tandem archive between the
advisory view and the order view.

The 2026-05-31 narrative: "Items in an order get deleted after the
timeline period. It gets deleted from the advisory and the order
(the two must work in tandem)."

Implementation chose soft-archive over hard-delete so the farmer's
History view + the `order_item_events` audit trail both survive
intact. Active order/dealer/facilitator surfaces filter on
`archived_at IS NULL` so archived rows vanish from the live UX.
"""
from alembic import op
import sqlalchemy as sa


revision = "d5e8a1f63b27"
down_revision = "c9d3e5f72a08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "order_items",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Composite index supports the dominant active-items query
    # (`WHERE order_id = ? AND archived_at IS NULL`) without bloating
    # the table with two separate single-column indexes.
    op.create_index(
        "ix_order_items_order_archived",
        "order_items",
        ["order_id", "archived_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_order_items_order_archived", table_name="order_items")
    op.drop_column("order_items", "archived_at")
