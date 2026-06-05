"""add_order_item_approval_round

Revision ID: c3a91b5fd812
Revises: 00e5a72be248
Create Date: 2026-06-05

Adds OrderItem.approval_round so the farmer's review page can queue
approval batches per order. The dealer's bulk submit-for-approval
gets round 1; later postpone-resolve auto-submits get round 2, 3, …
The farmer's review filters to MIN(approval_round) among
SENT_FOR_APPROVAL items so each round is decided before the next
appears.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3a91b5fd812'
down_revision: Union[str, Sequence[str], None] = '00e5a72be248'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'order_items',
        sa.Column('approval_round', sa.Integer(), nullable=True),
    )
    # Backfill existing SENT_FOR_APPROVAL + APPROVED + REJECTED items
    # to round 1 so the queue starts cleanly with the data we have.
    op.execute(
        """
        UPDATE order_items
        SET approval_round = 1
        WHERE status IN ('SENT_FOR_APPROVAL', 'APPROVED', 'REJECTED')
          AND approval_round IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column('order_items', 'approval_round')
