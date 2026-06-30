"""TriggeredCHAEntry.affected_plants_count — per-event count of damaged plants.

Revision ID: c2a91e7d4f88
Revises: d8e4f721c350
Create Date: 2026-06-30

Background:
For plant-wise crops (coconut, areca, banana, etc.) the farmer is now
prompted at diagnosis-acceptance time: "How many of your N plants are
affected?" That count, captured per `TriggeredCHAEntry`, drives the
total volume of inputs the farmer needs to purchase — per-plant
dosage is unchanged; what shifts is `Count` in the BL-06 volume
formula. Without this, a coconut farmer with 200 palms and 10
infested ones would pay for treatment sized for 200 — exactly the
"waste of money and resources" the principle was added to avoid.

Scope:
- PG and SP paths capture the count via the mandatory PWA prompt that
  fires after `commit-to-advisory`. Validate `1 ≤ n ≤
  subscription.number_of_plants`.
- QA path leaves this column NULL — the pundit doesn't know the count
  and the farmer isn't in the loop at QA-trigger time. The dealer
  surface shows a "Please check with the farmer" hint for those items
  and the dealer enters volume manually.
- No backfill for in-flight CHA timelines created before this column
  existed — they continue to fall back to NULL, which lets the dealer
  surface route them through the same manual-volume path.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2a91e7d4f88"
down_revision: Union[str, Sequence[str], None] = "d8e4f721c350"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "triggered_cha_entries",
        sa.Column(
            "affected_plants_count",
            sa.Integer(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("triggered_cha_entries", "affected_plants_count")
