"""user facilitator_declared_at

Revision ID: 769a25bb0abb
Revises: e7c9a6540e60
Create Date: 2026-05-21 05:30:16.671202

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '769a25bb0abb'
down_revision: Union[str, Sequence[str], None] = 'e7c9a6540e60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Adds users.facilitator_declared_at — set when the user
    confirms the facilitator declaration on /facilitator/profile.
    The PWA gates /facilitator/home on this being non-null."""
    op.add_column(
        'users',
        sa.Column('facilitator_declared_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('users', 'facilitator_declared_at')
