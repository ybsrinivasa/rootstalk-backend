"""practice_acknowledgements

Revision ID: 01d7d4dee8e0
Revises: c4a91e07f3d6
Create Date: 2026-06-19 23:34:52.196266

Adds the per-occurrence acknowledgement table backing the farmer's
"I've done this" tick on each advisory practice card + the dashboard
badge math. See `project_rootstalk_practice_ack_2026_06_19.md`.

Schema notes:
- `timeline_lineage_id` (NOT timeline.id) — survives publishes.
- `occurrence_date` discriminates frequency-recurring practices
  (Day 5 fertigation vs Day 12 fertigation are separate acks). For
  non-frequency practices the PWA sets it to the timeline's from_date
  so the ack is stable across the whole window.
- `marked_at` / `hidden_at` — two timestamps. UI states:
    - both null       → ACTIVE (tick is grey, counts toward badge)
    - marked, !hidden → MARKED (tick is green, delete button visible)
    - hidden          → invisible to farmer (no undo from PWA)
- Unique on (sub, lineage, practice, occurrence_date) so the upsert
  pattern doesn't dup-write.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '01d7d4dee8e0'
down_revision: Union[str, Sequence[str], None] = 'c4a91e07f3d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'practice_acknowledgements',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('farmer_user_id', sa.String(36),
                  sa.ForeignKey('users.id'), nullable=False),
        sa.Column('subscription_id', sa.String(36),
                  sa.ForeignKey('subscriptions.id'), nullable=False),
        sa.Column('timeline_lineage_id', sa.String(100), nullable=False),
        sa.Column('practice_id', sa.String(36),
                  sa.ForeignKey('practices.id'), nullable=False),
        sa.Column('occurrence_date', sa.Date, nullable=False),
        sa.Column('marked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('hidden_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            'subscription_id', 'timeline_lineage_id', 'practice_id', 'occurrence_date',
            name='uq_practice_ack_key',
        ),
    )
    op.create_index('ix_practice_ack_sub', 'practice_acknowledgements',
                    ['subscription_id'])
    op.create_index('ix_practice_ack_farmer', 'practice_acknowledgements',
                    ['farmer_user_id'])


def downgrade() -> None:
    op.drop_index('ix_practice_ack_farmer', table_name='practice_acknowledgements')
    op.drop_index('ix_practice_ack_sub', table_name='practice_acknowledgements')
    op.drop_table('practice_acknowledgements')
