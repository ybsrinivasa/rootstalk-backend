"""Order management V2 — schema foundation

Revision ID: b7e2c4f81a93
Revises: c8f5a3d9b7e1
Create Date: 2026-05-31

Batch 1 of the Order Management V2 redesign.

Three pieces, no behaviour changes — purely additive schema work
that the subsequent batches build on:

1. **`orders.category`** — hard PESTICIDE / FERTILIZER label on the
   Order row itself. Until now, category was inferred each time from
   the items' practices. Making it discrete:
     - lets the locked-brand + dealer-licence gates be checked
       trivially at send time
     - prevents mixed-category orders by construction
   Nullable so existing rows survive; backfilled below.

2. **`order_items.lineage_id` + `seed_orders.lineage_id`** — the
   journey identifier for "this physical item as it travels through
   dealers". When the farmer cancels and the items are migrated to
   a fresh DRAFT, the new OrderItem row inherits the same
   `lineage_id`. Reports group by lineage to reconstruct
   "Sanjay's pesticide P1 went to D1 → returned → went to D2 → bought".

3. **`order_item_events`** — append-only event log for the V2
   audit trail. Every status flip, every re-route, every postpone
   sets a row. Required by clients for fulfilment reporting.

Backfill strategy:
- `orders.category`: derived from the first item's `practice.l1_type`
  using the same PESTICIDE / FERTILIZER mapping the application uses.
  Orders with no resolvable items keep NULL; they're test-data
  remnants and the next deploy of orders/router.py won't accept
  category-less inputs.
- `order_items.lineage_id`, `seed_orders.lineage_id`: each existing
  row is its own lineage root → `lineage_id = id`.
"""
from alembic import op
import sqlalchemy as sa


revision = "b7e2c4f81a93"
down_revision = "c8f5a3d9b7e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ────────────────────────────────────────────────────────────────
    # 1. orders.category
    # ────────────────────────────────────────────────────────────────
    op.add_column(
        "orders",
        sa.Column("category", sa.String(20), nullable=True),
    )

    # Backfill: derive each existing order's category from its first
    # OrderItem's Practice.l1_type. L1 values that map to PESTICIDE
    # are {PESTICIDE, SPECIAL_INPUT}; L1 == FERTILIZER → FERTILIZER.
    # The router already encodes this mapping; we mirror it here in
    # raw SQL to avoid importing application code into the migration.
    op.execute("""
        UPDATE orders o
        SET category = CASE
            WHEN p.l1_type IN ('PESTICIDE', 'SPECIAL_INPUT') THEN 'PESTICIDE'
            WHEN p.l1_type = 'FERTILIZER' THEN 'FERTILIZER'
            ELSE NULL
        END
        FROM (
            SELECT DISTINCT ON (oi.order_id) oi.order_id, pr.l1_type
            FROM order_items oi
            JOIN practices pr ON pr.id = oi.practice_id
            ORDER BY oi.order_id, oi.created_at
        ) AS p
        WHERE p.order_id = o.id
    """)

    op.create_index(
        "ix_orders_category",
        "orders",
        ["category"],
    )

    # ────────────────────────────────────────────────────────────────
    # 2. lineage_id on the two item-bearing tables
    # ────────────────────────────────────────────────────────────────
    op.add_column(
        "order_items",
        sa.Column("lineage_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "seed_orders",
        sa.Column("lineage_id", sa.String(36), nullable=True),
    )

    # Backfill: each existing row is its own lineage root.
    op.execute("UPDATE order_items SET lineage_id = id WHERE lineage_id IS NULL")
    op.execute("UPDATE seed_orders SET lineage_id = id WHERE lineage_id IS NULL")

    # Now safely set NOT NULL — all rows have a value.
    op.alter_column("order_items", "lineage_id", nullable=False)
    op.alter_column("seed_orders", "lineage_id", nullable=False)

    op.create_index("ix_order_items_lineage", "order_items", ["lineage_id"])
    op.create_index("ix_seed_orders_lineage", "seed_orders", ["lineage_id"])

    # ────────────────────────────────────────────────────────────────
    # 3. order_item_events — the audit trail
    # ────────────────────────────────────────────────────────────────
    op.create_table(
        "order_item_events",
        sa.Column("id", sa.String(36), primary_key=True),
        # The journey this event belongs to. Indexed because the
        # "tell me the full story of item X" query is the primary
        # access pattern for reports.
        sa.Column("lineage_id", sa.String(36), nullable=False),
        # Where the event happened. One of these is set; nullable
        # because some events (e.g. CANCELLED_BY_FARMER on an empty
        # husk order) don't point at an item row.
        sa.Column("order_item_id", sa.String(36), nullable=True),
        sa.Column("seed_order_id", sa.String(36), nullable=True),
        sa.Column("order_id", sa.String(36), nullable=True),
        sa.Column("actor_user_id", sa.String(36), nullable=True),
        # FARMER / DEALER / FACILITATOR / SYSTEM. String not enum
        # because we want to extend without migrations.
        sa.Column("actor_role", sa.String(20), nullable=True),
        # CREATED, SENT, ACCEPTED, MARKED_AVAILABLE,
        # MARKED_POSTPONED, MARKED_NOT_AVAILABLE, POSTPONE_EXPIRED,
        # REROUTED_FROM, REROUTED_TO, CANCELLED_BY_FARMER,
        # TIMELINE_EXPIRED, PURCHASE_RECORDED, etc.
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("prev_status", sa.String(30), nullable=True),
        sa.Column("new_status", sa.String(30), nullable=True),
        # JSON payload for event-specific context: postpone days,
        # postponed_until, brand chosen, price, dealer presence at
        # cancel-attempt time, etc. SQLAlchemy reserves `metadata`
        # on Base, so we name the column `event_metadata`.
        sa.Column("event_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["seed_order_id"], ["seed_orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
    )

    op.create_index("ix_order_item_events_lineage", "order_item_events", ["lineage_id"])
    op.create_index("ix_order_item_events_created", "order_item_events", ["created_at"])
    op.create_index("ix_order_item_events_order", "order_item_events", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_order_item_events_order", table_name="order_item_events")
    op.drop_index("ix_order_item_events_created", table_name="order_item_events")
    op.drop_index("ix_order_item_events_lineage", table_name="order_item_events")
    op.drop_table("order_item_events")

    op.drop_index("ix_seed_orders_lineage", table_name="seed_orders")
    op.drop_index("ix_order_items_lineage", table_name="order_items")
    op.drop_column("seed_orders", "lineage_id")
    op.drop_column("order_items", "lineage_id")

    op.drop_index("ix_orders_category", table_name="orders")
    op.drop_column("orders", "category")
