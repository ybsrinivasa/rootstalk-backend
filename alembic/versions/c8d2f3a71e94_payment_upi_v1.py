"""payment_upi_v1

Revision ID: c8d2f3a71e94
Revises: b7c3e2a4d8f1
Create Date: 2026-08-21

UPI payment v1. Two changes:

(1) DealerProfile gains three payment-setup fields entered on the
Shop Details page — upi_vpa (dealer's UPI address / VPA), upi_phone
(phone linked to UPI; may differ from login phone), payment_display_name
(what farmer sees in their UPI app; defaults to shop_name when NULL).

(2) `batch_payments` table — one row per (order_id, approval_round)
capturing the payment lifecycle for that batch. Status machine:
    PENDING → FARMER_MARKED_PAID → DEALER_CONFIRMED
Fields farmer_marked_at / dealer_confirmed_at timestamp each edge.
txn_ref is the farmer-supplied UPI transaction reference (optional at
mark-paid; useful for dealer's reconciliation). mode restricted to
'UPI' in v1; Cash/Credit come later per user 2026-08-21.

Cash and Credit modes are NOT in v1 — user's ordering was "start UPI
now, plan Credit later." Table shape accommodates them via `mode`
column when we're ready.

No backfill — new table starts empty. New profile fields default NULL
(existing dealers can add them via Shop Details when they want to
enable UPI).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8d2f3a71e94'
down_revision: Union[str, Sequence[str], None] = 'b7c3e2a4d8f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # (1) DealerProfile payment-setup fields
    op.add_column('dealer_profiles', sa.Column('upi_vpa', sa.String(length=100), nullable=True))
    op.add_column('dealer_profiles', sa.Column('upi_phone', sa.String(length=20), nullable=True))
    op.add_column('dealer_profiles', sa.Column('payment_display_name', sa.String(length=200), nullable=True))

    # (2) batch_payments — per-(order, approval_round) payment lifecycle
    op.create_table(
        'batch_payments',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('order_id', sa.String(length=36), sa.ForeignKey('orders.id'), nullable=False),
        sa.Column('approval_round', sa.Integer(), nullable=False),
        sa.Column('mode', sa.String(length=20), nullable=False),  # 'UPI' | 'CASH' | 'CREDIT' (v2)
        sa.Column('amount', sa.DECIMAL(12, 2), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),  # PENDING / FARMER_MARKED_PAID / DEALER_CONFIRMED
        sa.Column('txn_ref', sa.String(length=100), nullable=True),
        sa.Column('farmer_marked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dealer_confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('order_id', 'approval_round', name='uq_batch_payment_order_round'),
    )
    op.create_index('ix_batch_payments_order', 'batch_payments', ['order_id'])
    op.create_index('ix_batch_payments_status', 'batch_payments', ['status'])


def downgrade() -> None:
    op.drop_index('ix_batch_payments_status', table_name='batch_payments')
    op.drop_index('ix_batch_payments_order', table_name='batch_payments')
    op.drop_table('batch_payments')
    op.drop_column('dealer_profiles', 'payment_display_name')
    op.drop_column('dealer_profiles', 'upi_phone')
    op.drop_column('dealer_profiles', 'upi_vpa')
