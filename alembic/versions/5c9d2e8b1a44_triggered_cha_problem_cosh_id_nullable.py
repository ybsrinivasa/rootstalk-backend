"""triggered_cha_entries: problem_cosh_id nullable for Q&A entries

Revision ID: 5c9d2e8b1a44
Revises: 4b8e2c1a93f5
Create Date: 2026-05-09

L4-real Sub-batch 4. Q&A entries triggered by a Pundit picking a
standard response are rooted in a question, not a Cosh-side problem
identifier. The CHA-flavoured `problem_cosh_id` has no meaningful
value for them. Relax to nullable so the same table hosts both
flavours.

`recommendation_type` is already `String(5)` — the new value 'QA'
fits without a migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5c9d2e8b1a44'
down_revision: Union[str, Sequence[str], None] = '4b8e2c1a93f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'triggered_cha_entries', 'problem_cosh_id',
        existing_type=sa.String(length=200),
        nullable=True,
    )


def downgrade() -> None:
    # Tightening back to NOT NULL would fail if any QA-triggered
    # rows exist — the operator must clear or migrate them first.
    op.alter_column(
        'triggered_cha_entries', 'problem_cosh_id',
        existing_type=sa.String(length=200),
        nullable=False,
    )
