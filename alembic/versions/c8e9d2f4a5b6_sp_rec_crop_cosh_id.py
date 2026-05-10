"""sp_recommendations: drop application_type, add crop_cosh_id

Revision ID: c8e9d2f4a5b6
Revises: a7c1f3e62b81
Create Date: 2026-05-10

CHA-SP hub Round 1: SE picks a crop first, then a specific problem
within that crop's list. Snapshotting `crop_cosh_id` on the row
keeps SP list-filtering fast and gives a clean audit trail of "the
SE was authoring for Tomato".

`application_type` ('SPRAY' / 'DRENCH' / 'SOIL') is dropped — same
reason it was dropped from PG (legacy, unused in V1 logic, lives at
the practice/element layer if needed).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8e9d2f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'a7c1f3e62b81'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'sp_recommendations',
        sa.Column('crop_cosh_id', sa.String(100), nullable=True),
    )
    op.drop_column('sp_recommendations', 'application_type')


def downgrade() -> None:
    op.add_column(
        'sp_recommendations',
        sa.Column('application_type', sa.String(20), nullable=True),
    )
    op.drop_column('sp_recommendations', 'crop_cosh_id')
