"""add return_reason to orders + seed_orders_full

Revision ID: c4d92e51a3f0
Revises: b3e1de55f4a2
Create Date: 2026-08-12

Distinguishes returned-to-farmer DRAFTs by who caused the return:
  farmer_cancel        — farmer tapped Cancel Order
  dealer_declined      — dealer marked NOT_AVAILABLE / declined the order
  facilitator_declined — facilitator rejected the order

Feeds the "CANCELLED BY YOU / DECLINED BY DEALER / DECLINED BY
FACILITATOR" chip on the Returned pill card. NULL on every non-
returned row (initial-composer, in-flight, terminal without return).

Cleared to NULL when the DRAFT flips to SENT (paired with clearing
is_returned_to_farmer + released_*).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4d92e51a3f0'
down_revision: Union[str, Sequence[str], None] = 'b3e1de55f4a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ('orders', 'seed_orders_full'):
        op.add_column(
            table,
            sa.Column('return_reason', sa.String(30), nullable=True),
        )


def downgrade() -> None:
    for table in ('orders', 'seed_orders_full'):
        op.drop_column(table, 'return_reason')
