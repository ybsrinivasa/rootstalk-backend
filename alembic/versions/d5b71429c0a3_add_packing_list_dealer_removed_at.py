"""add_packing_list_dealer_removed_at

Revision ID: d5b71429c0a3
Revises: c3a91b5fd812
Create Date: 2026-06-05

Adds packing_lists.dealer_removed_at so the dealer can voluntarily
remove an order's packing card from their Packing pill once they're
done with it (e.g. handed over to the farmer in person). The order
then qualifies for the Completed pill provided no other items are
still in flight.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5b71429c0a3'
down_revision: Union[str, Sequence[str], None] = 'c3a91b5fd812'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'packing_lists',
        sa.Column('dealer_removed_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('packing_lists', 'dealer_removed_at')
