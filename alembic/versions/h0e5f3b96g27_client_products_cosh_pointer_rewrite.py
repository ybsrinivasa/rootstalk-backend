"""client_products_cosh_pointer_rewrite

Revision ID: h0e5f3b96g27
Revises: g9d4e2a85f16
Create Date: 2026-10-17

Phase 6c of the Soil Nutrient module rework (Decision U) — the
Client Global Product List becomes a pointer table over Cosh
products, mirroring the Phase 6b fallback_products shape.

Walk-back of Phase 2's native authoring surface (shipped
2026-10-15, LIVE STAGING with zero rows — Clients never adopted
the native authoring surface before the walk-back, so no data to
preserve). Drop & recreate without data migration.

The sibling client_product_crop_assignments table rebuilds with
FK to the new pointer table; its own shape (product_id, client_id,
crop_cosh_id, status) stays identical to Phase 2.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'h0e5f3b96g27'
down_revision: Union[str, Sequence[str], None] = 'g9d4e2a85f16'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('client_product_crop_assignments')
    op.drop_table('client_products')

    op.create_table(
        'client_products',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('client_id', sa.String(length=36),
                  sa.ForeignKey('clients.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('cosh_product_id', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=20),
                  nullable=False, server_default='ACTIVE'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=36),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('client_id', 'cosh_product_id',
                            name='uq_client_product_cosh'),
    )
    op.create_index(
        'ix_client_products_client_id',
        'client_products', ['client_id'],
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
        'ix_client_product_crop_assignments_product_id',
        'client_product_crop_assignments', ['product_id'],
    )
    op.create_index(
        'ix_client_product_crop_assignments_client_id',
        'client_product_crop_assignments', ['client_id'],
    )
    op.create_index(
        'ix_client_product_crop_assignments_crop_cosh_id',
        'client_product_crop_assignments', ['crop_cosh_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_client_product_crop_assignments_crop_cosh_id',
                  table_name='client_product_crop_assignments')
    op.drop_index('ix_client_product_crop_assignments_client_id',
                  table_name='client_product_crop_assignments')
    op.drop_index('ix_client_product_crop_assignments_product_id',
                  table_name='client_product_crop_assignments')
    op.drop_table('client_product_crop_assignments')
    op.drop_index('ix_client_products_status',
                  table_name='client_products')
    op.drop_index('ix_client_products_client_id',
                  table_name='client_products')
    op.drop_table('client_products')

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
    op.create_index('ix_client_products_client_id',
                    'client_products', ['client_id'])
    op.create_index('ix_client_products_category_id',
                    'client_products', ['category_id'])
    op.create_index('ix_client_products_status',
                    'client_products', ['status'])

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
    op.create_index('ix_client_product_crop_assignments_product_id',
                    'client_product_crop_assignments', ['product_id'])
    op.create_index('ix_client_product_crop_assignments_client_id',
                    'client_product_crop_assignments', ['client_id'])
    op.create_index('ix_client_product_crop_assignments_crop_cosh_id',
                    'client_product_crop_assignments', ['crop_cosh_id'])
