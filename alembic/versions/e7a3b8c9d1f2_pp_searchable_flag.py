"""pp: client_farm_pundits.searchable

Revision ID: e7a3b8c9d1f2
Revises: d5e6f1a2b9c4
Create Date: 2026-05-30 10:00:00.000000

Promoter-Pundit V1 (2026-05-30) — phantom-pundit Option A. When a CA
designates a Facilitator-Promoter as a Promoter-Pundit, the toggle
endpoint auto-provisions a FarmPunditProfile + ClientFarmPundit if
the F-P doesn't already have one. The phantom row needs to be
hidden from any farmer-facing list (expert pickers, pundit search)
so the only way a farmer can choose a P-P is by typing their phone
number directly into the expert field.

`searchable` defaults to TRUE so every existing ClientFarmPundit row
keeps its current farmer-facing visibility unchanged. The phantom
rows created on PP toggle will be inserted with searchable=False
explicitly.

Additive + nullable-defaulted; code-only rollback is safe.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7a3b8c9d1f2'
down_revision: Union[str, Sequence[str], None] = 'd5e6f1a2b9c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "client_farm_pundits",
        sa.Column(
            "searchable", sa.Boolean(),
            nullable=False, server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("client_farm_pundits", "searchable")
