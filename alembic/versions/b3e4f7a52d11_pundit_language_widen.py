"""widen farm_pundit_languages.language_code to fit cosh UUIDs

Pre-Cosh-reshape this column held two-letter ISO codes ("en", "hi")
and was sized VARCHAR(10). The 2026-05-26 reshape repurposed it to
store the Cosh `pundit_languages` UUID (36 chars). VARCHAR(10) was
overflowing on every register submit (asyncpg
StringDataRightTruncationError → uvicorn 500 → PWA "Registration
failed.").

Bumping to VARCHAR(100) to match the sibling Cosh-id columns
(`crop_group_cosh_id`, `state_cosh_id`, etc.). Column name stays
`language_code` for minimum churn; future cleanup can rename it
to `language_cosh_id`.

Revision ID: b3e4f7a52d11
Revises: a1b9c2d4e5f6
Create Date: 2026-05-27
"""
from alembic import op
import sqlalchemy as sa


revision = "b3e4f7a52d11"
down_revision = "a1b9c2d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "farm_pundit_languages", "language_code",
        existing_type=sa.String(10),
        type_=sa.String(100),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "farm_pundit_languages", "language_code",
        existing_type=sa.String(100),
        type_=sa.String(10),
        existing_nullable=False,
    )
