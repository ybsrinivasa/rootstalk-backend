"""subscription deleted_by_user_id — Training per-sub removal

Revision ID: s1p6k4m07r3i
Revises: r0o5j3l96q2h
Create Date: 2026-11-15

Adds `subscriptions.deleted_by_user_id` (nullable FK to users, SET
NULL on delete) — set by the Training per-sub soft-delete flow so
later audits can ask "which actor removed this training sub".

Nullable + no backfill: existing soft-deleted rows (from the CA
admin cleanup path) stay with NULL here. Only the new Training
remove-sub endpoints populate this column.

No companion table — per user call 2026-11-15, Training removals
rely on (a) Subscription.deleted_at + deleted_by_user_id and (b)
PromoterAssignment.status flipped to REMOVED_BY_PROMOTER /
REMOVED_BY_FARMER (Python-enum only; column is String(30), no
PG enum migration needed).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 's1p6k4m07r3i'
down_revision: Union[str, Sequence[str], None] = 'r0o5j3l96q2h'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 2026-11-15 — the SQLAlchemy model has carried this column for
    # a while and SOME environments picked it up via create_all /
    # out-of-band schema push (confirmed staging already had it
    # before this migration). Guard so a re-apply is a no-op.
    bind = op.get_bind()
    exists = bind.execute(sa.text("""
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = 'subscriptions'
           AND column_name = 'deleted_by_user_id'
    """)).first()
    if exists:
        return
    op.add_column(
        'subscriptions',
        sa.Column(
            'deleted_by_user_id', sa.String(length=36),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(sa.text("""
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = 'subscriptions'
           AND column_name = 'deleted_by_user_id'
    """)).first()
    if exists:
        op.drop_column('subscriptions', 'deleted_by_user_id')
