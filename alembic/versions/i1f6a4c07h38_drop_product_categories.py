"""drop_product_categories

Revision ID: i1f6a4c07h38
Revises: h0e5f3b96g27
Create Date: 2026-10-17

Phase 6d of the Soil Nutrient module rework (Decision U consequence)
— drop product_categories + product_category_fields.

With the Phase 6b (Fallback) + Phase 6c (ClientProduct) pointer-table
rewrites, both product tables lost their category_id FK. The field
schema (N%, P2O5%, formulation, application_methods, …) that
product_category_fields encoded now lives in Cosh and is resolved at
read time via load_fertiliser_candidates(). Nothing in the backend
reads product_categories anymore; dropping them removes dead data
that would otherwise invite drift (CMs updating schemas no UI reads).

The two tables held platform-seeded reference data (small row counts,
no personalisation state), so no data-migration concerns. Downgrade
recreates the empty tables matching their Phase 1 shape so the old
authoring surfaces could in theory boot, but they'd find zero rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'i1f6a4c07h38'
down_revision: Union[str, Sequence[str], None] = 'h0e5f3b96g27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('product_category_fields')
    op.drop_table('product_categories')


def downgrade() -> None:
    op.create_table(
        'product_categories',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False, unique=True),
        sa.Column('scope', sa.String(length=20),
                  nullable=False, server_default='GLOBAL'),
        sa.Column('display_order', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('status', sa.String(length=20),
                  nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'product_category_fields',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('category_id', sa.String(length=36),
                  sa.ForeignKey('product_categories.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('field_key', sa.String(length=100), nullable=False),
        sa.Column('field_label', sa.String(length=200), nullable=False),
        sa.Column('field_type', sa.String(length=20), nullable=False),
        sa.Column('required', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('unit', sa.String(length=40), nullable=True),
        sa.Column('enum_values_json', sa.JSON(), nullable=True),
        sa.Column('display_order', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('status', sa.String(length=20),
                  nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_product_category_fields_category_id',
                    'product_category_fields', ['category_id'])
