"""Training Sandbox — shadow client under each real Client.

Revision ID: c4b9e18f5d20
Revises: f2a91e83b4c7
Create Date: 2026-07-24

Powers the Training Sandbox V1 feature (per user 2026-07-24 build
plan). Each real Client can spin up ONE training child at a time via
`POST /client/{cid}/training/start`. The child is a first-class
`clients` row with `is_training=True`, `parent_client_id=<real>`,
and a 12-day `training_ends_at` clock. After the clock expires the
child transitions ACTIVE → WINDING_DOWN (24h grace to finish
in-flight orders/queries) → hard-cascade-delete by the hourly
`training_expiry` celery task.

Design decisions locked with user on the day this migration lands:
- One active training per parent enforced at the DB via a partial
  unique index (belt to the app-level check). A second start-request
  raises IntegrityError → app returns 409.
- Four training fields must be either all-set (training child) or
  all-null (real client) — enforced by a CHECK. Prevents a real
  client from silently gaining a training_ends_at orphan.
- `v_real_clients` VIEW filters out training clients. Any read path
  that shouldn't leak training data can just query the view instead
  of remembering the WHERE clause. Defensive floor.
- FK on parent_client_id uses RESTRICT so a real client can't be
  deleted while a training child references it. Training children
  never have grandchildren (no training-of-training), so a training
  row can be deleted freely.

All additive nullable + one new view; code-only rollback safe if
the training endpoints are also stripped.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4b9e18f5d20"
down_revision: Union[str, Sequence[str], None] = "f2a91e83b4c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── New columns on `clients` ──────────────────────────────────────────
    op.add_column(
        "clients",
        sa.Column(
            "is_training", sa.Boolean(),
            server_default=sa.text("false"), nullable=False,
        ),
    )
    op.add_column(
        "clients",
        sa.Column("parent_client_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column(
            "training_started_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.add_column(
        "clients",
        sa.Column(
            "training_ends_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.add_column(
        "clients",
        # 'ACTIVE' during the 12 days, 'WINDING_DOWN' for the 24h grace
        # after training_ends_at, then the row is hard-deleted.
        sa.Column("training_status", sa.String(length=20), nullable=True),
    )

    # ── FK back to parent client (RESTRICT prevents real-client
    # deletion while a training child still references it). ──────────────
    op.create_foreign_key(
        "fk_clients_parent_client_id",
        "clients", "clients",
        ["parent_client_id"], ["id"],
        ondelete="RESTRICT",
    )

    # ── Shape-invariant CHECK. Four training fields are either all
    # set (a training child) or all null (a real client). Rejects a
    # real client that somehow got a training_ends_at orphan. ────────────
    op.create_check_constraint(
        "chk_training_client_shape",
        "clients",
        "(is_training = false "
        "  AND parent_client_id IS NULL "
        "  AND training_started_at IS NULL "
        "  AND training_ends_at IS NULL "
        "  AND training_status IS NULL) "
        "OR "
        "(is_training = true "
        "  AND parent_client_id IS NOT NULL "
        "  AND training_started_at IS NOT NULL "
        "  AND training_ends_at IS NOT NULL "
        "  AND training_status IS NOT NULL)",
    )

    # ── One active training per parent, enforced at the DB. The
    # partial index only covers ACTIVE / WINDING_DOWN rows, so a
    # cascade-deleted parent can start a fresh session immediately. ──────
    op.create_index(
        "uq_one_active_training_per_parent",
        "clients", ["parent_client_id"],
        unique=True,
        postgresql_where=sa.text(
            "is_training = true "
            "AND training_status IN ('ACTIVE', 'WINDING_DOWN')"
        ),
    )

    # ── Lookup index for the hourly expiry sweep. ────────────────────────
    op.create_index(
        "ix_clients_training_status",
        "clients", ["training_status"],
        postgresql_where=sa.text("training_status IS NOT NULL"),
    )

    # ── Defensive read-side view. Any surface that shouldn't leak
    # training data can query this instead of remembering the WHERE.
    # `v_real_clients` returns the same columns as `clients` so it
    # slots in wherever the raw table is queried today. ──────────────────
    op.execute(
        "CREATE VIEW v_real_clients AS "
        "SELECT * FROM clients WHERE is_training = false"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_real_clients")
    op.drop_index("ix_clients_training_status", table_name="clients")
    op.drop_index("uq_one_active_training_per_parent", table_name="clients")
    op.drop_constraint("chk_training_client_shape", "clients", type_="check")
    op.drop_constraint("fk_clients_parent_client_id", "clients", type_="foreignkey")
    op.drop_column("clients", "training_status")
    op.drop_column("clients", "training_ends_at")
    op.drop_column("clients", "training_started_at")
    op.drop_column("clients", "parent_client_id")
    op.drop_column("clients", "is_training")
