"""Order management V2 — dealer draft JSON column

Revision ID: a4c7e2f81b65
Revises: f1c4d7b25e88
Create Date: 2026-05-31

Batch 28 of the Order Management V2 redesign.

Adds `orders.dealer_draft` JSONB — per-item in-flight edits the
dealer's app debounce-syncs every few seconds so a network drop,
device reload, or screen change can't lose partial work.

Shape (server-authoritative): `{ <item_id>: {brand_cosh_id,
brand_name, given_volume, volume_unit, price} }`. Entries are
removed by the server when the corresponding item moves to
AVAILABLE (i.e. the dealer hits "Mark Available"); the PWA also
mirrors the same map into IndexedDB so the dealer can resume
from offline.

Default `{}` so existing rows are immediately addressable as a
dict without NULL checks throughout the read path.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "a4c7e2f81b65"
down_revision = "f1c4d7b25e88"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "dealer_draft",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("orders", "dealer_draft")
