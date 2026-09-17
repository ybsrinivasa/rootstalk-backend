"""brand_cache_ai_concentration

Revision ID: f3d5c2a8b1e6
Revises: e2c1d9a4b7f5
Create Date: 2026-09-17

Advisory-Only v1.9 — surface AI concentration on the brand cache
so the Brands endpoints can filter strictly on it. Dosage is
directly proportional to AI concentration; showing a farmer brands
with the wrong % puts them at risk of over/under-dosing.

Three columns added to brand_lookup_cache (all nullable — will fill
on the next `/admin/brand-cache/refresh`):

- ai_concentration_cosh_id — Cosh ID of the tradename's a.i. value
- ai_concentration_display — English display (e.g. '40')
- ai_concentration_translations — locale map mirrored from cosh_core_items

Applied via same rebuild path as formulation; follows the existing
one-tn-to-one-value assumption (see brand_cache.py — `.setdefault`
takes the first value if a trade name has multiple).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f3d5c2a8b1e6"
down_revision = "e2c1d9a4b7f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "brand_lookup_cache",
        sa.Column("ai_concentration_cosh_id", sa.String(100), nullable=True),
    )
    op.add_column(
        "brand_lookup_cache",
        sa.Column("ai_concentration_display", sa.String(200), nullable=True),
    )
    op.add_column(
        "brand_lookup_cache",
        sa.Column(
            "ai_concentration_translations",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("brand_lookup_cache", "ai_concentration_translations")
    op.drop_column("brand_lookup_cache", "ai_concentration_display")
    op.drop_column("brand_lookup_cache", "ai_concentration_cosh_id")
