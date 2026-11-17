"""Subscription.signup_promoter_user_id — immutable signup attribution.

Revision ID: v4s9n7p30u6l
Revises: u3r8m6o29t5k
Create Date: 2026-11-17

Lets the RM assign a Follow-up Promoter on ASSIGNED subs (e.g. when
the signup promoter leaves the organisation) WITHOUT losing the
original promoter's attribution. `promoter_user_id` becomes the
current effective promoter (mutable, written by the follow-up
mutation); the new `signup_promoter_user_id` snapshots the original
at sub creation and never mutates after.

- Column: nullable String(36), FK → users.id, ON DELETE SET NULL.
  NULL on SELF subs (there was no signup promoter) and on legacy
  rows the backfill can't resolve (unlikely).
- Backfill: for every ASSIGNED sub with promoter_user_id set,
  copy that value into signup_promoter_user_id. This preserves
  attribution for all existing rows before any follow-up mutation
  can touch promoter_user_id.
- Idempotent via information_schema guard — safe to re-run on
  envs that already have the column via create_all drift.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "v4s9n7p30u6l"
down_revision: Union[str, Sequence[str], None] = "u3r8m6o29t5k"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'subscriptions'
          AND column_name = 'signup_promoter_user_id'
    """)).first()
    if not exists:
        op.add_column(
            "subscriptions",
            sa.Column(
                "signup_promoter_user_id", sa.String(length=36), nullable=True,
            ),
        )
        op.create_foreign_key(
            "fk_subscriptions_signup_promoter_user_id",
            "subscriptions", "users",
            ["signup_promoter_user_id"], ["id"],
            ondelete="SET NULL",
        )

    # Backfill — for every ASSIGNED sub, the signup promoter IS the
    # current promoter_user_id (nothing has mutated it yet because
    # the follow-up mutation was SELF-only before this migration).
    # Only touches rows where signup is still NULL so re-runs are
    # idempotent + non-destructive.
    bind.execute(sa.text("""
        UPDATE subscriptions
        SET signup_promoter_user_id = promoter_user_id
        WHERE subscription_type = 'ASSIGNED'
          AND promoter_user_id IS NOT NULL
          AND signup_promoter_user_id IS NULL
    """))


def downgrade() -> None:
    op.drop_constraint(
        "fk_subscriptions_signup_promoter_user_id",
        "subscriptions",
        type_="foreignkey",
    )
    op.drop_column("subscriptions", "signup_promoter_user_id")
