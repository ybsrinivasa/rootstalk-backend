"""soil_nutrient_phase3_client_products

Revision ID: d4c2f574e217
Revises: d3b1f574e216
Create Date: 2026-10-15

Phase 3 of the Soil Nutrient Management module — Client Products
(Decision L in project_soil_nutrient_module_design.md).

Two new tables:
  - client_products — per-Client Global Product List, Tier 1 of the
    3-tier recommendation hierarchy. Authored by CA/SE/CM-EDIT in
    the Client Portal when soil_nutrient_module_enabled=True.
    Schema mirrors fallback_products so the engine's candidate loader
    can produce a uniform Candidate shape regardless of source.
  - client_product_crop_assignments — per-crop assignment on each
    product. Unlike Fallback (empty = universal), Client Products
    only participate in personalisation for the crops they're
    explicitly assigned to.

Both tables ON DELETE CASCADE from the clients table so Client
offboarding is clean.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4c2f574e217'
down_revision: Union[str, Sequence[str], None] = 'd3b1f574e216'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'client_products',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('client_id', sa.String(length=36),
                  sa.ForeignKey('clients.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('category_id', sa.String(length=36),
                  sa.ForeignKey('product_categories.id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('display_name', sa.String(length=500), nullable=False),
        sa.Column('manufacturer', sa.String(length=500), nullable=True),
        sa.Column('grade', sa.String(length=100), nullable=True),
        sa.Column('formulation', sa.String(length=100), nullable=True),
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
        sa.Column('application_methods', sa.JSON(), nullable=True),
        sa.Column('data_json', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(length=20),
                  nullable=False, server_default='ACTIVE'),
        sa.Column('created_by', sa.String(length=36),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        'ix_client_products_client_id',
        'client_products', ['client_id'],
    )
    op.create_index(
        'ix_client_products_category_id',
        'client_products', ['category_id'],
    )
    op.create_index(
        'ix_client_products_status',
        'client_products', ['status'],
    )

    op.create_table(
        'client_product_crop_assignments',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('product_id', sa.String(length=36),
                  sa.ForeignKey('client_products.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('client_id', sa.String(length=36),
                  sa.ForeignKey('clients.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('crop_cosh_id', sa.String(length=100), nullable=False),
        sa.Column('status', sa.String(length=20),
                  nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('product_id', 'crop_cosh_id',
                            name='uq_client_product_crop'),
    )
    op.create_index(
        'ix_client_product_crop_product_id',
        'client_product_crop_assignments', ['product_id'],
    )
    op.create_index(
        'ix_client_product_crop_client_crop',
        'client_product_crop_assignments', ['client_id', 'crop_cosh_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_client_product_crop_client_crop',
                  table_name='client_product_crop_assignments')
    op.drop_index('ix_client_product_crop_product_id',
                  table_name='client_product_crop_assignments')
    op.drop_table('client_product_crop_assignments')
    op.drop_index('ix_client_products_status',
                  table_name='client_products')
    op.drop_index('ix_client_products_category_id',
                  table_name='client_products')
    op.drop_index('ix_client_products_client_id',
                  table_name='client_products')
    op.drop_table('client_products')
