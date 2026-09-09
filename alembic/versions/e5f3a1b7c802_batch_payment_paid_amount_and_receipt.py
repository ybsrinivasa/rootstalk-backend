"""batch_payment_paid_amount_and_receipt

Revision ID: e5f3a1b7c802
Revises: b7a3f2c8e094
Create Date: 2026-09-09

UPI payment v1.1 simplification (user 2026-09-09): payment surface
restricted to the Pickup pill only. Farmer confirms after paying
via their UPI app by entering the ACTUAL amount they paid + the
transaction reference (now required) + an optional screenshot of
the payment confirmation.

Two new columns on `batch_payments`:
- `paid_amount` DECIMAL(12,2) — what the farmer actually paid.
  Distinct from `amount` (the quoted batch total) because the
  farmer may pay a different sum (rounded, discount at counter,
  advance / partial, etc.). Nullable — set on FARMER_MARKED_PAID.
- `screenshot_url` TEXT — optional S3 URL of the payment
  confirmation the farmer uploads. Nullable.

No backfill — existing rows keep NULL for both. Old flow (which
only stored `amount` + optional `txn_ref`) is compatible with
the new schema.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f3a1b7c802'
down_revision: Union[str, Sequence[str], None] = 'b7a3f2c8e094'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'batch_payments',
        sa.Column('paid_amount', sa.DECIMAL(12, 2), nullable=True),
    )
    op.add_column(
        'batch_payments',
        sa.Column('screenshot_url', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('batch_payments', 'screenshot_url')
    op.drop_column('batch_payments', 'paid_amount')
