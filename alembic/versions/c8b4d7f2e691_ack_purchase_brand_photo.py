"""ack purchase brand + photo (v2 Checkbox 3 follow-up 2026-09-22)

Revision ID: c8b4d7f2e691
Revises: b3f9e1a7d2c4
Create Date: 2026-09-22

Field feedback: when a farmer ticks "I've purchased this" on a
practice whose SE authored a recommended (non-locked) brand, the
current data model has no way to record WHICH brand was actually
bought. If the farmer bought a different brand of the same Common
Name / AI / Formulation (fully legitimate — recommendations are
substitutable), the app silently mis-attributes the purchase to
the recommended brand — reports and any downstream analytics come
out wrong.

Fix: PracticeAcknowledgement gains three nullable columns:

- purchased_brand_cosh_id — Cosh brand trade_name_cosh_id when the
  farmer picked a catalog brand. Same shape as OrderItem.brand_cosh_id.
- purchased_brand_text — free text for brands not in the Cosh
  catalog (farmer's "Other" typing path). MissingBrandReport row
  is written alongside so SA can close the catalog gap.
- purchased_photo_url — optional photo of the purchased product's
  label (uploaded via /media/upload?folder=purchase-photos). Ground
  truth even when brand text is imprecise.

All three nullable + backfill NULL. Farmer opts into each; nothing
is mandatory.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8b4d7f2e691'
down_revision: Union[str, Sequence[str], None] = 'b3f9e1a7d2c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('practice_acknowledgements', sa.Column(
        'purchased_brand_cosh_id', sa.String(100), nullable=True,
    ))
    op.add_column('practice_acknowledgements', sa.Column(
        'purchased_brand_text', sa.String(500), nullable=True,
    ))
    op.add_column('practice_acknowledgements', sa.Column(
        'purchased_photo_url', sa.String(1000), nullable=True,
    ))
    # `missing_brand_reports.source` distinguishes DEALER vs FARMER
    # submissions. Existing rows are all dealer-origin — backfill
    # 'DEALER' on upgrade.
    op.add_column('missing_brand_reports', sa.Column(
        'source', sa.String(20), nullable=True,
    ))
    op.execute("UPDATE missing_brand_reports SET source = 'DEALER' WHERE source IS NULL")


def downgrade() -> None:
    op.drop_column('missing_brand_reports', 'source')
    op.drop_column('practice_acknowledgements', 'purchased_photo_url')
    op.drop_column('practice_acknowledgements', 'purchased_brand_text')
    op.drop_column('practice_acknowledgements', 'purchased_brand_cosh_id')
