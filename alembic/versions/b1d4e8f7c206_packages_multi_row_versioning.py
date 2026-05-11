"""packages: multi-row versioning columns + relax name-uniqueness

Revision ID: b1d4e8f7c206
Revises: c8e9d2f4a5b6
Create Date: 2026-05-11

Locks the multi-row Local-side versioning model (see
project_rootstalk_global_to_local_pipe.md, 2026-05-11):

  - `Package.created_via` enum records the origin of each row
    (CM push / SE pull / SE-edit clone-to-draft / SE rollback
    republish). Lets the editor distinguish "Pulled v4" from
    "Self v3" in the history view.

  - `Package.source_version_id` FK is the lineage pointer for
    rollback-republishes: when SE picks a historical row and
    publishes its content as a new version, the new row records
    which historical row it came from. NULL for ordinary forward
    cycles.

  - The `uq_package_client_crop_name` unique constraint goes —
    it would block multiple INACTIVE history rows with the same
    name. Replaced by a *partial* unique index that only fires
    on non-INACTIVE rows (so DRAFT + PUBLISHED siblings still
    can't collide on name within the same client+crop, but
    INACTIVE history is unconstrained).

  - New composite index `(client_id, parent_global_id, status)`
    makes the push-status check + "does this client already have
    this Global Package?" lookups O(log n) regardless of how
    much history accumulates.

BL-13 superseded note: today's publish endpoint flips DRAFT →
ACTIVE on the same row. This migration is purely additive —
column adds don't break the in-place publish path. The endpoint
rewrite (Batch 3) is what actually creates the multi-row history.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1d4e8f7c206'
down_revision: Union[str, Sequence[str], None] = 'c8e9d2f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CREATED_VIA_ENUM = sa.Enum(
    'CM_PUSH', 'SE_PULL_DRAFT', 'SE_EDIT_DRAFT', 'SE_ROLLBACK_PUBLISH',
    name='packagecreatedvia',
)


def upgrade() -> None:
    bind = op.get_bind()
    CREATED_VIA_ENUM.create(bind, checkfirst=True)

    op.add_column(
        'packages',
        sa.Column('created_via', CREATED_VIA_ENUM, nullable=True),
    )
    op.add_column(
        'packages',
        sa.Column(
            'source_version_id', sa.String(36),
            sa.ForeignKey('packages.id'), nullable=True,
        ),
    )

    op.drop_constraint('uq_package_client_crop_name', 'packages', type_='unique')
    op.create_index(
        'uq_package_client_crop_name_active',
        'packages',
        ['client_id', 'crop_cosh_id', 'name'],
        unique=True,
        postgresql_where=sa.text("status <> 'INACTIVE'"),
    )

    op.create_index(
        'ix_packages_client_parent_status',
        'packages',
        ['client_id', 'parent_global_id', 'status'],
    )


def downgrade() -> None:
    op.drop_index('ix_packages_client_parent_status', table_name='packages')
    op.drop_index('uq_package_client_crop_name_active', table_name='packages')
    op.create_unique_constraint(
        'uq_package_client_crop_name', 'packages',
        ['client_id', 'crop_cosh_id', 'name'],
    )
    op.drop_column('packages', 'source_version_id')
    op.drop_column('packages', 'created_via')

    bind = op.get_bind()
    CREATED_VIA_ENUM.drop(bind, checkfirst=True)
