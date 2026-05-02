"""qr_scan_verified_on_order_item

Revision ID: b4d7f1e93c22
Revises: c8a3f2e91b77
Create Date: 2026-05-02 13:00:00.000000

Adds scan_verified flag to order_items so the farmer's 'Verified Genuine Product'
badge is stored permanently, and adds scan_attempt_number tracking.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'b4d7f1e93c22'
down_revision: Union[str, Sequence[str], None] = 'c8a3f2e91b77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('order_items', sa.Column('scan_verified', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('qr_scans', sa.Column('expected_brand_cosh_id', sa.String(200), nullable=True))
    op.add_column('qr_scans', sa.Column('scanned_brand_cosh_id', sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column('qr_scans', 'scanned_brand_cosh_id')
    op.drop_column('qr_scans', 'expected_brand_cosh_id')
    op.drop_column('order_items', 'scan_verified')
