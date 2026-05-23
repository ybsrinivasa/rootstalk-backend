"""standard_responses.status — DRAFT/ACTIVE/INACTIVE lifecycle

CA-QA polish (2026-05-23). The Q&A library now has the same
publish-then-toggle lifecycle the user already understands from the
CCA/PG/SP pipes, minus version history (edits to an ACTIVE row
propagate immediately — the curator uses the Inactive toggle as the
hide affordance during rewrites). Only ACTIVE rows are eligible for
Pundits to pick when responding to a farmer query.

Backfill: existing rows are set to ACTIVE because pre-lifecycle the
Pundit-facing endpoint returned every row. Switching them to DRAFT
would silently remove every already-live SR from the Pundit list.

Revision ID: c7e3a91d2058
Revises: 970d69856c1c
Create Date: 2026-05-23 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7e3a91d2058"
down_revision: Union[str, Sequence[str], None] = "970d69856c1c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default="ACTIVE" fills existing rows in a single statement.
    # Dropping the server_default afterwards leaves new (ORM-created)
    # rows on the Python-side default in models.py (DRAFT).
    op.add_column(
        "standard_responses",
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="ACTIVE",
        ),
    )
    op.alter_column("standard_responses", "status", server_default=None)


def downgrade() -> None:
    op.drop_column("standard_responses", "status")
