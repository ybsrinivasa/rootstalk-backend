"""Training Sandbox — per-crop expiry (drops session-level timer).

Revision ID: t2q7l5n18s4j
Revises: s1p6k4m07r3i
Create Date: 2026-11-16

The 12-day expiry clock moves from the Client (session) level to the
Subscription (per training crop) level. The CA's Training Sandbox now
only exposes Start / Close — no session timer. Each training sub picks
up an `auto_remove_at` = farmer-accept-time + 12d at the moment the
farmer accepts the promoter's invite; the hourly sweep soft-removes
subs whose clock has expired (same soft-remove the per-sub Promoter /
Farmer Remove-training-crop buttons already use).

Schema changes:
  - Add `subscriptions.auto_remove_at` (nullable DATETIME).
  - Partial index on `auto_remove_at` for the sweep.
  - Backfill `auto_remove_at` for existing ACTIVE training subs from
    the paired PromoterAssignment.farmer_responded_at (+ 12 days).
  - Drop `clients.training_ends_at` + `clients.training_status`.
  - Reshape `chk_client_shape` CHECK + rebuild partial unique index
    on `parent_client_id` (training children are now uniquely scoped
    by parent + is_training + exists).

Session-level `training_started_at` stays (CA UI shows start date);
`is_training` + `parent_client_id` + `training_expert_user_id` +
`training_dealer_user_id` all stay unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "t2q7l5n18s4j"
down_revision: Union[str, Sequence[str], None] = "s1p6k4m07r3i"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TRAINING_DAYS = 12


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1) Add Subscription.auto_remove_at (idempotent) ──────────────
    exists = bind.execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'subscriptions' AND column_name = 'auto_remove_at'
    """)).first()
    if not exists:
        op.add_column(
            "subscriptions",
            sa.Column("auto_remove_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Partial index for the hourly sweep. Rows with NULL auto_remove_at
    # (regular subs + pending training subs) are excluded.
    bind.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_subs_auto_remove_at
        ON subscriptions (auto_remove_at)
        WHERE auto_remove_at IS NOT NULL AND deleted_at IS NULL
    """))

    # ── 2) Backfill auto_remove_at for existing ACTIVE training subs ─
    # Driven by the paired PromoterAssignment.farmer_responded_at. Only
    # non-deleted subs under a training client that currently have NULL
    # auto_remove_at get touched.
    bind.execute(sa.text(f"""
        UPDATE subscriptions s
        SET auto_remove_at = pa.farmer_responded_at + INTERVAL '{TRAINING_DAYS} days'
        FROM promoter_assignments pa, clients c
        WHERE pa.subscription_id = s.id
          AND c.id = s.client_id
          AND c.is_training = true
          AND pa.farmer_responded_at IS NOT NULL
          AND s.deleted_at IS NULL
          AND s.auto_remove_at IS NULL
    """))

    # ── 3) Drop the training_status index + partial unique index ─────
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_clients_training_status"))
    bind.execute(sa.text(
        "DROP INDEX IF EXISTS uq_one_active_training_per_parent"
    ))

    # ── 4) Reshape chk_client_shape (drops refs to the two columns) ──
    # Shape is now:
    #   Real client:    is_training=false, is_coaching=false, parents NULL
    #   Training child: is_training=true,  is_coaching=false, parent_client_id set
    #   Coaching w/s:   is_training=false, is_coaching=true,  parent_client_id + parent_session_id set
    op.drop_constraint("chk_client_shape", "clients", type_="check")
    op.create_check_constraint(
        "chk_client_shape",
        "clients",
        "(is_training = false AND is_coaching = false "
        "  AND parent_client_id IS NULL "
        "  AND parent_session_id IS NULL) "
        "OR "
        "(is_training = true AND is_coaching = false "
        "  AND parent_client_id IS NOT NULL "
        "  AND training_started_at IS NOT NULL "
        "  AND parent_session_id IS NULL) "
        "OR "
        "(is_training = false AND is_coaching = true "
        "  AND parent_client_id IS NOT NULL "
        "  AND parent_session_id IS NOT NULL)",
    )

    # ── 5) Drop the two training-session columns ─────────────────────
    # The `v_real_clients` view does SELECT *, which pins the schema;
    # Postgres refuses to drop columns referenced by it. Rebuild the
    # view around the drop so it picks up the new column list.
    bind.execute(sa.text("DROP VIEW IF EXISTS v_real_clients"))

    # Model columns remain in SQLAlchemy drift via `create_all` on some
    # envs, so keep the drop idempotent.
    for col in ("training_status", "training_ends_at"):
        exists = bind.execute(sa.text(f"""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'clients' AND column_name = '{col}'
        """)).first()
        if exists:
            op.drop_column("clients", col)

    bind.execute(sa.text(
        "CREATE VIEW v_real_clients AS "
        "SELECT * FROM clients "
        "WHERE is_training = false AND is_coaching = false"
    ))

    # ── 6) Recreate partial unique index — one training child per
    #    parent, regardless of lifecycle state. A closed session has
    #    already cascade-deleted its child row, so "one at a time"
    #    is enforced simply by "child row exists". ─────────────────────
    bind.execute(sa.text("""
        CREATE UNIQUE INDEX uq_one_active_training_per_parent
        ON clients (parent_client_id)
        WHERE is_training = true AND parent_client_id IS NOT NULL
    """))


def downgrade() -> None:
    # Re-add the two columns + the old check + old indexes. Data in
    # auto_remove_at is preserved on the column but the old session-
    # level clock state is LOST (we didn't snapshot training_ends_at
    # anywhere before dropping it).
    bind = op.get_bind()

    bind.execute(sa.text("DROP INDEX IF EXISTS uq_one_active_training_per_parent"))

    op.add_column(
        "clients",
        sa.Column("training_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("training_status", sa.String(length=20), nullable=True),
    )
    # Default the resurrected rows to ACTIVE with a far-future ends_at
    # so a rolled-back deploy doesn't immediately mark everything as
    # due-for-cleanup.
    bind.execute(sa.text("""
        UPDATE clients
        SET training_status = 'ACTIVE',
            training_ends_at = now() + INTERVAL '365 days'
        WHERE is_training = true
    """))

    op.drop_constraint("chk_client_shape", "clients", type_="check")
    op.create_check_constraint(
        "chk_client_shape",
        "clients",
        "(is_training = false AND is_coaching = false "
        "  AND parent_client_id IS NULL "
        "  AND training_started_at IS NULL "
        "  AND training_ends_at IS NULL "
        "  AND training_status IS NULL "
        "  AND parent_session_id IS NULL) "
        "OR "
        "(is_training = true AND is_coaching = false "
        "  AND parent_client_id IS NOT NULL "
        "  AND training_started_at IS NOT NULL "
        "  AND training_ends_at IS NOT NULL "
        "  AND training_status IS NOT NULL "
        "  AND parent_session_id IS NULL) "
        "OR "
        "(is_training = false AND is_coaching = true "
        "  AND parent_client_id IS NOT NULL "
        "  AND parent_session_id IS NOT NULL "
        "  AND training_started_at IS NULL "
        "  AND training_ends_at IS NULL "
        "  AND training_status IS NULL)",
    )

    bind.execute(sa.text("""
        CREATE UNIQUE INDEX uq_one_active_training_per_parent
        ON clients (parent_client_id)
        WHERE is_training = true
          AND training_status IN ('ACTIVE', 'WINDING_DOWN')
    """))
    bind.execute(sa.text("""
        CREATE INDEX ix_clients_training_status
        ON clients (training_status)
        WHERE training_status IS NOT NULL
    """))

    bind.execute(sa.text("DROP INDEX IF EXISTS ix_subs_auto_remove_at"))
    op.drop_column("subscriptions", "auto_remove_at")
