"""soil_nutrient_phase4_normal_ranges

Revision ID: d5d3f574e218
Revises: d4c2f574e217
Create Date: 2026-10-15

Phase 4 of the Soil Nutrient Management module — Normal Range
authoring (Decision N two-tier scope).

Two new tables:
  - client_normal_ranges — crop-level defaults per (client, crop,
    parameter). Authored in Portal → Normal Ranges by CA/SE/CM-EDIT.
    Baseline the engine classifies against when no Package override.
  - package_normal_range_overrides — sparse per-Package overrides,
    only parameters with meaningful tweaks have rows. Authored
    inside the Package editor's 'Override Normal Range' tab.

Resolution order at classification time: Package override → crop
default → not classified. Partial authoring (parameter blank)
tolerated throughout: engine simply doesn't classify unknown
parameters.

Operator shape stored as String(20); valid values enforced at the
service layer (NormalRangeOperator enum in app code).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5d3f574e218'
down_revision: Union[str, Sequence[str], None] = 'd4c2f574e217'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'client_normal_ranges',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('client_id', sa.String(length=36),
                  sa.ForeignKey('clients.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('crop_cosh_id', sa.String(length=100), nullable=False),
        sa.Column('parameter_id', sa.String(length=36),
                  sa.ForeignKey('platform_soil_parameters.id',
                                ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('operator', sa.String(length=20), nullable=False),
        sa.Column('low_value', sa.Float(), nullable=True),
        sa.Column('high_value', sa.Float(), nullable=True),
        sa.Column('created_by', sa.String(length=36),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('client_id', 'crop_cosh_id', 'parameter_id',
                            name='uq_client_crop_parameter_range'),
    )
    op.create_index(
        'ix_client_normal_ranges_client_crop',
        'client_normal_ranges', ['client_id', 'crop_cosh_id'],
    )

    op.create_table(
        'package_normal_range_overrides',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('package_id', sa.String(length=36),
                  sa.ForeignKey('packages.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('parameter_id', sa.String(length=36),
                  sa.ForeignKey('platform_soil_parameters.id',
                                ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('operator', sa.String(length=20), nullable=False),
        sa.Column('low_value', sa.Float(), nullable=True),
        sa.Column('high_value', sa.Float(), nullable=True),
        sa.Column('created_by', sa.String(length=36),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('package_id', 'parameter_id',
                            name='uq_package_parameter_override'),
    )
    op.create_index(
        'ix_package_normal_range_overrides_package_id',
        'package_normal_range_overrides', ['package_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_package_normal_range_overrides_package_id',
                  table_name='package_normal_range_overrides')
    op.drop_table('package_normal_range_overrides')
    op.drop_index('ix_client_normal_ranges_client_crop',
                  table_name='client_normal_ranges')
    op.drop_table('client_normal_ranges')
