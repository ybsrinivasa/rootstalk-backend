"""conditional_answers_triggered_cha

Revision ID: d4c2f8e91a77
Revises: b9e7d4a3c1f8
Create Date: 2026-05-02 09:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'd4c2f8e91a77'
down_revision: Union[str, Sequence[str], None] = 'b9e7d4a3c1f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # BL-02: Stores farmer's YES/NO answers to conditional questions for one day
    op.create_table('conditional_answers',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('subscription_id', sa.String(36), sa.ForeignKey('subscriptions.id'), nullable=False),
        sa.Column('question_id', sa.String(36), sa.ForeignKey('conditional_questions.id'), nullable=False),
        sa.Column('answer_date', sa.Date(), nullable=False),   # YYYY-MM-DD — one answer per day per question
        sa.Column('answer', sa.String(10), nullable=False),    # YES | NO | BLANK
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('subscription_id', 'question_id', 'answer_date',
                            name='uq_conditional_answer_per_day'),
    )
    op.create_index('ix_cond_answers_sub_date', 'conditional_answers',
                    ['subscription_id', 'answer_date'])

    # CHA triggered by diagnosis or FarmPundit query response.
    # Links a subscription to the PG or SP recommendation that should appear in their advisory.
    op.create_table('triggered_cha_entries',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('subscription_id', sa.String(36), sa.ForeignKey('subscriptions.id'), nullable=False),
        sa.Column('farmer_user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('client_id', sa.String(36), sa.ForeignKey('clients.id'), nullable=False),
        sa.Column('problem_cosh_id', sa.String(200), nullable=False),
        sa.Column('recommendation_type', sa.String(5), nullable=False),  # SP | PG
        sa.Column('recommendation_id', sa.String(36), nullable=False),   # FK to sp_recommendations or pg_recommendations
        sa.Column('triggered_by', sa.String(20), nullable=False),         # DIAGNOSIS | QUERY | DIRECT
        sa.Column('triggered_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(20), server_default='ACTIVE', nullable=False),  # ACTIVE | DISMISSED
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_triggered_cha_sub', 'triggered_cha_entries', ['subscription_id'])
    op.create_index('ix_triggered_cha_farmer', 'triggered_cha_entries', ['farmer_user_id'])


def downgrade() -> None:
    op.drop_table('triggered_cha_entries')
    op.drop_table('conditional_answers')
