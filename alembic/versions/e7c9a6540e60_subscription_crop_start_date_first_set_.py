"""subscription crop_start_date_first_set_at and alert extras

Revision ID: e7c9a6540e60
Revises: d2f471a09c83
Create Date: 2026-05-20 16:35:18.725030

Two new columns on `subscriptions`:

- `crop_start_date_first_set_at` — stamped on the first PUT to
  /start-date. Drives the 15-day edit window before the date locks.
- `extra_alert_phone` + `extra_alert_name` — single free-text alert
  recipient replacing the old multi-row FARMER+PROMOTER pattern.
  Farmer always gets push notifications regardless; this captures
  one optional extra (dealer / facilitator / anyone). For ASSIGNED
  subs, GET /alert-preferences falls back to the promoter's phone
  when this is NULL — no migration backfill needed.

Autogenerate picked up unrelated pre-existing drift (dropped
tables / indexes elsewhere); those have been stripped — this
migration only touches the three new columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7c9a6540e60'
down_revision: Union[str, Sequence[str], None] = 'd2f471a09c83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column('crop_start_date_first_set_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('extra_alert_phone', sa.String(length=20), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('extra_alert_name', sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('subscriptions', 'extra_alert_name')
    op.drop_column('subscriptions', 'extra_alert_phone')
    op.drop_column('subscriptions', 'crop_start_date_first_set_at')
