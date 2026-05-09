"""pg_timelines: dual-FK to host Q&A advisories (UCAT pipe-3)

Revision ID: 4b8e2c1a93f5
Revises: 3a7c4e1f9b22
Create Date: 2026-05-09

L4-real Sub-batch 1. Per the user's UCAT framing (Universal Crop
Advisory Template), CCA / CHA / Q&A all share the Timeline →
Practice → Element shape; only the trigger / anchor differs. To
host Q&A timelines without duplicating tables, this migration
extends `pg_timelines` to admit either a PG-recommendation parent
OR a standard-response parent. Practices and Elements (which FK
to timeline_id) are reused as-is.

Also drops `answer_text` / `answer_media` from `standard_responses`
— they were the V1 notepad-style columns shipped earlier today
(commit 40f4238). Under UCAT a standard response carries Timelines,
not free-form text; the notepad columns become dead weight. The
Pundit's optional free-form text/media fallback (used when neither
problem-pick nor standard-pick applies) lives on QueryResponse,
not on StandardResponse.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4b8e2c1a93f5'
down_revision: Union[str, Sequence[str], None] = '3a7c4e1f9b22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Allow pg_timelines to host QA-rooted timelines.
    op.alter_column(
        'pg_timelines', 'pg_recommendation_id',
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.add_column(
        'pg_timelines',
        sa.Column(
            'standard_response_id', sa.String(length=36), nullable=True,
        ),
    )
    op.create_foreign_key(
        'pg_timelines_standard_response_id_fkey',
        'pg_timelines', 'standard_responses',
        ['standard_response_id'], ['id'],
    )

    # 2. Exactly-one-parent invariant. Enforced at the DB level so
    #    neither application code nor a future migration can produce
    #    orphan rows. CAST is a Postgres-ism but the project is
    #    Postgres-only.
    op.create_check_constraint(
        'pg_timelines_one_parent_chk',
        'pg_timelines',
        '(CASE WHEN pg_recommendation_id IS NOT NULL THEN 1 ELSE 0 END) '
        '+ (CASE WHEN standard_response_id IS NOT NULL THEN 1 ELSE 0 END) = 1',
    )

    # 3. Drop dead columns on standard_responses — see header comment.
    op.drop_column('standard_responses', 'answer_text')
    op.drop_column('standard_responses', 'answer_media')


def downgrade() -> None:
    # Restore the notepad columns first; they may have been read by
    # the old CRUD UI. Backfill is None — old behaviour with NULL
    # answer body is the empty-state the UI rendered anyway.
    op.add_column(
        'standard_responses',
        sa.Column('answer_media', sa.JSON(), nullable=True),
    )
    op.add_column(
        'standard_responses',
        sa.Column('answer_text', sa.Text(), nullable=True),
    )

    # Drop the polymorphism. If any QA-rooted rows exist, this will
    # fail the NOT NULL re-tightening — the operator must clear them
    # first. That's intentional: downgrade is a recovery path, not a
    # regular operation.
    op.drop_constraint(
        'pg_timelines_one_parent_chk', 'pg_timelines', type_='check',
    )
    op.drop_constraint(
        'pg_timelines_standard_response_id_fkey',
        'pg_timelines', type_='foreignkey',
    )
    op.drop_column('pg_timelines', 'standard_response_id')
    op.alter_column(
        'pg_timelines', 'pg_recommendation_id',
        existing_type=sa.String(length=36),
        nullable=False,
    )
