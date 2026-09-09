"""share_cross_dealer_purchases

Revision ID: b7a3f2c8e094
Revises: c9f2b4e8d371
Create Date: 2026-09-09

Farmer-controlled privacy toggle for the dealer's Farmer Ledger
"Purchased From Another Dealer" rows. Default is False (OFF) —
the safer default per user 2026-09-09: dealers see cross-shop
purchases only when the farmer explicitly opts in via their PWA
profile.

When False: `GET /dealer/ledger/farmers/{farmer_id}` skips
other-shop rows entirely for that farmer (dealer just sees fewer
rows; no "farmer opted out" hint, keeping the dealer-facing
experience trust-preserving).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7a3f2c8e094'
down_revision: Union[str, Sequence[str], None] = 'c9f2b4e8d371'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'share_cross_dealer_purchases',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'share_cross_dealer_purchases')
