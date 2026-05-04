"""promoter_allocations table + backfill from existing ACTIVE subscriptions

Revision ID: e8c4a7d62b91
Revises: d5e7b1a93f28
Create Date: 2026-05-04 16:00:00.000000

Per-promoter sub-account of the company subscription pool. The CA
allocates units to each promoter; promoters consume their own row
when assigning subscriptions; CA can reclaim unconsumed units back
to the company pool. Replaces the old "company-pool-as-single-gate"
model — see BL-11.

Backfill: existing ACTIVE Subscription rows with a non-null
promoter_user_id had each implicitly consumed one company-pool unit
under the old model. Seed promoter_allocations for those pairs so
the CA's view of "this promoter has used N units" is accurate from
day 1 — allocated_total = consumed_total = N, balance = 0. The CA
must explicitly re-allocate going forward.
"""
from alembic import op
import sqlalchemy as sa


revision = "e8c4a7d62b91"
down_revision = "d5e7b1a93f28"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "promoter_allocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "client_id", sa.String(36),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "promoter_user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("units_balance", sa.Integer, nullable=False, server_default="0"),
        sa.Column("allocated_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reclaimed_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("consumed_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "client_id", "promoter_user_id",
            name="uq_promoter_alloc_client_promoter",
        ),
    )

    # Backfill — seed allocations for historical promoter-led ACTIVE subs.
    # We use raw SQL for portability across SQLAlchemy versions and to
    # avoid pulling in the ORM during a migration.
    op.execute("""
        INSERT INTO promoter_allocations (
            id, client_id, promoter_user_id,
            units_balance, allocated_total, reclaimed_total, consumed_total,
            created_at, updated_at
        )
        SELECT
            md5(client_id || '|' || promoter_user_id)::text AS id,
            client_id,
            promoter_user_id,
            0           AS units_balance,
            COUNT(*)    AS allocated_total,
            0           AS reclaimed_total,
            COUNT(*)    AS consumed_total,
            now()       AS created_at,
            now()       AS updated_at
        FROM subscriptions
        WHERE status = 'ACTIVE'
          AND promoter_user_id IS NOT NULL
        GROUP BY client_id, promoter_user_id
    """)


def downgrade():
    op.drop_table("promoter_allocations")
