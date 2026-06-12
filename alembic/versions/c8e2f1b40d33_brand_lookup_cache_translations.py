"""brand_lookup_cache: add translations JSONB cols + extend units shape

Why: until 2026-06-12 brand_lookup_cache stored only English values
(trade_name, manufacturer_name, formulation_name). The PWA heading
on already-decided order items therefore rendered English even after
the user picked Hindi / Kannada / Tamil, while bullet lists fed by
cosh_core_items.translations rendered the chosen locale — making the
two surfaces look like they were showing different brands.

This migration extends the cache to carry the per-entry translations
map, so every brand-side surface can call pick_translation(translations,
lang, fallback) and render in the user's language. The English string
columns stay as audit-trail fallbacks (and for any non-localised callers
that still rely on them).

`units` already exists as a JSON column shaped `[{"cosh_id", "name"}]`.
We don't change the schema for units — the refresh code will start
writing the extended shape `[{"cosh_id", "name", "translations"}]` and
read sites can probe `translations` opportunistically. Existing rows
keep their old shape until the next refresh.

After this lands, run /admin/brand-cache/refresh (or wait for lazy
bootstrap on first read) to populate the new columns. Until then,
pick_translation falls back to the existing English columns — no
disruption.

Revision ID: c8e2f1b40d33
Revises: a7c1f4d28e69
Create Date: 2026-06-12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c8e2f1b40d33"
down_revision = "a7c1f4d28e69"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "brand_lookup_cache",
        sa.Column(
            "trade_name_translations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "brand_lookup_cache",
        sa.Column(
            "manufacturer_translations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "brand_lookup_cache",
        sa.Column(
            "formulation_translations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("brand_lookup_cache", "formulation_translations")
    op.drop_column("brand_lookup_cache", "manufacturer_translations")
    op.drop_column("brand_lookup_cache", "trade_name_translations")
