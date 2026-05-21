"""dealer_relationships add manufacturer_cosh_id + category

Revision ID: a5019f0b7c98
Revises: 769a25bb0abb
Create Date: 2026-05-21 07:09:52.372960

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5019f0b7c98'
down_revision: Union[str, Sequence[str], None] = '769a25bb0abb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Adds dealer_relationships.manufacturer_cosh_id (Cosh UUID
    of the picked manufacturer) and .category (PESTICIDE |
    FERTILIZER — same manufacturer can appear once per category).
    Both nullable; pre-2026-05-21 free-text rows are preserved."""
    op.add_column(
        'dealer_relationships',
        sa.Column('manufacturer_cosh_id', sa.String(length=100), nullable=True),
    )
    op.add_column(
        'dealer_relationships',
        sa.Column('category', sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('dealer_relationships', 'category')
    op.drop_column('dealer_relationships', 'manufacturer_cosh_id')
