"""alert receiver change log — Farmer Support v2 mutation #1

Revision ID: p8m3h1j74o0f
Revises: o7l2g0i63n9e
Create Date: 2026-11-14

New table `subscription_alert_receiver_changes` — permanent audit
trail for every RM-initiated change to a subscription's alert
receiver. Captures old → new user_ids, the RM who made the change,
and a timestamp. Permanent-by-design (same pattern as
farmer_call_logs); no edit, no delete endpoint will be added.

Shape (locked 2026-11-14):
  - old_user_id: nullable (was NULL when the fallback chain was
    resolving to a default like the sub's promoter)
  - new_user_id: nullable (the RM cleared the override — restoring
    the fallback chain)
  - changed_by_user_id: the RM/CM/SA who made the change (FK RESTRICT
    — never delete the person who made the change)
  - source: FARMER_SUPPORT_RM default, leaves room for future paths
    (e.g. mobile RM, bulk reassignment) without a column migration

Index: (subscription_id, changed_at) covers the only read pattern
— a per-sub history list, newest first.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'p8m3h1j74o0f'
down_revision: Union[str, Sequence[str], None] = 'o7l2g0i63n9e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'subscription_alert_receiver_changes',
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
        'ix_sarc_sub_changed_at', 'subscription_alert_receiver_changes',
        ['subscription_id', 'changed_at'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_sarc_sub_changed_at',
        table_name='subscription_alert_receiver_changes',
    )
    op.drop_table('subscription_alert_receiver_changes')
