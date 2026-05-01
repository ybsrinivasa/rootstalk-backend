"""triggered_cha_problem_name

Revision ID: a3f7e2b19c55
Revises: d4c2f8e91a77
Create Date: 2026-05-02 11:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'a3f7e2b19c55'
down_revision: Union[str, Sequence[str], None] = 'd4c2f8e91a77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Store the problem name for display in advisory ("Treatment for Leaf Blast")
    op.add_column('triggered_cha_entries',
        sa.Column('problem_name', sa.String(500), nullable=True))
    # Store the resolved parent problem_group_cosh_id (for PG fallback traceability)
    op.add_column('triggered_cha_entries',
        sa.Column('parent_pg_cosh_id', sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column('triggered_cha_entries', 'parent_pg_cosh_id')
    op.drop_column('triggered_cha_entries', 'problem_name')
