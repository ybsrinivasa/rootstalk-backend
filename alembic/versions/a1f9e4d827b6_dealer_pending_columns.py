"""dealer_pending_columns

Revision ID: a1f9e4d827b6
Revises: e4b7c9d128fa
Create Date: 2026-08-18

Tentative-until-submit rework. Every dealer action (Postpone / Mark NA /
Set Available with brand+vol+price / Change Selection) previously wrote
`order_items.status` and `brand_cosh_id/brand_name/given_volume/
volume_unit/price` immediately — the row was mutated on every tap. That
leaked tentative state into every downstream consumer of `status`
(farmer counts, is_returned auto-flip, badge queries, etc.) and forced
each one to remember to also gate on `approval_round IS NOT NULL`.

New model: dealer taps write ONLY to `dealer_pending_*` mirror columns.
Live `status` / brand / vol / price stay untouched until the dealer
taps Submit — at that point submit_for_approval promotes pending → live
and stamps `approval_round`. Farmer-facing surfaces read live status
and see nothing new until the submit.

Backfill: ONLY live AVAILABLE items move to the pending columns.
Under the old model, live AVAILABLE was always pre-submit (submit
promoted it to SENT_FOR_APPROVAL), so it maps cleanly to the new
model's "tentative AVAILABLE." Live POSTPONED and NOT_AVAILABLE
items are LEFT ALONE — under the old model those were committed
decisions the farmer already sees, and there's no way to distinguish
committed vs tentative in historical rows (POSTPONED never got
approval_round; NA didn't until 2026-08-17). Moving those to
pending would silently un-commit committed decisions, which is
what happened on staging 2026-08-18 (five orders vanished from
the Postponed pill after backfill ran).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1f9e4d827b6'
down_revision: Union[str, Sequence[str], None] = 'e4b7c9d128fa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('order_items', sa.Column('dealer_pending_status', sa.String(length=30), nullable=True))
    op.add_column('order_items', sa.Column('dealer_pending_brand_cosh_id', sa.String(length=200), nullable=True))
    op.add_column('order_items', sa.Column('dealer_pending_brand_name', sa.String(length=500), nullable=True))
    op.add_column('order_items', sa.Column('dealer_pending_given_volume', sa.DECIMAL(10, 4), nullable=True))
    op.add_column('order_items', sa.Column('dealer_pending_volume_unit', sa.String(length=50), nullable=True))
    op.add_column('order_items', sa.Column('dealer_pending_price', sa.DECIMAL(10, 2), nullable=True))

    op.execute("""
        UPDATE order_items SET
            dealer_pending_status = 'AVAILABLE',
            dealer_pending_brand_cosh_id = brand_cosh_id,
            dealer_pending_brand_name = brand_name,
            dealer_pending_given_volume = given_volume,
            dealer_pending_volume_unit = volume_unit,
            dealer_pending_price = price,
            status = 'PENDING',
            brand_cosh_id = NULL,
            brand_name = NULL,
            given_volume = NULL,
            volume_unit = NULL,
            price = NULL
        WHERE approval_round IS NULL
          AND status = 'AVAILABLE'
          AND archived_at IS NULL
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE order_items SET
            status = COALESCE(dealer_pending_status, status),
            brand_cosh_id = COALESCE(dealer_pending_brand_cosh_id, brand_cosh_id),
            brand_name = COALESCE(dealer_pending_brand_name, brand_name),
            given_volume = COALESCE(dealer_pending_given_volume, given_volume),
            volume_unit = COALESCE(dealer_pending_volume_unit, volume_unit),
            price = COALESCE(dealer_pending_price, price)
        WHERE dealer_pending_status IS NOT NULL
    """)
    op.drop_column('order_items', 'dealer_pending_price')
    op.drop_column('order_items', 'dealer_pending_volume_unit')
    op.drop_column('order_items', 'dealer_pending_given_volume')
    op.drop_column('order_items', 'dealer_pending_brand_name')
    op.drop_column('order_items', 'dealer_pending_brand_cosh_id')
    op.drop_column('order_items', 'dealer_pending_status')
