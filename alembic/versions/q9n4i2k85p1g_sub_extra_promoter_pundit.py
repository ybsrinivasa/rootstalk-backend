"""extra promoter-pundit column + audit log — Farmer Support v2 #2

Revision ID: q9n4i2k85p1g
Revises: p8m3h1j74o0f
Create Date: 2026-11-14

Adds per-subscription pinned Promoter-Pundit override:
  - `subscriptions.extra_promoter_pundit_user_id` — FK to users
    (nullable, SET NULL on delete). When set, the query-routing
    service prefers this user over the derived Priority-2 P-P
    (the sub's promoter if they're a P-P).
  - `subscription_promoter_pundit_changes` — permanent audit trail
    for every change to the pinned P-P. Append-only, mirrors the
    shape of subscription_alert_receiver_changes exactly.

Shape (locked 2026-11-14):
  - old_user_id: nullable (fallback chain was resolving)
  - new_user_id: nullable (RM cleared the pin)
  - changed_by_user_id: RESTRICT so the actor row is retained
  - source: 'FARMER_SUPPORT_RM' default; room for future actors
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'q9n4i2k85p1g'
down_revision: Union[str, Sequence[str], None] = 'p8m3h1j74o0f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column(
            'extra_promoter_pundit_user_id', sa.String(length=36),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )
    op.create_table(
        'subscription_promoter_pundit_changes',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column(
            'subscription_id', sa.String(length=36),
            sa.ForeignKey('subscriptions.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'old_user_id', sa.String(length=36),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'new_user_id', sa.String(length=36),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'changed_by_user_id', sa.String(length=36),
            sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'changed_at', sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text('now()'),
        ),
        sa.Column(
            'source', sa.String(length=40),
            nullable=False, server_default=sa.text("'FARMER_SUPPORT_RM'"),
        ),
    )
    op.create_index(
        'ix_sppc_sub_changed_at', 'subscription_promoter_pundit_changes',
        ['subscription_id', 'changed_at'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_sppc_sub_changed_at',
        table_name='subscription_promoter_pundit_changes',
    )
    op.drop_table('subscription_promoter_pundit_changes')
    op.drop_column('subscriptions', 'extra_promoter_pundit_user_id')
