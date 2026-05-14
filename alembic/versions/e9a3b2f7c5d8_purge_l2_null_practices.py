"""Purge shell Practices (l2_type IS NULL)

Revision ID: e9a3b2f7c5d8
Revises: d4f8a2c1e7b3
Create Date: 2026-05-14 17:30:00.000000

Batch 30 cleanup, per user 2026-05-14. Pre-Batch-30 the Add Practice
modal could submit without an L2 picked, leaving rows like
`l0=INPUT, l1=NULL, l2=NULL` which surface in the UI as "INPUT ·
No sub-type". These Practices carry no element spec, so they're
functionally empty.

Going forward, the create handlers reject `l2_type IS NULL` with
422 l2_type_required; this migration drops any pre-existing
shells. Cascades the small dependents first (elements,
conditional_questions, relation_conditionals) defensively even
though shell Practices never had any. Down-migration is a no-op:
the rows being deleted were never useful and recreating them would
be incorrect.
"""
from alembic import op


revision = "e9a3b2f7c5d8"
down_revision = "d4f8a2c1e7b3"
branch_labels = None
depends_on = None


def upgrade():
    # Elements that hang off shell practices.
    op.execute(
        "DELETE FROM elements WHERE practice_id IN "
        "(SELECT id FROM practices WHERE l2_type IS NULL)"
    )
    # Per-practice conditionals attached to shell practices.
    # (NB: relation_conditionals binds to relations, not practices;
    # conditional_questions binds to timelines, not practices.)
    op.execute(
        "DELETE FROM practice_conditionals WHERE practice_id IN "
        "(SELECT id FROM practices WHERE l2_type IS NULL)"
    )
    # The shells themselves.
    op.execute("DELETE FROM practices WHERE l2_type IS NULL")


def downgrade():
    # Shell Practices had no useful content. Re-creating them would
    # be incorrect; this migration is intentionally not reversible.
    pass
