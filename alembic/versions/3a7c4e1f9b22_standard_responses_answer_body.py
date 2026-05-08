"""standard_responses: add answer_text + answer_media + updated_at

Revision ID: 3a7c4e1f9b22
Revises: 250e9b0d3abc
Create Date: 2026-05-09

L4 of the Facilitator/Dealer/FarmPundit audit. Spec §14.9 defines a
standard Q&A library where Subject Experts curate question/answer
pairs that FarmPundits can pick when responding to farmer queries.
Pre-fix the model only had `question_text` — no answer body — so
the library could store questions but not answers, and there was
no UI surface either.

V1 answer body is text + media (JSON list of {media_type, url,
caption?}). The spec also allows embedding full Timelines /
Practices / Elements (same structure as a CHA plan); that's
deferred to V1.1 because it's significantly heavier integration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3a7c4e1f9b22'
down_revision: Union[str, Sequence[str], None] = '250e9b0d3abc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'standard_responses',
        sa.Column('answer_text', sa.Text(), nullable=True),
    )
    op.add_column(
        'standard_responses',
        sa.Column('answer_media', sa.JSON(), nullable=True),
    )
    op.add_column(
        'standard_responses',
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('standard_responses', 'updated_at')
    op.drop_column('standard_responses', 'answer_media')
    op.drop_column('standard_responses', 'answer_text')
