"""rm_cases_table

Revision ID: e3a7f2c91d55
Revises: b4d7f1e93c22
Create Date: 2026-05-02 15:00:00.000000

Case log for Neytiry RM support interactions.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'e3a7f2c91d55'
down_revision: Union[str, Sequence[str], None] = 'b4d7f1e93c22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'rm_cases',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('raised_by_user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('client_id', sa.String(36), sa.ForeignKey('clients.id'), nullable=True),
        sa.Column('category', sa.String(50), nullable=False, server_default='OTHER'),
        sa.Column('description', sa.Text, nullable=False),
        sa.Column('call_log', sa.Text, nullable=True),
        sa.Column('resolution_status', sa.String(20), nullable=False, server_default='OPEN'),
        sa.Column('is_escalated', sa.Boolean, nullable=False, server_default='false'),
        sa.Column('escalated_note', sa.Text, nullable=True),
        sa.Column('escalated_by_user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_rm_cases_user', 'rm_cases', ['user_id'])
    op.create_index('ix_rm_cases_raised_by', 'rm_cases', ['raised_by_user_id'])


def downgrade() -> None:
    op.drop_index('ix_rm_cases_raised_by', 'rm_cases')
    op.drop_index('ix_rm_cases_user', 'rm_cases')
    op.drop_table('rm_cases')
