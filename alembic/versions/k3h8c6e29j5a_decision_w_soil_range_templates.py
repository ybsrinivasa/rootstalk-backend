"""decision_w_soil_range_templates

Revision ID: k3h8c6e29j5a
Revises: j2g7b5d18i49
Create Date: 2026-10-17

Decision W (settled 2026-10-17 evening) — walks back Decision N's
crop-level Normal Range scope in favour of client-level, geography-
based Soil Range Templates attached per-Package.

Rationale: soil property norms (pH, EC, OC, free CaCO3, base N/P/K
levels) are governed by geography — parent material, climate,
drainage — not by what crop was grown on the plot. A single Dharwad-
red template serves Mango, Pomegranate, and Grape Packages equally.
The old crop-level shape forced SEs to author the same soil-fact
range three times.

Decision V's % adjustments (Low Adj / High Adj) are crop-sensitive,
not geography-sensitive, so they travel with the Package (which
knows its crop) via package_normal_range_overrides. Templates carry
operator + Low + High only.

New shape:
  - soil_range_templates (client-level library row:
      id, client_id, name, description, status)
  - soil_range_template_rows (one per parameter per template:
      id, template_id, parameter_id, operator, low_value, high_value)
  - packages.soil_range_template_id — nullable FK; Package without
    a template just has no classification by that path (Package
    overrides may still carry bounds + adjustments independently).

Drop:
  - client_normal_ranges — empty on staging per user confirmation
    ("team hasn't yet had a chance to play and test"), clean drop.

Keep unchanged:
  - package_normal_range_overrides — still per-Package, now the sole
    home for Decision V's low_pct_adjustment + high_pct_adjustment
    columns and for sparse Package-level bounds overrides against the
    chosen template.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'k3h8c6e29j5a'
down_revision: Union[str, Sequence[str], None] = 'j2g7b5d18i49'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'soil_range_templates',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('client_id', sa.String(length=36),
                  sa.ForeignKey('clients.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20),
                  nullable=False, server_default='ACTIVE'),
        sa.Column('created_by', sa.String(length=36),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('client_id', 'name',
                            name='uq_soil_range_template_client_name'),
    )
    op.create_index(
        'ix_soil_range_templates_client_id',
        'soil_range_templates', ['client_id'],
    )

    op.create_table(
        'soil_range_template_rows',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('template_id', sa.String(length=36),
                  sa.ForeignKey('soil_range_templates.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('parameter_id', sa.String(length=36),
                  sa.ForeignKey('platform_soil_parameters.id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('operator', sa.String(length=20), nullable=False),
        sa.Column('low_value', sa.Float(), nullable=True),
        sa.Column('high_value', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('template_id', 'parameter_id',
                            name='uq_soil_range_template_parameter'),
    )
    op.create_index(
        'ix_soil_range_template_rows_template_id',
        'soil_range_template_rows', ['template_id'],
    )

    # Nullable FK on packages — a Package without a template simply
    # has no bounds-classification (Package overrides may still carry
    # bounds + Decision V adjustments independently).
    op.add_column('packages', sa.Column(
        'soil_range_template_id', sa.String(length=36),
        sa.ForeignKey('soil_range_templates.id', ondelete='SET NULL'),
        nullable=True,
    ))
    op.create_index(
        'ix_packages_soil_range_template_id',
        'packages', ['soil_range_template_id'],
    )

    # Decision W consequence — package_normal_range_overrides.operator
    # becomes nullable so a Package can register a row with only
    # adjustments (and no bounds-override).
    op.alter_column(
        'package_normal_range_overrides', 'operator',
        existing_type=sa.String(length=20),
        nullable=True,
    )

    # Drop the old crop-level table and its index (if any).
    op.drop_table('client_normal_ranges')


def downgrade() -> None:
    # Recreate the dropped table (empty — original data wasn't
    # preserved per user instruction).
    op.create_table(
        'client_normal_ranges',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('client_id', sa.String(length=36),
                  sa.ForeignKey('clients.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('crop_cosh_id', sa.String(length=100), nullable=False),
        sa.Column('parameter_id', sa.String(length=36),
                  sa.ForeignKey('platform_soil_parameters.id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('operator', sa.String(length=20), nullable=False),
        sa.Column('low_value', sa.Float(), nullable=True),
        sa.Column('high_value', sa.Float(), nullable=True),
        sa.Column('low_pct_adjustment', sa.Float(),
                  nullable=False, server_default='0'),
        sa.Column('high_pct_adjustment', sa.Float(),
                  nullable=False, server_default='0'),
        sa.Column('created_by', sa.String(length=36),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('client_id', 'crop_cosh_id', 'parameter_id',
                            name='uq_client_crop_parameter_range'),
    )

    op.alter_column(
        'package_normal_range_overrides', 'operator',
        existing_type=sa.String(length=20),
        nullable=False,
    )

    op.drop_index('ix_packages_soil_range_template_id',
                  table_name='packages')
    op.drop_column('packages', 'soil_range_template_id')

    op.drop_index('ix_soil_range_template_rows_template_id',
                  table_name='soil_range_template_rows')
    op.drop_table('soil_range_template_rows')
    op.drop_index('ix_soil_range_templates_client_id',
                  table_name='soil_range_templates')
    op.drop_table('soil_range_templates')
