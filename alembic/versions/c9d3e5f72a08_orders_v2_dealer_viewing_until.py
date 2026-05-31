"""Order management V2 — dealer presence heartbeat

Revision ID: c9d3e5f72a08
Revises: b7e2c4f81a93
Create Date: 2026-05-31

Batch 2 of the Order Management V2 redesign.

Adds `orders.dealer_viewing_until` — the lease that gates farmer
cancellation while the dealer is actively working with the order
on their screen. Single column, no backfill needed (NULL == not
viewing, the safe default for existing rows).

Heartbeat semantics:
- Dealer's app PUTs to /dealer/orders/{id}/heartbeat every ~20 s
  while the order detail screen is mounted.
- Each heartbeat stamps `dealer_viewing_until = now + 30 s`, so
  one missed heartbeat doesn't immediately end the lease but the
  lease never lingers more than ~30 s past the dealer closing
  the screen.
- Farmer cancel endpoint refuses (HTTP 409 `dealer_currently_viewing`)
  while `dealer_viewing_until > now`.
- If the dealer's network drops, the lease expires naturally; no
  explicit "close" call is required.

The narrative (2026-05-31): "The order can be cancelled if it is
lying in the dealer's order list and he is, right at that time,
not processing it physically." Even ACCEPTED orders are cancellable
— only the live-presence check blocks. The farmer should never be
left at the dealer's mercy.
"""
from alembic import op
import sqlalchemy as sa


revision = "c9d3e5f72a08"
down_revision = "b7e2c4f81a93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("dealer_viewing_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "dealer_viewing_until")
