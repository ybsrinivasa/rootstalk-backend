"""Query.affected_plants_count — optional at query submission.

Revision ID: d7f4c8b2e919
Revises: c2a91e7d4f88
Create Date: 2026-06-30

Background:
For plant-wise crops the query submission form now carries an
optional "How many of your N plants are affected?" field. If the
farmer fills it in, the count propagates into the QA-triggered
TriggeredCHAEntry when the pundit picks a Standard Response that
fires a CHA. If they leave it blank, the field stays NULL —
intentional: at query time the system can't tell if the query is
about a pest at all, so we don't insist. The dealer's order surface
shows "Please check with the farmer" for items where the count is
absent.

This column doesn't need an index — read paths fetch the row by id
and reads of `affected_plants_count` are always single-row hops.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7f4c8b2e919"
down_revision: Union[str, Sequence[str], None] = "c2a91e7d4f88"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "queries",
        sa.Column(
            "affected_plants_count",
            sa.Integer(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("queries", "affected_plants_count")
