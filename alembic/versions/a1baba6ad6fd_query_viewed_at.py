"""query_viewed_at

Revision ID: a1baba6ad6fd
Revises: 01d7d4dee8e0
Create Date: 2026-06-20

Adds `viewed_at` to `queries` so the dashboard-attention badge can
distinguish "expert has replied" from "farmer has actually read the
reply." Without this, the badge counted every RESPONDED query
forever — the farmer's count never dropped after opening.

Set by the farmer-side `GET /farmer/queries?subscription_id=…`
endpoint when it returns a RESPONDED row to the farmer for the
first time. That endpoint is the natural "I'm reading my queries"
moment (per-sub queries page).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1baba6ad6fd'
down_revision: Union[str, Sequence[str], None] = '01d7d4dee8e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'queries',
        sa.Column('viewed_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('queries', 'viewed_at')
