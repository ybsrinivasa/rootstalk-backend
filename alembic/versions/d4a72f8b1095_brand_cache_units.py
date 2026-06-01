"""Brand lookup cache — allowed pack units from tradenames_units

Revision ID: d4a72f8b1095
Revises: c8f1e2b4a906
Create Date: 2026-06-01

Adds `brand_lookup_cache.units` (JSON array of `{cosh_id, name}`),
populated from Cosh's `tradenames_units` Connect. Replaces the
formulation-class inference for the dealer's Unit dropdown.
"""
from alembic import op
import sqlalchemy as sa


revision = "d4a72f8b1095"
down_revision = "c8f1e2b4a906"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "brand_lookup_cache",
        sa.Column(
            "units",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("brand_lookup_cache", "units")
