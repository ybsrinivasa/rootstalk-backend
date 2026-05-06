"""ClientCrop soft removal + cascade flag on Package

Revision ID: b5e91d4827af
Revises: a3d7f9c41e22
Create Date: 2026-05-06 17:00:00.000000

CCA Step 1 / Batch 1A — replaces hard-delete on
DELETE /client/{client_id}/crops/{crop_id} with a soft-removal flag.
On removal, every ACTIVE Package under that (client, crop) is
INACTIVATED and stamped with `cascade_inactivated_at` so a later
CA re-add can revive exactly those rows. DRAFT and
independently-INACTIVE Packages are left alone.

Existing rows: removed_at and cascade_inactivated_at are NULL,
i.e. all current crops are on the conveyor belt and no Package
was inactivated by a cascade.
"""
from alembic import op
import sqlalchemy as sa


revision = "b5e91d4827af"
down_revision = "a3d7f9c41e22"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "client_crops",
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "packages",
        sa.Column("cascade_inactivated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("packages", "cascade_inactivated_at")
    op.drop_column("client_crops", "removed_at")
