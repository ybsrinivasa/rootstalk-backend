"""Client.cosh_manufacturer_id — deterministic link to Cosh input_manufacturers.

Revision ID: a4c8e2f6d1b3
Revises: f8b6d10c2e75
Create Date: 2026-07-05

Background:
The QR Product Authentication module surfaces a Brand Portfolio for
manufacturer clients. Previously the CA had to type "manufacturer
name as it appears in Cosh" — fragile: the CA can't know how Cosh
stored their company. This column replaces the guessing game with
a deterministic link the SA sets at approval time via a
searchable dropdown of every active `input_manufacturers` Cosh Core
row.

Only meaningful when is_manufacturer=True (backend guard on
edit_client refuses the pair). Independent of the seed-flavour
axis (seed varieties come from RootsTalk, not Cosh).

No backfill: existing manufacturer clients land with NULL and the
SA links them one-by-one from the SA portal Client detail modal.
Additive nullable, code-only rollback safe.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4c8e2f6d1b3"
down_revision: Union[str, Sequence[str], None] = "f8b6d10c2e75"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column(
            "cosh_manufacturer_id", sa.String(length=200), nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("clients", "cosh_manufacturer_id")
