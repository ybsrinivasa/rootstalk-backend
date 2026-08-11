"""add released_dealer_user_id + released_facilitator_user_id

Revision ID: b3e1de55f4a2
Revises: a7d1c8f24b09
Create Date: 2026-08-11

Cancel-migrate (Model B) cards on the farmer's Returned pill benefit
from context: farmer glances at the card and immediately sees which
dealer the order was previously with, alongside a subtle "Cancelled
by you" marker.

Two mirrored FK columns on orders + seed_orders_full:
  released_dealer_user_id      — set to the outgoing dealer at cancel
  released_facilitator_user_id — set to the outgoing facilitator

For pest/fert: populated when the cancel-migrate DRAFT is minted (copy
from the source order). For seed: populated in-place before dealer_user_id
and facilitator_user_id get cleared on the flip-to-DRAFT. Both cleared
when the DRAFT flips to SENT (informational only — no downstream
dependency).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3e1de55f4a2'
down_revision: Union[str, Sequence[str], None] = 'a7d1c8f24b09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ('orders', 'seed_orders_full'):
        op.add_column(
            table,
            sa.Column(
                'released_dealer_user_id',
                sa.String(36),
                sa.ForeignKey('users.id'),
                nullable=True,
            ),
        )
        op.add_column(
            table,
            sa.Column(
                'released_facilitator_user_id',
                sa.String(36),
                sa.ForeignKey('users.id'),
                nullable=True,
            ),
        )


def downgrade() -> None:
    for table in ('orders', 'seed_orders_full'):
        op.drop_column(table, 'released_facilitator_user_id')
        op.drop_column(table, 'released_dealer_user_id')
