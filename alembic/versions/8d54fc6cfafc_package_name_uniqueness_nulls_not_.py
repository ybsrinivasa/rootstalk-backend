"""package name uniqueness — NULLS NOT DISTINCT + LOWER(name)

2026-05-22 — close two gaps in the existing partial unique index
`uq_package_client_crop_name_active`:

  1. **NULLS DISTINCT default**: Global Packages have `client_id IS
     NULL`. Postgres treats NULLs as distinct in unique indexes by
     default, so two Global rows with the same (NULL, crop, name)
     status=ACTIVE could coexist. Surfaced when testers found two
     `v1 ACTIVE` rows of `PkgTom2.0` in the version history of one
     Global package. Fixed via `NULLS NOT DISTINCT` (Postgres 15+).

  2. **Case-sensitive name match**: the old index treated "PkgTom2.0"
     and "pkgtom2.0" as different — matching the case-sensitive
     `Package.name == name` semantics but not the case-insensitive
     check in `_assert_package_name_available`. Fixed via
     `LOWER(name)` in the index expression.

The app-layer helper already enforces both rules at write time;
this migration aligns the DB backstop. Pre-existing duplicates (if
any) must be cleaned manually before the new index can be created —
the migration will fail loudly with a `duplicate key value` error
if data violates the new constraint.

Revision ID: 8d54fc6cfafc
Revises: d0259a1f00d0
Create Date: 2026-05-22 12:11:27.893740
"""
from typing import Sequence, Union

from alembic import op


revision: str = '8d54fc6cfafc'
down_revision: Union[str, Sequence[str], None] = 'd0259a1f00d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(
        "uq_package_client_crop_name_active",
        table_name="packages",
    )
    # Raw SQL for the partial expression index — alembic's
    # create_index doesn't expose NULLS NOT DISTINCT cleanly.
    op.execute(
        "CREATE UNIQUE INDEX uq_package_client_crop_name_active "
        "ON packages (client_id, crop_cosh_id, LOWER(name)) "
        "NULLS NOT DISTINCT "
        "WHERE status = 'ACTIVE'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_package_client_crop_name_active")
    op.execute(
        "CREATE UNIQUE INDEX uq_package_client_crop_name_active "
        "ON packages (client_id, crop_cosh_id, name) "
        "WHERE status = 'ACTIVE'"
    )
