"""advisory_only_mode

Revision ID: b7f4e8c12a03
Revises: a4c7e2b91d68
Create Date: 2026-09-16

Advisory-Only Mode v1 — supports university-type clients who publish
crop advisories but don't run a dealer network. See
docs/AdvisoryOnly_v1_scoping.md for the full design.

Adds three columns each on `clients` and `subscriptions`:

Client (SA-editable, mutable):
- `advisory_only_mode` (bool, default False) — main mode toggle.
- `dealer_list_enabled` (bool, default False) — opt-in add-on; meaningful
  only when advisory_only_mode is True. SA-portal greys it out otherwise.
- `subscription_fee_paise` (int, nullable) — flat-fee override. NULL keeps
  existing bulk-discount pricing logic. Non-null triggers
  `qty × subscription_fee_paise` on both FARMER_PAYS and COMPANY_PAYS
  top-up flows. Defaults to 9900 in the SA-portal UI when advisory_only
  is ticked; editable per client.

Subscription (snapshot at create, immutable):
- `advisory_only_mode` — captured from Client's value at subscribe time.
- `dealer_list_enabled` — same.
- `subscription_fee_paise` — same. NULL = traditional bulk-discount
  pricing was used; non-null = flat pricing snapshot.

Backfill: existing rows → False / NULL, i.e. zero functional change.
All read paths default to the existing behaviour for legacy subs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7f4e8c12a03'
down_revision: Union[str, Sequence[str], None] = 'a4c7e2b91d68'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Client
    op.add_column('clients', sa.Column(
        'advisory_only_mode', sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.add_column('clients', sa.Column(
        'dealer_list_enabled', sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.add_column('clients', sa.Column(
        'subscription_fee_paise', sa.Integer(), nullable=True,
    ))

    # Subscription (snapshot columns)
    op.add_column('subscriptions', sa.Column(
        'advisory_only_mode', sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.add_column('subscriptions', sa.Column(
        'dealer_list_enabled', sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.add_column('subscriptions', sa.Column(
        'subscription_fee_paise', sa.Integer(), nullable=True,
    ))


def downgrade() -> None:
    op.drop_column('subscriptions', 'subscription_fee_paise')
    op.drop_column('subscriptions', 'dealer_list_enabled')
    op.drop_column('subscriptions', 'advisory_only_mode')
    op.drop_column('clients', 'subscription_fee_paise')
    op.drop_column('clients', 'dealer_list_enabled')
    op.drop_column('clients', 'advisory_only_mode')
