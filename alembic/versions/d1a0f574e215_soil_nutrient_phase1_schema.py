"""soil_nutrient_phase1_schema

Revision ID: d1a0f574e215
Revises: c8b4d7f2e691
Create Date: 2026-10-15

Phase 1 of the Soil Nutrient Management module (Decisions L, M, N, P
in project_soil_nutrient_module_design.md). Adds platform-scoped
foundation tables + two Client flags.

Four new tables (platform-scoped, SA/CM authors in SA Portal):
  - platform_soil_parameters — the 21+ parameter canon (Decision N).
    Seeded with 22 rows modeled on the ICL Narayanswamy soil-test
    report (Grape Red globe, 2026-01-23) in the next migration.
  - product_categories — platform category list (Decision L). Two
    v1 categories: Direct Soil Application + Water Solubles.
  - product_category_fields — per-category field schema driving
    product authoring UI.
  - preset_soil_profiles — soil-value profiles for Mock input
    (Decision P + MQ5-B).

Two Client flags (SA-toggled in the SA Portal, bool default False):
  - soil_nutrient_module_enabled — gates the module on/off per Client.
    Enforced: cannot be True for is_coaching=True or is_training=True
    Clients (sandbox clients don't carry live nutrition personalisation).
  - has_soil_testing_lab — enables the lab-partner path (Decision K).
    Enforced: cannot be True for is_coaching=True Clients.

Both guards are enforced in the SA Portal UI (checkbox greyed out)
and at the service layer (write-side validation).

Seed data lands in the next migration (d2a0f574e215).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1a0f574e215'
# prod-safe-soil-2026-10-22: re-root the soil chain so it chains OFF
# the current prod coaching tip (e7b8c592d104, workspace snapshot url)
# instead of the pre-coaching c8b4d7f2e691. This lets `alembic upgrade
# head` apply the full soil chain forward on prod, which currently has
# e7b8c592d104 as its alembic_version. On main the original value
# c8b4d7f2e691 stays; this edit is prod-safe-soil-only.
down_revision: Union[str, Sequence[str], None] = 'e7b8c592d104'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Client flags ──────────────────────────────────────────────────
    op.add_column('clients', sa.Column(
        'soil_nutrient_module_enabled', sa.Boolean(),
        nullable=False, server_default=sa.false(),
    ))
    op.add_column('clients', sa.Column(
        'has_soil_testing_lab', sa.Boolean(),
        nullable=False, server_default=sa.false(),
    ))

    # ── platform_soil_parameters ──────────────────────────────────────
    op.create_table(
        'platform_soil_parameters',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False, unique=True),
        sa.Column('unit', sa.String(length=40), nullable=True),
        sa.Column('category_group', sa.String(length=20), nullable=False),
        sa.Column('formula', sa.String(length=200), nullable=True),
        sa.Column('measurement_method_note', sa.Text(), nullable=True),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        'ix_platform_soil_parameters_category_group',
        'platform_soil_parameters', ['category_group'],
    )

    # ── product_categories ────────────────────────────────────────────
    op.create_table(
        'product_categories',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False, unique=True),
        sa.Column('scope', sa.String(length=20), nullable=False, server_default='GLOBAL'),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )

    # ── product_category_fields ───────────────────────────────────────
    op.create_table(
        'product_category_fields',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('category_id', sa.String(length=36),
                  sa.ForeignKey('product_categories.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('field_key', sa.String(length=100), nullable=False),
        sa.Column('field_label', sa.String(length=200), nullable=False),
        sa.Column('field_type', sa.String(length=20), nullable=False),
        sa.Column('required', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('unit', sa.String(length=40), nullable=True),
        sa.Column('enum_values_json', sa.JSON(), nullable=True),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('category_id', 'field_key', name='uq_category_field_key'),
    )
    op.create_index(
        'ix_product_category_fields_category_id',
        'product_category_fields', ['category_id'],
    )

    # ── preset_soil_profiles ──────────────────────────────────────────
    op.create_table(
        'preset_soil_profiles',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False, unique=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('values_json', sa.JSON(), nullable=False),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('preset_soil_profiles')
    op.drop_index('ix_product_category_fields_category_id',
                  table_name='product_category_fields')
    op.drop_table('product_category_fields')
    op.drop_table('product_categories')
    op.drop_index('ix_platform_soil_parameters_category_group',
                  table_name='platform_soil_parameters')
    op.drop_table('platform_soil_parameters')
    op.drop_column('clients', 'has_soil_testing_lab')
    op.drop_column('clients', 'soil_nutrient_module_enabled')
