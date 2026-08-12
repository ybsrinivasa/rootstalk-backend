"""add is_returned_to_facilitator to orders + seed_orders_full

Revision ID: e8f75d1c92a3
Revises: c4d92e51a3f0
Create Date: 2026-08-12

Facilitator-side parallel of is_returned_to_farmer. TRUE when a dealer
declines a facilitator-forwarded order, so the order returns to the
facilitator (not the farmer) for re-routing to a different dealer.
Farmer chip stays "Routed" (facilitator still handling); facilitator's
Returned pill picks it up with the standard "Send to another dealer"
affordance.

Cleared to FALSE when the facilitator forwards to a new dealer via
/facilitator/orders/{id}/route-to-dealer (or the seed equivalent).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e8f75d1c92a3'
down_revision: Union[str, Sequence[str], None] = 'c4d92e51a3f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ('orders', 'seed_orders_full'):
        op.add_column(
            table,
            sa.Column(
                'is_returned_to_facilitator',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    for table in ('orders', 'seed_orders_full'):
        op.drop_column(table, 'is_returned_to_facilitator')
