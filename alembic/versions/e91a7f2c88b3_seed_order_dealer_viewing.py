"""add dealer_viewing_until to seed_orders_full

Revision ID: e91a7f2c88b3
Revises: d2349be0895d
Create Date: 2026-08-11

Mirrors `orders.dealer_viewing_until` (Orders V2 Batch 2) on the
seed side so we can gate `POST /farmer/seed-orders/{oid}/cancel`
on live dealer presence — same "no cancel while the dealer is
actively working with this order" semantics.

There's no dealer-side seed-order detail page today (dealers act
on seed orders via inline buttons on the main /dealer/orders
list), so no PWA heartbeat call site exists yet. The column +
endpoint are provisioned in advance — when a dealer seed-detail
screen lands, its mount effect can start pinging /dealer/seed-
orders/{oid}/heartbeat and the farmer's cancel gate immediately
becomes live.

Column defaults to NULL, so the gate is a no-op until a heartbeat
lands. Backfill unnecessary.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e91a7f2c88b3'
down_revision: Union[str, Sequence[str], None] = 'd2349be0895d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'seed_orders_full',
        sa.Column(
            'dealer_viewing_until',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('seed_orders_full', 'dealer_viewing_until')
