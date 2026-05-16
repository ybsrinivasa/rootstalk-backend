"""Add is_brand_locked to practices (Batch 39I-a, 2026-05-16).

Per-Practice Brand Lock flag. Defaults to False; valid only when
the Practice carries a BRAND_NAME element (server-side validation).
Drives order routing and dealer-side picker semantics in 39I-b.

Revision ID: d7b3e4f1c2a8
Revises: c4d8e2f1a7b3
Create Date: 2026-05-16
"""
import sqlalchemy as sa
from alembic import op


revision = "d7b3e4f1c2a8"
down_revision = "c4d8e2f1a7b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "practices",
        sa.Column(
            "is_brand_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Drop the server_default once existing rows are populated; new
    # inserts go through the SQLAlchemy model default (False).
    op.alter_column("practices", "is_brand_locked", server_default=None)


def downgrade() -> None:
    op.drop_column("practices", "is_brand_locked")
