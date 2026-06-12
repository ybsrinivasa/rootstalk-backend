"""dealer_manufacturer_catalog: add manufacturer_translations JSONB col

The dealer-Dealerships page reads from `dealer_manufacturer_catalog`
(a parallel cache to brand_lookup_cache that materialises the
L2 → common_name → trade_name → manufacturer walk per category).
Like brand_lookup_cache before 2026-06-12, it cached only the English
manufacturer_name — so the Dealerships page stayed English even after
input_manufacturers sync brought Hindi / Kannada / Tamil names into
cosh_core_items.

This migration mirrors the brand_lookup_cache pattern: nullable JSONB
column populated at refresh time. The English string column stays as
audit-trail fallback. Reads use pick_translation(translations, lang,
manufacturer_name).

Revision ID: e4f1a8b209c5
Revises: c8e2f1b40d33
Create Date: 2026-06-12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "e4f1a8b209c5"
down_revision = "c8e2f1b40d33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dealer_manufacturer_catalog",
        sa.Column(
            "manufacturer_translations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dealer_manufacturer_catalog", "manufacturer_translations")
