"""soil_nutrient_phase2_fallback

Revision ID: d3b1f574e216
Revises: d2a0f574e215
Create Date: 2026-10-15

Phase 2 of the Soil Nutrient Management module — Fallback Catalogue
(Decision M in project_soil_nutrient_module_design.md).

Two new tables:
  - fallback_products — industry-wide product list, Tier 2 of the
    3-tier recommendation hierarchy. Native RootsTalk entity (NOT
    Cosh-sourced per Decision M's partial supersession of Decision D).
    Authored by SA/CM in the SA Portal.
  - fallback_product_crop_assignments — M:N against crops. Optional:
    no rows for a product means it's cross-crop-applicable (universal).

Hybrid storage: hot nutrient-content columns (N%, P%, K%, Ca%, Mg%,
S%, Zn%, Fe%, Mn%, Cu%, B%) as proper columns; application_methods
as JSON array; data_json for anything else from the category schema.

No seed data — SA populates via the UI as the catalogue grows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd3b1f574e216'
down_revision: Union[str, Sequence[str], None] = 'd2a0f574e215'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'fallback_products',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('category_id', sa.String(length=36),
                  sa.ForeignKey('product_categories.id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('display_name', sa.String(length=500), nullable=False),
        sa.Column('manufacturer', sa.String(length=500), nullable=True),
        sa.Column('grade', sa.String(length=100), nullable=True),
        sa.Column('formulation', sa.String(length=100), nullable=True),
        # Hot nutrient-content columns
        sa.Column('n_pct', sa.Float(), nullable=True),
        sa.Column('p2o5_pct', sa.Float(), nullable=True),
        sa.Column('k2o_pct', sa.Float(), nullable=True),
        sa.Column('ca_pct', sa.Float(), nullable=True),
        sa.Column('mg_pct', sa.Float(), nullable=True),
        sa.Column('s_pct', sa.Float(), nullable=True),
        sa.Column('zn_pct', sa.Float(), nullable=True),
        sa.Column('fe_pct', sa.Float(), nullable=True),
        sa.Column('mn_pct', sa.Float(), nullable=True),
        sa.Column('cu_pct', sa.Float(), nullable=True),
        sa.Column('b_pct', sa.Float(), nullable=True),
        # Application methods as JSON array of strings
        sa.Column('application_methods', sa.JSON(), nullable=True),
        # Catch-all for additional fields keyed by field_key
        sa.Column('data_json', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(length=20),
                  nullable=False, server_default='ACTIVE'),
        sa.Column('created_by', sa.String(length=36),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        'ix_fallback_products_category_id',
        'fallback_products', ['category_id'],
    )
    op.create_index(
        'ix_fallback_products_status',
        'fallback_products', ['status'],
    )

    op.create_table(
        'fallback_product_crop_assignments',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('product_id', sa.String(length=36),
                  sa.ForeignKey('fallback_products.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('crop_cosh_id', sa.String(length=100), nullable=False),
        sa.Column('status', sa.String(length=20),
                  nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('product_id', 'crop_cosh_id',
                            name='uq_fallback_product_crop'),
    )
    op.create_index(
        'ix_fallback_crop_assignments_product_id',
        'fallback_product_crop_assignments', ['product_id'],
    )
    op.create_index(
        'ix_fallback_crop_assignments_crop_cosh_id',
        'fallback_product_crop_assignments', ['crop_cosh_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_fallback_crop_assignments_crop_cosh_id',
                  table_name='fallback_product_crop_assignments')
    op.drop_index('ix_fallback_crop_assignments_product_id',
                  table_name='fallback_product_crop_assignments')
    op.drop_table('fallback_product_crop_assignments')
    op.drop_index('ix_fallback_products_status',
                  table_name='fallback_products')
    op.drop_index('ix_fallback_products_category_id',
                  table_name='fallback_products')
    op.drop_table('fallback_products')
