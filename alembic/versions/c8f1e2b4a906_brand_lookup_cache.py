"""Brand lookup cache

Revision ID: c8f1e2b4a906
Revises: a4c7e2f81b65
Create Date: 2026-06-01

Fix 2026-06-01 — BL-07's brand picker was returning empty lists
because it searched `cosh_core_items` for `core_type='brand'`, but
Cosh stores brands as `trade_names` + the `tradename_commonname`
Connect. The walk on every request would have been too slow given
the 13k+ trade-name dataset, so we materialise it.

One row per (common_name_cosh_id, trade_name_cosh_id) with the
brand name, manufacturer, and formulation pre-resolved. Truncate-
and-reload via /admin/brand-cache/refresh (or lazy on first read).
Mirrors the dealer_manufacturer_catalog pattern.
"""
from alembic import op
import sqlalchemy as sa


revision = "c8f1e2b4a906"
down_revision = "a4c7e2f81b65"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brand_lookup_cache",
        sa.Column("common_name_cosh_id", sa.String(100), primary_key=True),
        sa.Column("trade_name_cosh_id", sa.String(100), primary_key=True),
        sa.Column("trade_name", sa.String(500), nullable=False),
        sa.Column("manufacturer_cosh_id", sa.String(100), nullable=True),
        sa.Column("manufacturer_name", sa.String(500), nullable=True),
        sa.Column("formulation_cosh_id", sa.String(100), nullable=True),
        sa.Column("formulation_name", sa.String(500), nullable=True),
        sa.Column(
            "refreshed_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_brand_cache_cn", "brand_lookup_cache", ["common_name_cosh_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_brand_cache_cn", table_name="brand_lookup_cache")
    op.drop_table("brand_lookup_cache")
