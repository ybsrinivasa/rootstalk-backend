"""add is_returned_to_farmer to orders + seed_orders_full

Revision ID: a7d1c8f24b09
Revises: e91a7f2c88b3
Create Date: 2026-08-11

Reintroduces the cancel-migrate flow (Model B) that was superseded
on 2026-06-20 by release-not-migrate. The 2026-08-11 revisit: when
the farmer taps Cancel Order, in-flight items should come BACK to
the farmer as a DRAFT continuation the farmer can either forward
to a different dealer or discard. Previously we routed those items
straight to REROUTED and left the farmer to re-order from scratch
via Advisory — which throws away farmer intent when the actual
problem is with the dealer, not the items.

`is_returned_to_farmer` marks a DRAFT as "came from a farmer
cancel, not the initial composer / bulk / reroute-returned paths."
The Returned pill query keys off this flag so cancel-migrated
DRAFTs land on the same pill as dealer-declined items. The new
/discard endpoint gates on this flag so it can't accidentally kill
an initial-composer DRAFT.

Default FALSE; existing rows are all initial-composer / reroute
DRAFTs, so leaving the flag off is correct.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7d1c8f24b09'
down_revision: Union[str, Sequence[str], None] = 'e91a7f2c88b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ('orders', 'seed_orders_full'):
        op.add_column(
            table,
            sa.Column(
                'is_returned_to_farmer',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    for table in ('orders', 'seed_orders_full'):
        op.drop_column(table, 'is_returned_to_farmer')
