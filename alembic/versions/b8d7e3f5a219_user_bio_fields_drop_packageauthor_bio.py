"""Move designation + professional_profile from PackageAuthor to User.

Per user 2026-05-18: these fields belong on the User itself (captured
once at Subject Expert creation in User Management) — not per-package.
The PWA reads them via JOIN at advisory render time.

Per-package storage was Batch CA-Welcome / spec §4.1 legacy; the
fields were never read by the PWA. Existing values (if any) are
seed/test data per the user — no data migration needed.

Revision ID: b8d7e3f5a219
Revises: a4f9c2d1b502
Create Date: 2026-05-18

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "b8d7e3f5a219"
down_revision = "a4f9c2d1b502"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("designation", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("professional_profile", sa.Text(), nullable=True),
    )
    op.drop_column("package_authors", "designation")
    op.drop_column("package_authors", "professional_profile")


def downgrade() -> None:
    op.add_column(
        "package_authors",
        sa.Column("professional_profile", sa.Text(), nullable=True),
    )
    op.add_column(
        "package_authors",
        sa.Column("designation", sa.String(length=255), nullable=True),
    )
    op.drop_column("users", "professional_profile")
    op.drop_column("users", "designation")
