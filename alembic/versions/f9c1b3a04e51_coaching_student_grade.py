"""Coaching Sandbox — student certification grade.

Revision ID: f9c1b3a04e51
Revises: e7b1c4a09d52
Create Date: 2026-09-01

Adds a graduated certification grade to CoachingStudent. Design
decision locked with user 2026-09-01: four states —
  - NULL: not certified (default; matches pre-existing behaviour)
  - SATISFACTORY: certified, met the bar
  - GOOD: certified, exceeded expectations
  - EXCELLENT: certified, outstanding

Only rows with a non-NULL grade AND non-NULL certified_at are
considered certified. The service layer will enforce this
invariant on writes (grade is set alongside certified_at). No
database CHECK constraint here — the app-layer coupling keeps
the migration additive and reversible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9c1b3a04e51"
down_revision: Union[str, Sequence[str], None] = "e7b1c4a09d52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "coaching_students",
        sa.Column("grade", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("coaching_students", "grade")
