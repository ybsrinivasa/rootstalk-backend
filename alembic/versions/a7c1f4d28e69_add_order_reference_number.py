"""add_order_reference_number

Revision ID: a7c1f4d28e69
Revises: fa3c8d491e7e
Create Date: 2026-06-07

Adds Order.reference_number — a human-readable Order ID shared across
every order in a lineage chain.

  Format: RT-YY-NNNNNN  (e.g. RT-26-001247)
    RT     : fixed prefix per user direction 2026-06-07
    YY     : 2-digit year of the ROOT order's creation
    NNNNNN : 6-digit sequential counter, global within (RT, YY)

Backfill walks the orders table grouped by
COALESCE(lineage_root_id, id) so every lineage gets one shared
reference. Sequence is assigned by ascending root creation time so
older orders get lower numbers.

NOT unique — by design. All siblings in a lineage carry the same
reference_number. The index is a regular non-unique BTree so the
PWA's reference-lookup queries stay sub-millisecond.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7c1f4d28e69'
down_revision: Union[str, Sequence[str], None] = 'fa3c8d491e7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'orders',
        sa.Column('reference_number', sa.String(length=15), nullable=True),
    )
    op.create_index(
        'ix_orders_reference_number',
        'orders',
        ['reference_number'],
        unique=False,
    )

    # Backfill: group orders by COALESCE(lineage_root_id, id) and
    # assign one RT-YY-NNNNNN per group. Year comes from the
    # earliest-in-group created_at. Counter is scoped to YY, walks
    # ascending.
    op.execute(
        """
        WITH root_groups AS (
          SELECT COALESCE(lineage_root_id, id) AS root_id,
                 MIN(created_at)               AS root_created_at
          FROM orders
          GROUP BY COALESCE(lineage_root_id, id)
        ),
        numbered AS (
          SELECT root_id,
                 EXTRACT(YEAR FROM root_created_at)::int % 100 AS yy,
                 ROW_NUMBER() OVER (
                   PARTITION BY EXTRACT(YEAR FROM root_created_at)::int
                   ORDER BY root_created_at, root_id
                 ) AS seq
          FROM root_groups
        )
        UPDATE orders o
        SET reference_number = 'RT-'
                             || lpad(n.yy::text, 2, '0')
                             || '-'
                             || lpad(n.seq::text, 6, '0')
        FROM numbered n
        WHERE COALESCE(o.lineage_root_id, o.id) = n.root_id;
        """
    )


def downgrade() -> None:
    op.drop_index('ix_orders_reference_number', table_name='orders')
    op.drop_column('orders', 'reference_number')
