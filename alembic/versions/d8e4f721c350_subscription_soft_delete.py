"""Subscription soft delete — deleted_at + deleted_by_user_id.

Revision ID: d8e4f721c350
Revises: b8e4a72f3019
Create Date: 2026-06-28

Background:
CA Admins need the ability to clear practice / test subscriptions
created during client training (clients start training over the next
5-7 days from 2026-06-27). Decision locked: **soft delete only** — no
scheduled purge for now. Hide everywhere on the read side; physical
rows stay in place. Only soft-delete the SUBSCRIPTION row; the User
account is platform-wide and stays untouched.

Schema changes:
- `subscriptions.deleted_at` (nullable TIMESTAMP WITH TIME ZONE) —
  NULL = active subscription; non-NULL = soft-deleted at that time.
- `subscriptions.deleted_by_user_id` (nullable FK to users.id) —
  audit pointer to the CA Admin who triggered the soft delete.
- Partial index on `deleted_at IS NULL` to keep the live-subscription
  hot path fast (most queries filter on it).

Read-path strategy (see app/modules/subscriptions/soft_delete.py):
A SQLAlchemy session-level event listener automatically appends
`deleted_at IS NULL` to every SELECT on the Subscription mapper,
with an opt-out via `execution_options(include_deleted=True)` for
the admin cleanup endpoint. Cascade tables (orders, queries, etc.)
don't need their own deleted_at column — they reference
subscription_id, and the listener filters those too where joins
exist. Hot read paths that filter cascade tables by subscription_id
directly (without joining Subscription) need an explicit filter.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8e4f721c350"
down_revision: Union[str, Sequence[str], None] = "b8e4a72f3019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "deleted_by_user_id",
            sa.String(36),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_subscriptions_deleted_by_user_id",
        "subscriptions",
        "users",
        ["deleted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Partial index on the hot path: every live-subscription query
    # filters `deleted_at IS NULL`. The partial index keeps the live
    # row count in a compact b-tree even as soft-deleted rows
    # accumulate over time.
    op.create_index(
        "ix_subscriptions_alive",
        "subscriptions",
        ["client_id", "farmer_user_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_alive", table_name="subscriptions")
    op.drop_constraint(
        "fk_subscriptions_deleted_by_user_id",
        "subscriptions",
        type_="foreignkey",
    )
    op.drop_column("subscriptions", "deleted_by_user_id")
    op.drop_column("subscriptions", "deleted_at")
