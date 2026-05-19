"""Batch CC (2026-05-19) — per-client custom variables under Cosh parameters.

Adds `variables.client_id` (nullable FK → clients.id). NULL means
"global" (Cosh-shipped or SA-added). When the CA adds a custom
variable to a Cosh parameter, the new row carries the CA's
client_id so other clients don't see it.

Schema also widens the uniqueness rule:
  • Drops the old UniqueConstraint(parameter_id, name).
  • Adds a partial unique index on (parameter_id, name) WHERE
    client_id IS NULL — globals stay globally unique.
  • Adds a partial unique index on
    (parameter_id, client_id, name) WHERE client_id IS NOT NULL —
    two different clients can share a name.

Revision ID: c8e94a3b21f7
Revises: a5b1f8d62e09
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa


revision = "c8e94a3b21f7"
down_revision = "a5b1f8d62e09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "variables",
        sa.Column(
            "client_id", sa.String(length=36),
            sa.ForeignKey("clients.id"), nullable=True,
        ),
    )
    op.drop_constraint("variables_parameter_id_name_key", "variables", type_="unique")
    op.create_index(
        "uq_variables_parameter_name_global",
        "variables",
        ["parameter_id", "name"],
        unique=True,
        postgresql_where=sa.text("client_id IS NULL"),
    )
    op.create_index(
        "uq_variables_parameter_client_name_custom",
        "variables",
        ["parameter_id", "client_id", "name"],
        unique=True,
        postgresql_where=sa.text("client_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_variables_parameter_client_name_custom", table_name="variables")
    op.drop_index("uq_variables_parameter_name_global", table_name="variables")
    op.create_unique_constraint(
        "variables_parameter_id_name_key",
        "variables",
        ["parameter_id", "name"],
    )
    op.drop_column("variables", "client_id")
