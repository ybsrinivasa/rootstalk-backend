"""student portfolios — testimonial + hiring profile

Revision ID: n6k1f9h52m8d
Revises: m5j0e8g41l7c
Create Date: 2026-11-04

Two surfaces students build up after certification:
  (a) a Testimonial profile — photo, short video, written testimonial
      — that RootsTalk may use for marketing, each asset gated by its
      own consent flag so a student can share one without the others.
  (b) a Hiring profile — CV versions (PDF only, 100 MB cap enforced in
      the upload endpoint) + employment status (is_employed and
      is_open_to_offers can both be true, employed-but-looking is
      supported) + a single "share my CV with partner companies"
      consent flag.

Keyed by user_id (not coaching_students.id) because the profile is a
property of the person, not a cohort — a second-time student shouldn't
re-upload. PWA access to the surface is gated by having at least one
CoachingStudent row with certified_at IS NOT NULL (enforced in the
router, not the schema).

`student_cvs` keeps all upload history. A partial unique index
guarantees at most one `is_latest=true` row per user_id, so the
"current CV" lookup stays O(1) and the handler flipping the flag at
insert time is race-safe.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'n6k1f9h52m8d'
down_revision: Union[str, Sequence[str], None] = 'm5j0e8g41l7c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'student_portfolios',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column(
            'user_id', sa.String(length=36),
            sa.ForeignKey('users.id', ondelete='CASCADE'),
            nullable=False, unique=True,
        ),

        # Marketing assets.
        sa.Column('photo_url', sa.Text(), nullable=True),
        sa.Column('video_url', sa.Text(), nullable=True),
        sa.Column('testimonial_text', sa.Text(), nullable=True),
        sa.Column(
            'marketing_consent_photo', sa.Boolean(),
            nullable=False, server_default=sa.text('false'),
        ),
        sa.Column(
            'marketing_consent_video', sa.Boolean(),
            nullable=False, server_default=sa.text('false'),
        ),
        sa.Column(
            'marketing_consent_testimonial', sa.Boolean(),
            nullable=False, server_default=sa.text('false'),
        ),

        # Hiring profile.
        sa.Column(
            'is_employed', sa.Boolean(),
            nullable=False, server_default=sa.text('false'),
        ),
        sa.Column('employer_name', sa.String(length=500), nullable=True),
        sa.Column('employed_since', sa.Date(), nullable=True),
        sa.Column(
            'is_open_to_offers', sa.Boolean(),
            nullable=False, server_default=sa.text('false'),
        ),
        sa.Column('open_to_offers_notes', sa.Text(), nullable=True),
        sa.Column(
            'hiring_consent_share_cv', sa.Boolean(),
            nullable=False, server_default=sa.text('false'),
        ),

        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text('now()'),
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text('now()'),
        ),
    )

    op.create_table(
        'student_cvs',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column(
            'user_id', sa.String(length=36),
            sa.ForeignKey('users.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('file_url', sa.Text(), nullable=False),
        sa.Column('filename', sa.String(length=500), nullable=False),
        sa.Column(
            'is_latest', sa.Boolean(),
            nullable=False, server_default=sa.text('false'),
        ),
        sa.Column(
            'uploaded_at', sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text('now()'),
        ),
    )
    op.create_index(
        'ix_student_cvs_user_id', 'student_cvs', ['user_id'],
    )
    # At most one is_latest=true row per user — flipping the current
    # pointer in the insert handler is race-safe because Postgres
    # rejects a concurrent duplicate `true`.
    op.create_index(
        'ix_student_cvs_latest_per_user', 'student_cvs', ['user_id'],
        unique=True, postgresql_where=sa.text('is_latest = true'),
    )


def downgrade() -> None:
    op.drop_index('ix_student_cvs_latest_per_user', table_name='student_cvs')
    op.drop_index('ix_student_cvs_user_id', table_name='student_cvs')
    op.drop_table('student_cvs')
    op.drop_table('student_portfolios')
