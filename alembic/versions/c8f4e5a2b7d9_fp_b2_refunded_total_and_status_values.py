"""F-P B2: refunded_total + new AssignmentStatus values

Revision ID: c8f4e5a2b7d9
Revises: 7e2270ba25aa
Create Date: 2026-05-29 17:00:00.000000

F-P Assign-Package-to-Farmer write-side (2026-05-29). Three refund
paths agreed in the design lock (farmer reject / 72h auto-expire /
F-P self-cancel) all credit one unit back to the promoter's kitty
via a new `refund_to_promoter` service. Audit-side we add a
`refunded_total` running total alongside the existing
`allocated_total` / `reclaimed_total` / `consumed_total` so
`consumed_total` keeps its meaning as the ever-consumed count.

AssignmentStatus is a String(30) column (not a PG enum), so the
new values `EXPIRED` and `CANCELLED_BY_PROMOTER` need no schema
change — the Python enum extension in models.py is sufficient.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8f4e5a2b7d9'
down_revision: Union[str, Sequence[str], None] = '7e2270ba25aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "promoter_allocations",
        sa.Column(
            "refunded_total", sa.Integer(),
            nullable=False, server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("promoter_allocations", "refunded_total")
