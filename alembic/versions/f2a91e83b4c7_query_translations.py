"""Query translations — free-text Q&A translation cache + source-locale tags.

Revision ID: f2a91e83b4c7
Revises: e8f4d21a9c33
Create Date: 2026-07-12

Powers the farmer↔pundit translation flow (Query.description,
QueryResponse.text, QueryRemark.remark). All three source rows get a
new `<field>_locale` column so the reader-side resolver knows whether
translation is even needed. Cache lives in a fresh
`query_translations` table keyed on (entity_type, entity_id,
target_locale) so panel-expert threads (3 pundits, 3 locales) reuse
each other's translations when the target locale collides.

Design decisions locked with user 2026-07-12:
- English-pivot only. Every translation is farmer_lang↔English; no
  Indic-to-Indic pairs stored.
- If farmer_lang == 'en', zero rows written for that thread.
- source_locale trusted from User.language_code at write time; no
  auto-detection in v1.

All additive nullable; code-only rollback safe.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2a91e83b4c7"
down_revision: Union[str, Sequence[str], None] = "e8f4d21a9c33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Source-locale tags on the existing tables ─────────────────────────
    op.add_column(
        "queries",
        sa.Column("description_locale", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "query_responses",
        sa.Column("text_locale", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "query_remarks",
        sa.Column("remark_locale", sa.String(length=10), nullable=True),
    )

    # ── Translation cache ─────────────────────────────────────────────────
    op.create_table(
        "query_translations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("source_locale", sa.String(length=10), nullable=False),
        sa.Column("target_locale", sa.String(length=10), nullable=False),
        sa.Column("translated_text", sa.Text(), nullable=False),
        sa.Column(
            "translated_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        # Provider label for debugging quality regressions; e.g. `sonnet`,
        # `opus`, `google`. Not queried on the read path.
        sa.Column("provider", sa.String(length=20), nullable=True),
        sa.UniqueConstraint(
            "entity_type", "entity_id", "target_locale",
            name="uq_query_translations_entity_target",
        ),
    )
    op.create_index(
        "ix_query_translations_lookup",
        "query_translations",
        ["entity_type", "entity_id", "target_locale"],
    )


def downgrade() -> None:
    op.drop_index("ix_query_translations_lookup", table_name="query_translations")
    op.drop_table("query_translations")
    op.drop_column("query_remarks", "remark_locale")
    op.drop_column("query_responses", "text_locale")
    op.drop_column("queries", "description_locale")
