"""coaching_student_feedback

Revision ID: e6a7b4c13f29
Revises: d5d3f574e218
Create Date: 2026-10-16

Student Expectations + Feedback mechanism (2026-10-16 design).

Two new tables:
  - coaching_student_moment1_responses — expectations captured at
    onboarding (optional, imagination-oriented). One row per student;
    answers_json keyed by question identifier. Updateable during
    ACTIVE coaching.
  - coaching_student_moment2_feedbacks — feedback captured post-
    session via a coach-triggered one-click email flow. feedback_token
    authenticates the student's one-click form access. content_json
    is NULL until submitted. Reminder cadence timestamps track 48h
    gentle email + 96h coach dashboard nudge.

Certification gate enforces submitted_at IS NOT NULL at the service
layer (not here as a CHECK — allows the coach-with-SA-approval
override design that may ship later).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e6a7b4c13f29'
# prod-safe-soil-2026-10-22: this migration shipped to prod on
# prod-safe-2026-10-18 with down_revision=c8b4d7f2e691 (soil skipped).
# To keep the actual applied history consistent and let the soil
# chain apply forward from the current prod head, we re-root the
# soil chain OFF this branch (see d1a0f574e215.down_revision below).
# e6a7b4c13f29 therefore matches its prod-applied ancestor.
down_revision: Union[str, Sequence[str], None] = 'c8b4d7f2e691'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'coaching_student_moment1_responses',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('student_id', sa.String(length=36),
                  sa.ForeignKey('coaching_students.id', ondelete='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('answers_json', sa.JSON(), nullable=False,
                  server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        'coaching_student_moment2_feedbacks',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('student_id', sa.String(length=36),
                  sa.ForeignKey('coaching_students.id', ondelete='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('feedback_token', sa.String(length=64), nullable=False,
                  unique=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('requested_by_coach_user_id', sa.String(length=36),
                  sa.ForeignKey('users.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('content_json', sa.JSON(), nullable=True),
        sa.Column('reminder_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('coach_nudge_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        'ix_coaching_moment2_requested_at',
        'coaching_student_moment2_feedbacks', ['requested_at'],
    )
    op.create_index(
        'ix_coaching_moment2_submitted_at',
        'coaching_student_moment2_feedbacks', ['submitted_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_coaching_moment2_submitted_at',
                  table_name='coaching_student_moment2_feedbacks')
    op.drop_index('ix_coaching_moment2_requested_at',
                  table_name='coaching_student_moment2_feedbacks')
    op.drop_table('coaching_student_moment2_feedbacks')
    op.drop_table('coaching_student_moment1_responses')
