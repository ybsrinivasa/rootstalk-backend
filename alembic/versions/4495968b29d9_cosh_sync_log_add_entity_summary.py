"""cosh_sync_log add entity_summary

Revision ID: 4495968b29d9
Revises: 7f9a3fe23558
Create Date: 2026-05-21 07:42:06.267667

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4495968b29d9'
down_revision: Union[str, Sequence[str], None] = '7f9a3fe23558'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Adds cosh_sync_log.entity_summary JSON column. Populated by
    process_payload with [{entity_type, inserted, updated, failed}].
    Surfaced on the SA Sync Log page so each row shows what got
    synced instead of just a UUID and an aggregate count."""
    op.add_column(
        'cosh_sync_log',
        sa.Column('entity_summary', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('cosh_sync_log', 'entity_summary')
