"""decision_v_pct_adjustments

Revision ID: j2g7b5d18i49
Revises: i1f6a4c07h38
Create Date: 2026-10-17

Decision V (settled 2026-10-17) — per-parameter % adjustment on
classification. The engine needs a concrete rule like "if N is Low,
raise the target by X%; if High, lower it by Y%". These two columns
on the Normal Range row supply the X and Y.

Scope mirrors the two-tier Normal Range storage: crop-level default
on `client_normal_ranges`, Package-level override on
`package_normal_range_overrides`. Both default to 0.0 (= no
adjustment) so existing rows interpret cleanly without a backfill
pass.

Operator-gated validity (enforced at the service layer, not the DB —
the DB stays permissive so UI can leave both at 0.0 without failing
inserts):
  RANGE        — both low_pct_adjustment and high_pct_adjustment valid
  UPPER_BOUND  — only high_pct_adjustment is meaningful; low_pct_adjustment
                 must be 0.0
  LOWER_BOUND  — only low_pct_adjustment is meaningful; high_pct_adjustment
                 must be 0.0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'j2g7b5d18i49'
down_revision: Union[str, Sequence[str], None] = 'i1f6a4c07h38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ('client_normal_ranges', 'package_normal_range_overrides'):
        op.add_column(table, sa.Column(
            'low_pct_adjustment', sa.Float(),
            nullable=False, server_default='0',
        ))
        op.add_column(table, sa.Column(
            'high_pct_adjustment', sa.Float(),
            nullable=False, server_default='0',
        ))


def downgrade() -> None:
    for table in ('client_normal_ranges', 'package_normal_range_overrides'):
        op.drop_column(table, 'high_pct_adjustment')
        op.drop_column(table, 'low_pct_adjustment')
