"""diagnosis_sessions

Revision ID: b9e7d4a3c1f8
Revises: f2a8c3e91b44
Create Date: 2026-05-01 22:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'b9e7d4a3c1f8'
down_revision: Union[str, Sequence[str], None] = 'f2a8c3e91b44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('diagnosis_sessions',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('subscription_id', sa.String(36), sa.ForeignKey('subscriptions.id'), nullable=False),
        sa.Column('farmer_user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('crop_cosh_id', sa.String(100), nullable=False),
        sa.Column('crop_stage_cosh_id', sa.String(100), nullable=True),
        sa.Column('initial_plant_part_cosh_id', sa.String(100), nullable=False),
        sa.Column('remaining_problem_ids', sa.JSON(), nullable=False),
        sa.Column('answers', sa.JSON(), nullable=False),    # [{part, symptom, sub_part, sub_symptom, answer}]
        sa.Column('has_yes_answer', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('status', sa.String(20), server_default='ACTIVE', nullable=False),  # ACTIVE|DIAGNOSED|ABORTED
        sa.Column('diagnosed_problem_cosh_id', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_diagnosis_sessions_farmer', 'diagnosis_sessions', ['farmer_user_id'])
    op.create_index('ix_diagnosis_sessions_subscription', 'diagnosis_sessions', ['subscription_id'])


def downgrade() -> None:
    op.drop_table('diagnosis_sessions')
