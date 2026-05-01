"""client_promoters

Revision ID: e7f4b2c91d88
Revises: c3a1e8f92d40
Create Date: 2026-05-01 18:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'e7f4b2c91d88'
down_revision: Union[str, Sequence[str], None] = 'c3a1e8f92d40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('client_promoters',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('client_id', sa.String(36), sa.ForeignKey('clients.id'), nullable=False),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('promoter_type', sa.String(20), nullable=False),  # DEALER / FACILITATOR
        sa.Column('status', sa.String(20), default='ACTIVE', nullable=False, server_default='ACTIVE'),
        sa.Column('territory_notes', sa.Text(), nullable=True),
        sa.Column('registered_by', sa.String(36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('registered_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('client_id', 'user_id', 'promoter_type', name='uq_client_promoter_role'),
    )
    op.create_index('ix_client_promoters_client_id', 'client_promoters', ['client_id'])
    op.create_index('ix_client_promoters_user_id', 'client_promoters', ['user_id'])


def downgrade() -> None:
    op.drop_table('client_promoters')
