"""diagnosis_sessions.committed_at — opt-in CHA trigger

Per user direction 2026-05-25: the CHA trigger that brings the
diagnosed problem's treatment recommendations into the farmer's
advisory now fires only when the farmer explicitly taps the
"Add Treatment Recommendations to the Advisory" CTA on the
diagnosed screen. The new column records when that commit
happened so the trigger is idempotent against double-tap.

Existing rows (auto-committed under the old behaviour) are NOT
backfilled — they predate the opt-in rule, so leaving committed_at
NULL is the most honest representation. The endpoint's idempotency
guard reads from this column going forward.

Revision ID: d8f1a4c92e51
Revises: c7e3a91d2058
Create Date: 2026-05-25 21:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8f1a4c92e51"
down_revision: Union[str, Sequence[str], None] = "c7e3a91d2058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "diagnosis_sessions",
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("diagnosis_sessions", "committed_at")
