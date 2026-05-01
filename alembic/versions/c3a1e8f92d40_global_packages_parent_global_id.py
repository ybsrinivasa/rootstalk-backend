"""global_packages_parent_global_id

Revision ID: c3a1e8f92d40
Revises: 7f2742301d00
Create Date: 2026-05-01 16:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'c3a1e8f92d40'
down_revision: Union[str, Sequence[str], None] = '3f543047d712'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Make client_id nullable so global packages (client_id=NULL) are supported
    op.alter_column('packages', 'client_id', nullable=True)
    # Track which global package a client copy was forked from
    op.add_column('packages', sa.Column('parent_global_id', sa.String(36), nullable=True))
    op.create_foreign_key('fk_packages_parent_global', 'packages', 'packages',
                          ['parent_global_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_packages_parent_global', 'packages', type_='foreignkey')
    op.drop_column('packages', 'parent_global_id')
    op.alter_column('packages', 'client_id', nullable=False)
