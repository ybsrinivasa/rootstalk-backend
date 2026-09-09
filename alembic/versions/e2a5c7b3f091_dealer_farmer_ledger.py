"""dealer_farmer_ledger

Revision ID: e2a5c7b3f091
Revises: d0e4a51b9c72
Create Date: 2026-09-09

Farmer Ledger v1 — dealer-facing farmer roster + purchase history.
See project_rootstalk_dealer_farmer_ledger for the design.

Two new tables:

(1) `dealer_manual_sale` — dealer-recorded sale entries for farmers who
    did NOT transact via RootsTalk's own order flow (walk-in cash sales,
    off-app credit, ledger entries brought in from a paper passbook).
    Deliberately has NO `subscription_id` — manual entries must NEVER
    feed the farmer's advisory (dealer typos would pollute the
    recommendation engine). Manual entries are the dealer's private
    record; the farmer's advisory sees only own RT-mediated purchases.

(2) `dealer_farmer_note` — per-dealer per-farmer free-text note (the
    passbook marginalia analog: "prefers X brand", "always pays late").
    UNIQUE(dealer_user_id, farmer_user_id) so each dealer maintains
    exactly one note per farmer. Notes are dealer-private — never
    exposed to any other dealer, the farmer, or the CA/SA surfaces.

Roster + own-shop history are read straight off PackingList
(picked_up_at IS NOT NULL) + Order + OrderItem — no new tables needed.
Cross-shop anonymised rows are the same read against OTHER dealers'
picked-up PackingLists, projected to brand+manufacturer+qty (no
dealer_user_id, no price).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2a5c7b3f091'
down_revision: Union[str, Sequence[str], None] = 'd0e4a51b9c72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dealer_manual_sale',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('dealer_user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('farmer_user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('category', sa.String(length=20), nullable=False),  # SEED | PESTICIDE | FERTILIZER
        sa.Column('product_name', sa.String(length=255), nullable=False),
        sa.Column('brand', sa.String(length=255), nullable=True),
        sa.Column('manufacturer', sa.String(length=255), nullable=True),
        sa.Column('qty', sa.DECIMAL(12, 3), nullable=False),
        sa.Column('unit', sa.String(length=20), nullable=False),  # kg / L / packet / ...
        sa.Column('price', sa.DECIMAL(12, 2), nullable=True),
        sa.Column('sale_date', sa.Date(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        'ix_dealer_manual_sale_dealer_farmer',
        'dealer_manual_sale',
        ['dealer_user_id', 'farmer_user_id'],
    )
    op.create_index(
        'ix_dealer_manual_sale_dealer_sale_date',
        'dealer_manual_sale',
        ['dealer_user_id', 'sale_date'],
    )

    op.create_table(
        'dealer_farmer_note',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('dealer_user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('farmer_user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('dealer_user_id', 'farmer_user_id', name='uq_dealer_farmer_note'),
    )


def downgrade() -> None:
    op.drop_table('dealer_farmer_note')
    op.drop_index('ix_dealer_manual_sale_dealer_sale_date', table_name='dealer_manual_sale')
    op.drop_index('ix_dealer_manual_sale_dealer_farmer', table_name='dealer_manual_sale')
    op.drop_table('dealer_manual_sale')
