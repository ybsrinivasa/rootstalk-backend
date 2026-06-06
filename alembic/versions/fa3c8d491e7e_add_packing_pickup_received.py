"""add_packing_pickup_received

Revision ID: fa3c8d491e7e
Revises: e8f4c205a917
Create Date: 2026-06-06

Adds pickup + farmer-received tracking on packing_lists so the
dealer can see who collected items from their shop and whether they
ultimately reached the farmer:

  picked_up_at         — when first pickup happened
  picked_up_by_user_id — the user who picked up (farmer or facilitator);
                          role derived from the order's farmer_user_id /
                          facilitator_user_id at read time
  farmer_received_at   — when items reached the farmer's hands. Auto-set
                          on farmer pickup (same value as picked_up_at);
                          set later when farmer confirms after a
                          facilitator pickup.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'fa3c8d491e7e'
down_revision: Union[str, Sequence[str], None] = 'e8f4c205a917'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'packing_lists',
        sa.Column('picked_up_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'packing_lists',
        sa.Column(
            'picked_up_by_user_id',
            sa.String(length=36),
            sa.ForeignKey('users.id'),
            nullable=True,
        ),
    )
    op.add_column(
        'packing_lists',
        sa.Column('farmer_received_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('packing_lists', 'farmer_received_at')
    op.drop_column('packing_lists', 'picked_up_by_user_id')
    op.drop_column('packing_lists', 'picked_up_at')
