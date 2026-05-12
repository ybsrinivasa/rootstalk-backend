"""parameters + variables: cosh_id columns for Cosh-side mirror

Revision ID: c3e1a8f4b277
Revises: b2f9e3a814c7
Create Date: 2026-05-12

User shipped the `crops_parameters_variables` Connect on
2026-05-12. RootsTalk now needs to surface those Cosh-sourced
parameters and variables in the PoP signature picker alongside
CM-authored Custom rows.

The mirror approach (locked 2026-05-12): for each unique
(crop_cosh_id, parameter_cosh_id) pair in the Connect, materialise
one local `parameters` row with `source=COSH` and `cosh_id` set
to the Cosh-side parameter UUID. Same pattern for `variables`
keyed on `(parameter_id, cosh_id)`.

The `cosh_id` columns are nullable — Custom (CM-authored) rows
leave them NULL; Cosh-mirrored rows set them. Partial unique
indexes prevent duplicate Cosh-mirror rows without constraining
the Custom flow.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3e1a8f4b277'
down_revision: Union[str, Sequence[str], None] = 'b2f9e3a814c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'parameters',
        sa.Column('cosh_id', sa.String(36), nullable=True),
    )
    op.add_column(
        'variables',
        sa.Column('cosh_id', sa.String(36), nullable=True),
    )
    # Partial unique: only enforced when cosh_id is set. A given
    # Cosh package_parameter UUID may appear once per crop on the
    # local side; a given Cosh package_variable UUID may appear
    # once per parameter.
    op.create_index(
        'uq_parameters_crop_cosh_id',
        'parameters',
        ['crop_cosh_id', 'cosh_id'],
        unique=True,
        postgresql_where=sa.text('cosh_id IS NOT NULL'),
    )
    op.create_index(
        'uq_variables_parameter_cosh_id',
        'variables',
        ['parameter_id', 'cosh_id'],
        unique=True,
        postgresql_where=sa.text('cosh_id IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_variables_parameter_cosh_id', table_name='variables')
    op.drop_index('uq_parameters_crop_cosh_id', table_name='parameters')
    op.drop_column('variables', 'cosh_id')
    op.drop_column('parameters', 'cosh_id')
