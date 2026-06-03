"""add_order_lineage_root_id

Revision ID: 00e5a72be248
Revises: d4a72f8b1095
Create Date: 2026-06-03 09:30:26.706603

Adds Order.lineage_root_id so the farmer's Manage tab can group sub-
orders (reroute children) under one card per original procurement
intent. Self-referential FK on orders.id; nullable for legacy rows
(client code treats null as "this row IS the root").
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '00e5a72be248'
down_revision: Union[str, Sequence[str], None] = 'd4a72f8b1095'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'orders',
        sa.Column('lineage_root_id', sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        'fk_orders_lineage_root',
        'orders', 'orders',
        ['lineage_root_id'], ['id'],
    )
    op.create_index(
        'ix_orders_lineage_root', 'orders', ['lineage_root_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_orders_lineage_root', table_name='orders')
    op.drop_constraint('fk_orders_lineage_root', 'orders', type_='foreignkey')
    op.drop_column('orders', 'lineage_root_id')
