"""add final_confirmed_at columns

Revision ID: d5a9f81b3c72
Revises: e8f75d1c92a3
Create Date: 2026-08-14

Phase 2 of the order-lifecycle rework (see
`project_rootstalk_order_lifecycle_rework_2026_08_13.md`). Introduces
an explicit dealer commitment step between the farmer's APPROVED
decision and the item reaching the Pickup pill.

  order_items.final_confirmed_at        — timestamp the dealer stamped
                                          "final commitment" on the item
                                          (payment / credit settled).
                                          NULL = still awaiting the
                                          dealer's Final Confirmation.
  seed_orders_full.final_confirmed_at   — same semantics, seed lifecycle.

Nullable = "not yet Final Confirmed" is the correct default; no back-
fill needed. The Pickup-pill gate reads `status = APPROVED AND
final_confirmed_at IS NOT NULL`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5a9f81b3c72'
down_revision: Union[str, Sequence[str], None] = 'e8f75d1c92a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'order_items',
        sa.Column('final_confirmed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'seed_orders_full',
        sa.Column('final_confirmed_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('seed_orders_full', 'final_confirmed_at')
    op.drop_column('order_items', 'final_confirmed_at')
