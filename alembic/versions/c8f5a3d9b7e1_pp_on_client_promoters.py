"""client_promoters.is_promoter_pundit — Promoter-as-Pundit flag

Revision ID: c8f5a3d9b7e1
Revises: f1a2b3c4d5e6
Create Date: 2026-05-31

Mirrors the long-standing `client_farm_pundits.is_promoter_pundit`
column onto `client_promoters` so a Field Manager can designate one
of the client's Facilitators / Dealers as a Promoter-Pundit without
first having to onboard them as a FarmPundit.

Per-(user, client) mutual exclusion with the FarmPundit P-P flag is
enforced at the application layer (write-time guard), not via a DB
constraint — the two source tables don't share a primary key.
"""
from alembic import op
import sqlalchemy as sa


revision = "c8f5a3d9b7e1"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "client_promoters",
        sa.Column(
            "is_promoter_pundit",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Index to make the "find P-Ps for this client" query a single hop
    # for the routing chain + the CA read-only sub-tab.
    op.create_index(
        "ix_client_promoters_pp_per_client",
        "client_promoters",
        ["client_id", "is_promoter_pundit"],
    )


def downgrade() -> None:
    op.drop_index("ix_client_promoters_pp_per_client", table_name="client_promoters")
    op.drop_column("client_promoters", "is_promoter_pundit")
