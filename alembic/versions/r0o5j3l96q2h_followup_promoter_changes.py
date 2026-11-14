"""follow-up promoter change log — Farmer Support v2 #3

Revision ID: r0o5j3l96q2h
Revises: q9n4i2k85p1g
Create Date: 2026-11-14

New table `subscription_followup_promoter_changes` — permanent
audit trail for every RM-initiated change to a SELF-subscribed
subscription's follow-up promoter. Same shape as
subscription_alert_receiver_changes and subscription_promoter_
pundit_changes. Append-only — no edit, no delete endpoint will
be added.

No new column on `subscriptions` — the RM-delegated facilitator
is stored in the existing `Subscription.promoter_user_id`
(reused; distinction from "signup promoter" is encoded in
`subscription_type = SELF`). The AssignmentStatus Python enum
gains a `REVOKED_BY_RM` value via a Python-only change (the
column is String, not PG enum, so no DB migration needed).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'r0o5j3l96q2h'
down_revision: Union[str, Sequence[str], None] = 'q9n4i2k85p1g'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'subscription_followup_promoter_changes',
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
        'ix_sfpc_sub_changed_at', 'subscription_followup_promoter_changes',
        ['subscription_id', 'changed_at'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_sfpc_sub_changed_at',
        table_name='subscription_followup_promoter_changes',
    )
    op.drop_table('subscription_followup_promoter_changes')
