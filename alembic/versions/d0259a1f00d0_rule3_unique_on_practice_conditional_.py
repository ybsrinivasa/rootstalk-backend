"""rule3 unique on practice_conditional + relation_conditional

2026-05-22 — Conditional Question attachment exclusivity (Rule 3) is
enforced today by app-layer code in `link_practice_conditional` /
`link_relation_conditional`, but the database has no constraint. A
direct API or future bug could attach the same Practice to two CQs,
or to both YES and NO branches of a single CQ, doubling the logic
the farmer sees. This migration adds DB-level uniqueness so the
exclusivity is a structural invariant, not a code review item.

Constraints added:
  • practice_conditionals.practice_id — UNIQUE. A Practice can be
    linked to AT MOST one Conditional Question, regardless of answer.
    Covers Rule 3a (no Yes + No on same CQ) and Rule 3b (no second CQ).
  • relation_conditionals.relation_id — UNIQUE. Mirror constraint for
    Relations. Supersedes the existing UniqueConstraint("relation_id",
    "question_id"), which only prevented dupes of the SAME pair.

Defensive: this migration assumes no current data violates the new
constraints. The app layer rejected violations at write time since
Batch 4B (2026-05-07), so any existing duplicates would be the
result of a code bypass. The pre-flight DELETE blocks would surface
in alembic upgrade as a clear UniqueViolation if any slipped through
— operator can then audit + clean manually.

Revision ID: d0259a1f00d0
Revises: 4495968b29d9
Create Date: 2026-05-22 08:02:03.126780
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'd0259a1f00d0'
down_revision: Union[str, Sequence[str], None] = '4495968b29d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add UNIQUE constraint on practice_id (practice_conditionals) and
    relation_id (relation_conditionals). Drop the old narrower
    (relation_id, question_id) pair on relation_conditionals — the new
    constraint subsumes it."""
    op.create_unique_constraint(
        "uq_practice_conditionals_practice_id",
        "practice_conditionals",
        ["practice_id"],
    )
    op.drop_constraint(
        "uq_relation_conditional_relation_question",
        "relation_conditionals",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_relation_conditionals_relation_id",
        "relation_conditionals",
        ["relation_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_relation_conditionals_relation_id",
        "relation_conditionals",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_relation_conditional_relation_question",
        "relation_conditionals",
        ["relation_id", "question_id"],
    )
    op.drop_constraint(
        "uq_practice_conditionals_practice_id",
        "practice_conditionals",
        type_="unique",
    )
