"""add training_expert_user_id and training_dealer_user_id to clients

Revision ID: d2349be0895d
Revises: 923c943d216e
Create Date: 2026-08-03

Per-session assignment slots on the training-child Client row:

  training_expert_user_id — one ACTIVE onboarded PE of the parent
    client whom the CA has marked as the go-to expert for THIS
    training session. Training-farmer queries route directly to
    this user's device instead of the round-robin queue.

  training_dealer_user_id — one User row (must be ACTIVE, have a
    DealerProfile, and NOT be in any client's ClientPromoter) that
    the CA has designated as the demo dealer for the session. The
    training-farmer's recipient picker surfaces this row with a
    "Training" chip.

Both are optional. Both live on the training-child row, so the
existing hard-close cascade in `_cascade_delete_training_child`
drops them automatically when the session ends.

Only meaningful when Client.is_training=True; enforced at the
application layer (adding a CHECK would complicate the migration
without adding real safety since the child row is short-lived).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2349be0895d'
down_revision: Union[str, Sequence[str], None] = '923c943d216e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'clients',
        sa.Column(
            'training_expert_user_id',
            sa.String(length=36),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )
    op.add_column(
        'clients',
        sa.Column(
            'training_dealer_user_id',
            sa.String(length=36),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('clients', 'training_dealer_user_id')
    op.drop_column('clients', 'training_expert_user_id')
