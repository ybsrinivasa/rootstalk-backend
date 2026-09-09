"""user_self_registered_at

Revision ID: c9f2b4e8d371
Revises: f3d8b1c4e5a7
Create Date: 2026-09-09

Adds `User.self_registered_at`, the definitive "user has proved
ownership of this phone via OTP" timestamp. Set exactly once, in
the OTP verify-and-login flow — either when the flow creates a
new user, or when it finds a pre-existing user (typically a
dealer-created ledger row) that has NULL here and needs to be
"claimed".

Why not password_hash / current_session_id?
- password_hash is only set by explicit password reset. RootsTalk
  is OTP-only, so real registered users have NULL here.
- current_session_id is cleared on logout, so it goes back to
  NULL for real registered users who happen to be signed out.
- self_registered_at is set once and never cleared.

Backfill: every EXISTING User row is treated as self-registered
(set to created_at). This is safe because the Farmer Ledger's
manual-entry table (dealer_manual_sale) launched empty — no
dealer-created unclaimed users exist yet at migration time.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9f2b4e8d371'
down_revision: Union[str, Sequence[str], None] = 'f3d8b1c4e5a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('self_registered_at', sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill: every existing user is treated as PWA-registered.
    op.execute("UPDATE users SET self_registered_at = created_at WHERE self_registered_at IS NULL")


def downgrade() -> None:
    op.drop_column('users', 'self_registered_at')
