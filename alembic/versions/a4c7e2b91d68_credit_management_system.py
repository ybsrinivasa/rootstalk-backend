"""credit_management_system

Revision ID: a4c7e2b91d68
Revises: d1a8e4b0f9c3
Create Date: 2026-09-14

Credit Management System (CMS) v1 — shared dual-confirmed ledger
between a dealer and a farmer. See docs/CMS_v1_scoping.md for the
full design.

Three new tables:

(1) `credit_account` — one per (dealer, farmer) pair. Auto-created
    on first credit entry. UNIQUE(dealer_user_id, farmer_user_id)
    enforces the pairing; the account itself is thin — it just anchors
    entries and carries dealer-private notes.

(2) `credit_entry` — the actual ledger. Every credit, payment,
    adjustment, or void is a row. `amount_paise` always positive;
    direction implied by `entry_type`. Balance is computed on read,
    never stored (avoids drift). `due_date` is only meaningful for
    CREDIT_ADVANCED — it anchors the trust score. `entry_date` has
    per-type editability rules enforced in the service layer:
    CREDIT_ADVANCED locked to today, OPENING_BALANCE/PAYMENT_MADE
    editable while PROPOSED.

    Immutability rule: a CONFIRMED entry can never change. Corrections
    happen via paired-void (both parties sign a new adjustment or
    void entry). Enforced in the service layer.

(3) `credit_reminder_pref` — per-user cadence prefs. One row per user,
    created lazily. Farmers only use weekly_summary_enabled +
    new_entry_push_enabled; daily_summary is dealer-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4c7e2b91d68'
down_revision: Union[str, Sequence[str], None] = 'd1a8e4b0f9c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'credit_account',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('dealer_user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('farmer_user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('opened_by', sa.String(length=10), nullable=False),  # DEALER | FARMER
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dealer_notes', sa.Text(), nullable=True),
        sa.UniqueConstraint('dealer_user_id', 'farmer_user_id', name='uq_credit_account_pair'),
    )
    op.create_index('ix_credit_account_dealer', 'credit_account', ['dealer_user_id'])
    op.create_index('ix_credit_account_farmer', 'credit_account', ['farmer_user_id'])

    op.create_table(
        'credit_entry',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('account_id', sa.String(length=36), sa.ForeignKey('credit_account.id'), nullable=False),
        sa.Column('entry_type', sa.String(length=20), nullable=False),
        # OPENING_BALANCE | CREDIT_ADVANCED | PAYMENT_MADE | ADJUSTMENT_UP | ADJUSTMENT_DOWN | VOID
        sa.Column('amount_paise', sa.BigInteger(), nullable=False),
        sa.Column('entry_date', sa.Date(), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('initiated_by', sa.String(length=10), nullable=False),  # DEALER | FARMER
        sa.Column('initiator_user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='PROPOSED'),
        # PROPOSED | CONFIRMED | DISPUTED | VOIDED
        sa.Column('related_sale_id', sa.String(length=36), sa.ForeignKey('dealer_manual_sale.id'), nullable=True),
        sa.Column('payment_method', sa.String(length=20), nullable=True),  # CASH | UPI | BANK | CHEQUE | OTHER
        sa.Column('payment_ref', sa.String(length=200), nullable=True),
        sa.Column('receipt_media_id', sa.String(length=36), nullable=True),
        sa.Column('initiator_note', sa.Text(), nullable=True),
        sa.Column('confirmer_note', sa.Text(), nullable=True),
        sa.Column('dispute_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('confirmer_user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_credit_entry_account', 'credit_entry', ['account_id'])
    op.create_index('ix_credit_entry_account_status', 'credit_entry', ['account_id', 'status'])
    op.create_index('ix_credit_entry_due_date', 'credit_entry', ['due_date'])

    op.create_table(
        'credit_reminder_pref',
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), primary_key=True),
        sa.Column('daily_summary_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('weekly_summary_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('new_entry_push_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('quiet_hours_start', sa.Time(), nullable=True),
        sa.Column('quiet_hours_end', sa.Time(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('credit_reminder_pref')
    op.drop_index('ix_credit_entry_due_date', table_name='credit_entry')
    op.drop_index('ix_credit_entry_account_status', table_name='credit_entry')
    op.drop_index('ix_credit_entry_account', table_name='credit_entry')
    op.drop_table('credit_entry')
    op.drop_index('ix_credit_account_farmer', table_name='credit_account')
    op.drop_index('ix_credit_account_dealer', table_name='credit_account')
    op.drop_table('credit_account')
