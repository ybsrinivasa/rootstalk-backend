"""dealer_pending_final_confirmation

Revision ID: b7c3e2a4d8f1
Revises: a1f9e4d827b6
Create Date: 2026-08-19

Applies the tentative-until-submit pattern to Final Confirmation.
Prior model: dealer tapped Final Confirm / Cancel on a Packing card
and each tap committed instantly — no second chance for a mistap in
a rural shop.

New model: per-item taps set `dealer_pending_final_confirmation`
('CONFIRM' | 'CANCEL' | NULL). Live `final_confirmed_at` / status
only move when the dealer taps a batch-level Submit that requires
every APPROVED-not-yet-FC item in the batch to carry a tentative
decision first. Same rhythm as the order-level Submit for Approval /
Submit Response flow — one mental model across the dealer's day.

Backfill: none. Pending starts at NULL for every existing row;
future dealer taps land there directly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c3e2a4d8f1'
down_revision: Union[str, Sequence[str], None] = 'a1f9e4d827b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'order_items',
        sa.Column('dealer_pending_final_confirmation', sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('order_items', 'dealer_pending_final_confirmation')
