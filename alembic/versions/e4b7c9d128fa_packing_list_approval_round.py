"""packing_list_approval_round

Revision ID: e4b7c9d128fa
Revises: d5a9f81b3c72
Create Date: 2026-08-17

Per-batch Final Confirmation + Pickup rework. A single PackingList
per order can't distinguish "batch 1 already received" from "batch 2
awaiting final confirmation" — the farmer's Pickup pill + dealer's
Packing/Final-Confirmation pills all conflate them, causing the
mixed-batch bugs seen 2026-08-17 (RT-26-000445 / 447).

Adds `approval_round` (nullable int) to `packing_lists`. One row per
(order_id, approval_round). Existing rows get backfilled to round 1
(their items are all in round 1 by definition — nothing else could
have run yet). Seed orders keep approval_round NULL (they don't have
rounds — one seed order = one item = one packing list).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e4b7c9d128fa'
down_revision: Union[str, Sequence[str], None] = 'd5a9f81b3c72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'packing_lists',
        sa.Column('approval_round', sa.Integer(), nullable=True),
    )
    # Backfill existing pest/fert PLs to round 1. Seed PLs (order_id
    # IS NULL, seed_order_id IS NOT NULL) stay NULL — no round concept.
    op.execute(
        "UPDATE packing_lists SET approval_round = 1 "
        "WHERE order_id IS NOT NULL AND approval_round IS NULL"
    )
    # Index for the per-batch lookup path.
    op.create_index(
        'ix_packing_lists_order_round',
        'packing_lists',
        ['order_id', 'approval_round'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_packing_lists_order_round', table_name='packing_lists')
    op.drop_column('packing_lists', 'approval_round')
