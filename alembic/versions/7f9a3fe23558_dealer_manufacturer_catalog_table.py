"""dealer_manufacturer_catalog table

Revision ID: 7f9a3fe23558
Revises: a5019f0b7c98
Create Date: 2026-05-21 07:22:14.378471

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7f9a3fe23558'
down_revision: Union[str, Sequence[str], None] = 'a5019f0b7c98'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Materialised view of the Cosh manufacturer walk per dealer
    category. Truncate-and-reload write path; lazy populate on
    first read; manual refresh via admin endpoint."""
    op.create_table(
        'dealer_manufacturer_catalog',
        sa.Column('category', sa.String(length=20), nullable=False),
        sa.Column('manufacturer_cosh_id', sa.String(length=100), nullable=False),
        sa.Column('manufacturer_name', sa.String(length=500), nullable=False),
        sa.Column('refreshed_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('category', 'manufacturer_cosh_id',
                                name='pk_dealer_manufacturer_catalog'),
    )


def downgrade() -> None:
    op.drop_table('dealer_manufacturer_catalog')
