"""client_promoters: add is_promoter flag

Revision ID: 250e9b0d3abc
Revises: d8b1e6c4a572
Create Date: 2026-05-08 20:26:36.815205

Option C of the audit (2026-05-08): separate the company-onboarding
link from the Promoter designation. Pre-Option-C, every row in
`client_promoters` was treated as both — the row's existence meant
the user was a Promoter. Now, `is_promoter` is the explicit flag
on top of the link.

Backfill: existing rows are set to `is_promoter=True` to preserve
current behaviour. New rows created by the existing CA-portal flow
also default to True until the V1.1 redesign separates the two
steps in the UI.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '250e9b0d3abc'
down_revision: Union[str, Sequence[str], None] = 'd8b1e6c4a572'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Three-step backfill avoids needing a default at the SQL level
    # while keeping the column NOT NULL in the final state.
    # 1. Add nullable.
    op.add_column(
        'client_promoters',
        sa.Column('is_promoter', sa.Boolean(), nullable=True),
    )
    # 2. Backfill existing rows. Pre-Option-C, every row in this
    #    table = a Promoter, so True preserves current behaviour.
    op.execute("UPDATE client_promoters SET is_promoter = TRUE WHERE is_promoter IS NULL")
    # 3. Tighten to NOT NULL.
    op.alter_column(
        'client_promoters', 'is_promoter',
        existing_type=sa.Boolean(),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column('client_promoters', 'is_promoter')
