"""ClientUser.deactivated_at — timestamp for CA-history sorting.

Revision ID: e8f4d21a9c33
Revises: d0e4a51b9c72
Create Date: 2026-07-07

Adds `deactivated_at` to `client_users` so the SA-facing per-client CA
table can render history chronologically (most-recent-first, current
active CA on top).

Nullable — populated only when a row flips from ACTIVE→INACTIVE via
`rotate_ca_admin` or the new activate/deactivate CA endpoints. NULL
means either "never deactivated" (current active row) or "row was
INACTIVE before this migration and we don't know the exact time".

Backfill: existing INACTIVE rows get their `created_at` stamped in
so ordering is at least stable (creation order == fairly close to
deactivation order for the CA-only slice, since past CAs were
demoted-on-replacement not archived long after creation). Not a
perfect proxy but good enough for the display shape; the SA can
still eyeball the User's email + name to identify each row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8f4d21a9c33"
down_revision: Union[str, Sequence[str], None] = "d0e4a51b9c72"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "client_users",
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE client_users SET deactivated_at = created_at "
        "WHERE status = 'INACTIVE' AND deactivated_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("client_users", "deactivated_at")
