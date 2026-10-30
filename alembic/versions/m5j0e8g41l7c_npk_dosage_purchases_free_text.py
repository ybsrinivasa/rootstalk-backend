"""npk_dosage_purchases — free-text brand entry + photo support

Revision ID: m5j0e8g41l7c
Revises: l4i9d7f30k6b
Create Date: 2026-10-30

Advisory-Only Mode NPK dosage practices: let farmers record a
purchase for a brand that isn't in Cosh's catalogue yet (shop sells
a product RootsTalk hasn't catalogued). Companion flow to the
pesticide-side "Not on this list? Type it in" affordance.

Column changes on `npk_dosage_purchases`:
  - common_name_cosh_id / trade_name_cosh_id → nullable (free-text
    rows carry no Cosh refs; we display the farmer's typed name).
  - source: 'COSH' (default, catalogued brand) | 'FARMER_ENTERED'
    (free-text).
  - photo_url: nullable S3 URL for a bag-label photo.

Backfill: existing rows set source='COSH' (that's what the first
cohort recorded).

Unique constraint stays on (practice_id, farmer_user_id,
trade_name_cosh_id) because UNIQUE treats NULL as distinct in
Postgres — so multiple free-text rows per farmer per practice are
fine; dupes within trade_name_display are a client-side concern.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'm5j0e8g41l7c'
down_revision: Union[str, Sequence[str], None] = 'l4i9d7f30k6b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'npk_dosage_purchases',
        sa.Column('source', sa.String(length=20),
                  nullable=False, server_default='COSH'),
    )
    op.add_column(
        'npk_dosage_purchases',
        sa.Column('photo_url', sa.Text(), nullable=True),
    )
    op.alter_column(
        'npk_dosage_purchases', 'common_name_cosh_id',
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.alter_column(
        'npk_dosage_purchases', 'trade_name_cosh_id',
        existing_type=sa.String(length=36),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        'npk_dosage_purchases', 'trade_name_cosh_id',
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.alter_column(
        'npk_dosage_purchases', 'common_name_cosh_id',
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.drop_column('npk_dosage_purchases', 'photo_url')
    op.drop_column('npk_dosage_purchases', 'source')
