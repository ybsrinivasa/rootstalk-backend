"""QRScan.seed_order_id + SeedOrderFull.scan_verified — seed scan parity.

Revision ID: b7e2f931d54c
Revises: a4c8e2f6d1b3
Create Date: 2026-07-05

Background:
The QR Product Authentication module currently only supports the
pesticide/fertilizer scan path (QRScan.order_item_id → OrderItem).
Seed orders live in a different table (`seed_orders_full`) with
`variety_id` as the match key instead of `brand_cosh_id`.

Changes:
1. `qr_scans.order_item_id` becomes nullable so a scan row can
   attach to a seed order instead.
2. Add `qr_scans.seed_order_id` — nullable FK to seed_orders_full.
3. Add `seed_orders_full.scan_verified` — boolean, defaults False,
   parity with `order_items.scan_verified`. The farmer PWA lights
   up a "✓ Verified" chip on the seed order card once a matching
   scan lands.

App-level XOR guard on the scan endpoint enforces exactly one of
{order_item_id, seed_order_id} is set per scan row — a DB check
constraint was considered but skipped so existing rows (all with
order_item_id populated) don't need backfill semantics.

Non-destructive additive migration — safe to roll back at the code
level without downgrading the DB. Existing scan rows keep working
because order_item_id remains populated on all of them.
"""

from alembic import op
import sqlalchemy as sa


revision = "b7e2f931d54c"
down_revision = "a4c8e2f6d1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("qr_scans", "order_item_id", nullable=True)
    op.add_column(
        "qr_scans",
        sa.Column("seed_order_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_qr_scans_seed_order_id",
        "qr_scans", "seed_orders_full",
        ["seed_order_id"], ["id"],
    )
    op.add_column(
        "seed_orders_full",
        sa.Column(
            "scan_verified", sa.Boolean(),
            nullable=False, server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("seed_orders_full", "scan_verified")
    op.drop_constraint("fk_qr_scans_seed_order_id", "qr_scans", type_="foreignkey")
    op.drop_column("qr_scans", "seed_order_id")
    op.alter_column("qr_scans", "order_item_id", nullable=False)
