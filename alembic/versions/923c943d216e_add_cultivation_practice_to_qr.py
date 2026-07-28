"""add_cultivation_practice_to_product_qr_codes

Revision ID: 923c943d216e
Revises: c4b9e18f5d20
Create Date: 2026-07-28

Per-QR free-text describing the cultivation practice for the batch,
authored by the Client and rendered on the public /verify/{qr_id}
landing. Complements SeedVariety.cultivation_notes which is set at
variety level and only applies to seed products.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '923c943d216e'
down_revision: Union[str, Sequence[str], None] = 'c4b9e18f5d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'product_qr_codes',
        sa.Column('cultivation_practice', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('product_qr_codes', 'cultivation_practice')
