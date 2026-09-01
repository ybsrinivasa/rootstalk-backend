"""Coaching Sandbox — certificate number + generated_at + pdf_url fields.

Revision ID: a2c8d5e91b34
Revises: f9c1b3a04e51
Create Date: 2026-09-01

Adds three fields to `coaching_students` for the digital-certificate
feature (Phase 6b):

  - `certificate_number` — UUID (as String(36)), unique when set.
    Generated on first cert issue. Doubles as the public verification
    slug (`/verify/<cert_number>` on the client-portal). Nullable so
    a certified student without a generated certificate can exist
    briefly between certify + generate.
  - `certificate_generated_at` — timestamp of the most recent PDF
    generation. Regenerating (e.g. grade updated) refreshes this.
  - `certificate_pdf_url` — S3 URL of the generated PDF. Nullable
    until first generation.

All three are only meaningful when the student is certified
(`certified_at IS NOT NULL AND grade IS NOT NULL`) — enforced at the
app layer, not with a DB CHECK.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2c8d5e91b34"
down_revision: Union[str, Sequence[str], None] = "f9c1b3a04e51"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "coaching_students",
        sa.Column(
            "certificate_number", sa.String(length=36),
            nullable=True, unique=True,
        ),
    )
    op.add_column(
        "coaching_students",
        sa.Column("certificate_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "coaching_students",
        sa.Column("certificate_pdf_url", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("coaching_students", "certificate_pdf_url")
    op.drop_column("coaching_students", "certificate_generated_at")
    op.drop_column("coaching_students", "certificate_number")
