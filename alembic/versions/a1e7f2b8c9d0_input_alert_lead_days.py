"""input_alert_lead_days on Client + Subscription

Revision ID: a1e7f2b8c9d0
Revises: f3d5c2a8b1e6
Create Date: 2026-09-18

Advisory-Only Mode v1.13 — pre-window INPUT alerts. Fires the daily
INPUT alert `lead_days` days BEFORE the practice's authored window
opens, so an advisory-only farmer buying inputs offline has travel /
shop-hours lead time. Regular Mode subs still fire on window-open day
(lead_days=0).

Client (SA-editable, mutable):
- `input_alert_lead_days` (int, nullable) — days-before-window to fire
  the INPUT alert. NULL → code default 2; explicit 0 → no pre-alert
  (fire on window-open day only, matching Regular Mode). Meaningful
  only for advisory-only clients; SA-portal shows the field inside
  the advisory-only sub-section.

Subscription (snapshot at create, immutable):
- `input_alert_lead_days` — captured from Client's value at subscribe
  time so existing subs keep their original policy when the SA flips
  the client-level value.

Backfill: NULL on both — code default 2 kicks in for existing
advisory-only subs, and the alerts engine only applies the pre-window
shift when `sub.advisory_only_mode` is True, so Regular subs are
byte-identical regardless of the column value.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1e7f2b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'f3d5c2a8b1e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('clients', sa.Column(
        'input_alert_lead_days', sa.Integer(), nullable=True,
    ))
    op.add_column('subscriptions', sa.Column(
        'input_alert_lead_days', sa.Integer(), nullable=True,
    ))


def downgrade() -> None:
    op.drop_column('subscriptions', 'input_alert_lead_days')
    op.drop_column('clients', 'input_alert_lead_days')
