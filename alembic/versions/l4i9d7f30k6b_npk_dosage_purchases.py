"""npk_dosage_purchases — per-brand purchase records for NPK dosage practices

Revision ID: l4i9d7f30k6b
Revises: k3h8c6e29j5a
Create Date: 2026-10-30

Advisory-Only Mode NPK Dosage purchase tracking (2026-10-30). The
existing per-practice "I've purchased this" ack carries one
`purchased_brand_cosh_id`; for NPK dosage practices the farmer
typically buys multiple products (Mixed + Straights for the gap) so
single-brand ack doesn't fit. This table carries one row per brand
the farmer records against an NPK Dosage practice.

Per-row we record:
  - product_role (MIXED / STRAIGHT_N / STRAIGHT_P / STRAIGHT_K) —
    the farmer's intent when confirming the purchase (picked from
    Brands screen's ranked Mixed list or the enabled straights list)
  - match_target (nullable; N/P/K for MIXED only) — the ranking's
    anchor nutrient so we can later compute "apply X kg of this
    product" consistently with how rank_mixed computed its kg_product.

Backend endpoints emit per-record computed quantity:
    kg_total = (dose_nutrient × farm_area_acres) / (concentration/100)
where concentration comes from the common_name's Cosh row.

No cascade from practice/subscription to this table yet — records
survive subscription edits (farmer's purchase history is a factual
record, not a derivable). Soft-delete via DELETE endpoint only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'l4i9d7f30k6b'
down_revision: Union[str, Sequence[str], None] = 'k3h8c6e29j5a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'npk_dosage_purchases',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('subscription_id', sa.String(length=36),
                  sa.ForeignKey('subscriptions.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('practice_id', sa.String(length=36),
                  sa.ForeignKey('practices.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('farmer_user_id', sa.String(length=36),
                  sa.ForeignKey('users.id'), nullable=False),
        # Cosh identity of the chemistry (what rank_mixed/
        # enabled_straights considered). Not strictly needed for
        # display but kept for auditability + so a brand rename in
        # Cosh doesn't orphan the record.
        sa.Column('common_name_cosh_id', sa.String(length=36), nullable=False),
        sa.Column('trade_name_cosh_id', sa.String(length=36), nullable=False),
        # Cached brand name for display — Cosh may rename / inactivate
        # the trade name later, but the farmer's record of what they
        # bought is a factual, point-in-time record.
        sa.Column('trade_name_display', sa.String(length=200), nullable=False),
        # Product role at purchase time — MIXED or STRAIGHT_[NPK].
        # Determines which nutrient's dose anchors the kg computation
        # for Straights; for MIXED the `match_target` column anchors.
        sa.Column('product_role', sa.String(length=20), nullable=False),
        # Only populated for MIXED rows. The ranking's match_target
        # (one of N / P / K) — the nutrient rank_mixed anchored on
        # when ranking this Mixed for the practice's dose. Lets us
        # recompute `kg_product_total × area` identically to the way
        # the Brands screen showed it at pick-time.
        sa.Column('match_target', sa.String(length=4), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        # One brand per (practice, farmer) — "I bought Spic Urea
        # twice" doesn't make sense; the farmer can delete + re-add
        # if they want to change which brand.
        sa.UniqueConstraint(
            'practice_id', 'farmer_user_id', 'trade_name_cosh_id',
            name='uq_npk_dosage_purchase_brand_per_practice',
        ),
    )
    op.create_index(
        'ix_npk_dosage_purchases_practice_id',
        'npk_dosage_purchases', ['practice_id'],
    )
    op.create_index(
        'ix_npk_dosage_purchases_subscription_id',
        'npk_dosage_purchases', ['subscription_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_npk_dosage_purchases_subscription_id',
                  table_name='npk_dosage_purchases')
    op.drop_index('ix_npk_dosage_purchases_practice_id',
                  table_name='npk_dosage_purchases')
    op.drop_table('npk_dosage_purchases')
