"""Add relation_conditionals table (CCA Step 4 / Batch 4B Path A)

Revision ID: e7a2b8d4f193
Revises: d1e3f7c89b2a
Create Date: 2026-05-07 14:00:00.000000

When a Practice is part of a saved Relation, conditional links bind
to the Relation rather than the individual Practice (spec §6.4 +
user clarification 2026-05-07). PracticeConditional remains in use
for independent practices.

Path A introduces a new `relation_conditionals` table with a unique
(relation_id, question_id) constraint mirroring the structure of
practice_conditionals.
"""
from alembic import op
import sqlalchemy as sa


revision = "e7a2b8d4f193"
down_revision = "d1e3f7c89b2a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "relation_conditionals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "relation_id", sa.String(length=36),
            sa.ForeignKey("relations.id"), nullable=False,
        ),
        sa.Column(
            "question_id", sa.String(length=36),
            sa.ForeignKey("conditional_questions.id"), nullable=False,
        ),
        sa.Column("answer", sa.String(length=10), nullable=False),
        sa.UniqueConstraint(
            "relation_id", "question_id",
            name="uq_relation_conditional_relation_question",
        ),
    )


def downgrade():
    op.drop_table("relation_conditionals")
