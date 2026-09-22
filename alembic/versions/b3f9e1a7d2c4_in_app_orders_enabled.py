"""in_app_orders_enabled on Client + Subscription

Revision ID: b3f9e1a7d2c4
Revises: a1e7f2b8c9d0
Create Date: 2026-09-22

Advisory-Only Mode v2 — Checkbox 3 "Enable in-app ordering".
When set on a client that also has advisory_only_mode=True, farmers
on that client get the Order button + Orders tile back (like Regular
Mode) while retaining the show-inputs-upfront advisory-only
experience. See project_rootstalk_advisory_only_v2_checkbox3_scoping.md.

Client (SA-editable, mutable):
- `in_app_orders_enabled` (Boolean, nullable) — third flag under the
  advisory-only section. NULL / False → v1 behaviour (no in-app
  orders). True → hybrid mode. Meaningful only when
  `advisory_only_mode=True`; SA-portal UI greys it out otherwise.

Subscription (snapshot at create, immutable):
- `in_app_orders_enabled` — captured from Client's value at subscribe
  time so flipping the client-level value later doesn't shift
  existing subs' order-eligibility.

Backfill: NULL on both — code default is False. Regular Mode subs
(advisory_only_mode=False) are unaffected regardless of column value
because every guard is gated on `advisory_only_mode AND NOT
in_app_orders_enabled` — Regular flows always fail the outer test.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3f9e1a7d2c4'
down_revision: Union[str, Sequence[str], None] = 'a1e7f2b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('clients', sa.Column(
        'in_app_orders_enabled', sa.Boolean(), nullable=True,
    ))
    op.add_column('subscriptions', sa.Column(
        'in_app_orders_enabled', sa.Boolean(), nullable=True,
    ))


def downgrade() -> None:
    op.drop_column('subscriptions', 'in_app_orders_enabled')
    op.drop_column('clients', 'in_app_orders_enabled')
