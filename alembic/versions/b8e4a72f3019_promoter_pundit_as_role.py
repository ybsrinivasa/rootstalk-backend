"""Promote Promoter-Pundit to a first-class PunditRole; drop is_promoter_pundit flag.

Revision ID: b8e4a72f3019
Revises: b3d7e9a14c82
Create Date: 2026-06-23

Background:
Pre-this migration, a Promoter-Pundit row in client_farm_pundits was
modelled as `role='PANEL' AND is_promoter_pundit=true`. Two badges on
the dashboard ("Panel" + "Promoter") leaked from this dual-state.

User locked the design 2026-06-23:
- Promoter-Pundit is its own role (`PROMOTER_PUNDIT`), distinct from
  regular pundits (PRIMARY / PANEL).
- A user can have at most ONE PROMOTER_PUNDIT row across all clients
  (PP serves a single company because they are a Promoter for one).
- A user cannot be both PROMOTER_PUNDIT and a regular pundit at the
  same client. The existing UniqueConstraint(client_id, pundit_id)
  already enforces single-row-per-pair; the write-path guards reject
  role transitions.

Migration steps:
1. Convert every row with `is_promoter_pundit=true` to `role='PROMOTER_PUNDIT'`.
2. Drop the `is_promoter_pundit` column.
3. Add a partial unique index ensuring at most one PROMOTER_PUNDIT row
   per pundit (across clients).

Production has not started yet (user, 2026-06-23) — no data-loss risk.
"""
from alembic import op
import sqlalchemy as sa


revision = "b8e4a72f3019"
down_revision = "b3d7e9a14c82"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: heal data. Any active row with the flag becomes the new role.
    op.execute(
        "UPDATE client_farm_pundits SET role = 'PROMOTER_PUNDIT' "
        "WHERE is_promoter_pundit = TRUE"
    )
    # Step 2: drop the flag column.
    op.drop_column("client_farm_pundits", "is_promoter_pundit")
    # Step 3: enforce single-company-PP-per-user via a partial unique index.
    op.create_index(
        "ux_client_farm_pundits_promoter_per_pundit",
        "client_farm_pundits",
        ["pundit_id"],
        unique=True,
        postgresql_where=sa.text("role = 'PROMOTER_PUNDIT'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_client_farm_pundits_promoter_per_pundit",
        table_name="client_farm_pundits",
    )
    op.add_column(
        "client_farm_pundits",
        sa.Column("is_promoter_pundit", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
    )
    op.execute(
        "UPDATE client_farm_pundits SET is_promoter_pundit = TRUE, role = 'PANEL' "
        "WHERE role = 'PROMOTER_PUNDIT'"
    )
