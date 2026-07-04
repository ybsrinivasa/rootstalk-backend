"""Client.hidden_from_discovery — SA-only flag to hide COMPANY_PAYS clients from farmer discovery.

Revision ID: e3b6f28c1a45
Revises: d7f4c8b2e919
Create Date: 2026-07-04

Background:
Testorg was created on prod as an internal / testing / demo client
using the COMPANY_PAYS model. It naturally doesn't leak into
`/farmer/discover/crops` or `/farmer/discover/companies` (those
already filter to FARMER_PAYS), but IT DOES show up in the Crops &
Companies drawer via `/farmer/discover/crops-and-companies` — a
2026-06-22 change deliberately widened that endpoint to surface
COMPANY_PAYS clients too so farmers know which company advisories
operate in their district.

This flag gives the SA an opt-out for internal / demo / training
clients that should never appear on any farmer surface. Backend
guard: settable only on COMPANY_PAYS clients (a FARMER_PAYS client
must remain discoverable by definition; hiding one would create a
farmer with no path to subscribe).

Column is nullable=False, server_default=false so existing rows land
as `False` (visible — the historic default). No backfill needed.
Safe to roll back code-only without dropping the column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3b6f28c1a45"
down_revision: Union[str, Sequence[str], None] = "d7f4c8b2e919"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column(
            "hidden_from_discovery",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("clients", "hidden_from_discovery")
