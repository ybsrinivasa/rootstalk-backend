"""heal_stuck_all_na_orders

Revision ID: c4f8e2a91b73
Revises: a1baba6ad6fd
Create Date: 2026-06-20

Data heal for orders that got stranded by the pre-fix
`_update_order_status` blind spot: when every item ended in a terminal
non-approved state (NOT_AVAILABLE, REROUTED, REMOVED, REJECTED,
NOT_NEEDED, SKIPPED), the recomputer never moved the order off
PROCESSING / SENT_FOR_APPROVAL / PARTIALLY_APPROVED. The dealer +
farmer saw a "Waiting for farmer approval / Reset items / View Packing
List" surface with nothing actually approvable.

Triggering example: RT-26-000112 had one Beauveria item, dealer marked
it NOT_AVAILABLE; order stayed in SENT_FOR_APPROVAL forever.

The reverse migration is a no-op — these orders have nothing to put
them back into; rolling back the code fix is sufficient to recreate the
bug if needed.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c4f8e2a91b73'
down_revision: Union[str, Sequence[str], None] = 'a1baba6ad6fd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE orders
        SET status = 'COMPLETED'
        WHERE status IN ('PROCESSING', 'SENT_FOR_APPROVAL', 'PARTIALLY_APPROVED')
          AND EXISTS (
              SELECT 1 FROM order_items
              WHERE order_items.order_id = orders.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM order_items
              WHERE order_items.order_id = orders.id
                AND order_items.status IN (
                    'PENDING', 'AVAILABLE', 'POSTPONED',
                    'SENT_FOR_APPROVAL', 'APPROVED'
                )
          );
    """)


def downgrade() -> None:
    pass
