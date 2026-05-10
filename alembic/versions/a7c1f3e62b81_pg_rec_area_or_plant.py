"""pg_recommendations: drop application_type, add area_or_plant

Revision ID: a7c1f3e62b81
Revises: 5c9d2e8b1a44
Create Date: 2026-05-10

CHA hub Round 1: PGRecommendation models the SE's authoring unit
("two bundles per PG — one for area-wise crops, one for plant-wise").
The user's framing 2026-05-10:
  • PG is crop-agnostic; the discriminator is area_or_plant.
  • Each (client, problem_group, area_or_plant) is its own bundle —
    own DRAFT/ACTIVE status, own version, own import flow.

The legacy `application_type` column ('SPRAY' / 'DRENCH' / 'SOIL') was
never used in V1 logic and is being dropped to keep the model clean.
If the team needs application-method tagging later, it lives at the
Practice / Element layer (where it already does for CCA), not at the
Recommendation layer.

Forward path: drop application_type, add area_or_plant nullable. The
sweep of route + factory + test code is in the same commit; no live
PG data on the testing server today (no published PG recs), so no
backfill script.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7c1f3e62b81'
down_revision: Union[str, Sequence[str], None] = '5c9d2e8b1a44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'pg_recommendations',
        sa.Column('area_or_plant', sa.String(20), nullable=True),
    )
    op.drop_column('pg_recommendations', 'application_type')


def downgrade() -> None:
    op.add_column(
        'pg_recommendations',
        sa.Column('application_type', sa.String(20), nullable=True),
    )
    op.drop_column('pg_recommendations', 'area_or_plant')
