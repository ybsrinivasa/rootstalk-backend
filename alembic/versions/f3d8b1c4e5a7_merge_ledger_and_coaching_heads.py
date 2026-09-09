"""merge_ledger_and_coaching_heads

Revision ID: f3d8b1c4e5a7
Revises: a2c8d5e91b34, e2a5c7b3f091
Create Date: 2026-09-09

Empty merge — unifies the two open alembic heads so
`alembic upgrade head` (singular) can resolve. See the two parents:

- a2c8d5e91b34 — coaching_certificate_fields (coaching sandbox line)
- e2a5c7b3f091 — dealer_farmer_ledger (Farmer Ledger v1 line)

No schema changes. Adding this now because the staging deploy script
uses `alembic upgrade head` (singular) and refuses when there are
multiple heads.
"""
from typing import Sequence, Union


revision: str = 'f3d8b1c4e5a7'
down_revision: Union[str, Sequence[str], None] = ('a2c8d5e91b34', 'e2a5c7b3f091')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
