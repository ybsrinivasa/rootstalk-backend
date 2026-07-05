"""content_translations unified table for SE-authored content.

Revision ID: d0e4a51b9c72
Revises: c9d3f47a2e18
Create Date: 2026-07-05

Background:
SE-authored content on the CA portal (package description, practice
element text, standard-response question, seed variety bullets) is
authored in English but needs to reach farmers in their language.
Existing translation tables (`parameter_translations`,
`variable_translations`, `conditional_question_translations`) cover
three specific fields with a dedicated table each. Extending that
pattern for every new translatable field would mean ~4 more tables
now and one per future field — noisy schema for what is really a
uniform pattern.

This unified table lets any (entity_type, entity_id, field_path,
language_code) row store a translation with:
  - source_hash: SHA256 hex of the English source at translate-time,
                 so delta detection compares current source hash to
                 stored hash. Skip retranslation when unchanged.
  - translation_status: PENDING (queued / generating) / APPROVED /
                        STALE (source drifted since translate) /
                        FAILED (Claude call bombed; retry queued).
                        String not enum so we can add values later
                        without migration.
  - translated_text: the localised content. For scalar English
                     fields, plain text. For JSONB list fields
                     (SeedVariety.description_points), JSON-encoded
                     list of translated items.

field_path is mostly empty string for scalar fields; kept in the
key to support future indexed sub-fields without another migration.

Entity types shipped in Phase T-1:
  - package.description
  - element.value
  - standard_response.question_text
  - seed_variety.description_points

Additional entity types (pesticide/fertilizer/seed-batch fields
pending user's client conversations, see
project_rootstalk_qr_module_pending_fields_2026_07_05.md) plug in
without schema change.

Existing 3 translation tables are NOT migrated into this. Their
serving code paths stay; we may consolidate later if it hurts. This
migration is purely additive.

Nullable-additive, code-only rollback safe.
"""

from alembic import op
import sqlalchemy as sa


revision = "d0e4a51b9c72"
down_revision = "c9d3f47a2e18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_translations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("field_path", sa.String(128), nullable=False, server_default=""),
        sa.Column("language_code", sa.String(10), nullable=False),
        sa.Column("translated_text", sa.Text(), nullable=True),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column(
            "translation_status", sa.String(20),
            nullable=False, server_default="PENDING",
        ),
        sa.Column("translated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "approved_by", sa.String(36),
            sa.ForeignKey("users.id"), nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "entity_type", "entity_id", "field_path", "language_code",
            name="uq_content_translations_key",
        ),
    )
    op.create_index(
        "ix_content_translations_lookup",
        "content_translations",
        ["entity_type", "entity_id", "language_code"],
    )


def downgrade() -> None:
    op.drop_index("ix_content_translations_lookup", table_name="content_translations")
    op.drop_table("content_translations")
