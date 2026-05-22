"""wipe legacy client_organisation_types rows

2026-05-22 — the client-onboarding org-type checklist switched from
hardcoded slugs (`org_type_seed_companies`, `org_type_pesticide_mfr`,
…) to the live Cosh `organization_types` Core (UUIDs). Existing
`client_organisation_types` rows reference the legacy slugs and no
longer match any Cosh row; they would silently fail to surface in
the SE's profile and (worse) silently fail the Seed Company gate
even if the client was meant to keep that role.

User authorised seeder-data removal (2026-05-22): we wipe the
table so the SA can re-onboard / re-tag affected clients via the
new checklist. Cleaner than a guess-mapping that gets some clients
wrong.

Revision ID: 970d69856c1c
Revises: 8d54fc6cfafc
Create Date: 2026-05-22 14:19:25.542580
"""
from typing import Sequence, Union

from alembic import op


revision: str = '970d69856c1c'
down_revision: Union[str, Sequence[str], None] = '8d54fc6cfafc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # One-time wipe. New rows will use Cosh organization_types UUIDs
    # for org_type_cosh_id.
    op.execute("DELETE FROM client_organisation_types")


def downgrade() -> None:
    # Nothing to restore — the wiped rows are unrecoverable.
    # Downgrade is a no-op so it doesn't fail.
    pass
