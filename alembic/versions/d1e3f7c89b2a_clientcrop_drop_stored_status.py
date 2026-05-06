"""Drop stored ClientCrop.status — derived from PoPs now

Revision ID: d1e3f7c89b2a
Revises: c0e2bf1da3a4
Create Date: 2026-05-06 19:00:00.000000

CCA Step 1 / Batch 1D — closes findings 3 and 6 of the CCA Step 1
audit. Per spec, a crop's active/inactive state is derived from
its Packages: ACTIVE iff at least one PoP under it is ACTIVE. The
stored `client_crops.status` column is dead weight that would only
drift the moment any code path wrote to it. Drop it.

The list endpoint emits `is_active` (boolean) and a derived
`status` ("ACTIVE" / "INACTIVE") for portal compat, both computed
in the router.

DEPLOYMENT ORDER NOTE: deploy code first, run migration second.
The new code does not read `status` from this table; old code
would error on a missing column. Reverse order would briefly break
the API. (For dev/staging where rollouts are atomic this doesn't
matter — flagged for prod.)
"""
from alembic import op
import sqlalchemy as sa


revision = "d1e3f7c89b2a"
down_revision = "c0e2bf1da3a4"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("client_crops", "status")


def downgrade():
    op.add_column(
        "client_crops",
        sa.Column(
            "status", sa.String(length=20),
            nullable=False, server_default="ACTIVE",
        ),
    )
