"""packages: relax name-unique index to ACTIVE-only

Revision ID: b2f9e3a814c7
Revises: b1d4e8f7c206
Create Date: 2026-05-11

Batch-1 used `WHERE status <> 'INACTIVE'` which inadvertently
blocked the common case of a PUBLISHED row + a sibling DRAFT for
the same (client, crop, name) — exactly what the SE-pull flow
produces (v1 PUBLISHED stays live while v2 DRAFT is being
reviewed). Both rows carry the same name (inherited from Global).

The actually-correct semantic is "at most one ACTIVE row per
(client, crop, name)". DRAFT and INACTIVE rows are not
name-constrained.

Single-DRAFT invariant is enforced in app code, not by this index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2f9e3a814c7'
down_revision: Union[str, Sequence[str], None] = 'b1d4e8f7c206'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('uq_package_client_crop_name_active', table_name='packages')
    op.create_index(
        'uq_package_client_crop_name_active',
        'packages',
        ['client_id', 'crop_cosh_id', 'name'],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index('uq_package_client_crop_name_active', table_name='packages')
    op.create_index(
        'uq_package_client_crop_name_active',
        'packages',
        ['client_id', 'crop_cosh_id', 'name'],
        unique=True,
        postgresql_where=sa.text("status <> 'INACTIVE'"),
    )
