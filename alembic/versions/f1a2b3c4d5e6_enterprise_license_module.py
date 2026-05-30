"""enterprise license module

Revision ID: f1a2b3c4d5e6
Revises: e7a3b8c9d1f2
Create Date: 2026-05-30 14:00:00.000000

EL module (2026-05-30) — SA-managed enterprise licences for clients
that prefer a flat-fee bulk arrangement (govt departments, large
co-ops, NGO partnerships). The schema additions:

  - `subscription_pools.note` — optional invoice / PO reference for
    the SA-grant path. Razorpay top-ups leave this NULL; SA invoice
    grants paste the invoice number / reference here.

  - `enterprise_licenses` — one row per granted licence. Carries the
    [from_date, to_date] window, a status (ACTIVE | EXPIRED | REVOKED),
    the SA user who granted it, and an optional note. Daily Celery
    task flips ACTIVE → EXPIRED on the to_date and the linked
    Client's status to INACTIVE.

Both additions are pure additions; rollback is a code-only revert.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e7a3b8c9d1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscription_pools",
        sa.Column("note", sa.Text(), nullable=True),
    )

    op.create_table(
        "enterprise_licenses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "client_id", sa.String(36),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        # ACTIVE / EXPIRED / REVOKED — String like the rest of the
        # status columns in this codebase to keep the cross-pipe
        # serialisation consistent.
        sa.Column(
            "status", sa.String(20),
            nullable=False, server_default="ACTIVE",
        ),
        sa.Column(
            "granted_by_user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    # Hot index for the daily Celery sweep: scan ACTIVE rows ordered
    # by to_date to find the ones due for reminder / closure.
    op.create_index(
        "ix_enterprise_licenses_status_to_date",
        "enterprise_licenses",
        ["status", "to_date"],
    )
    # Hot index for the "does this client have an active licence?"
    # read that lives on every consume / kitty check.
    op.create_index(
        "ix_enterprise_licenses_client_status",
        "enterprise_licenses",
        ["client_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_enterprise_licenses_client_status", "enterprise_licenses")
    op.drop_index("ix_enterprise_licenses_status_to_date", "enterprise_licenses")
    op.drop_table("enterprise_licenses")
    op.drop_column("subscription_pools", "note")
