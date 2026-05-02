"""order_item_or_tracking_relation_id

Revision ID: c8a3f2e91b77
Revises: f3e9a2b17c44
Create Date: 2026-05-02 11:30:00.000000

Adds relation_id to order_items so OR-group auto-close can work,
and adds an expires_at column for BL-10 order expiry.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'c8a3f2e91b77'
down_revision: Union[str, Sequence[str], None] = 'f3e9a2b17c44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('order_items', sa.Column('relation_id', sa.String(36), nullable=True))
    op.add_column('order_items', sa.Column('relation_type', sa.String(20), nullable=True))
    op.add_column('orders', sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('orders', 'expires_at')
    op.drop_column('order_items', 'relation_type')
    op.drop_column('order_items', 'relation_id')
