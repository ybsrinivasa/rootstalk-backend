"""User session and deleted_at columns

Revision ID: c7b3a91e4d22
Revises: a9f3e1c82b44
Create Date: 2026-05-02 18:00:00.000000

Adds:
- users.current_session_id  VARCHAR(36) — single-device enforcement (JWT jti)
- users.deleted_at           TIMESTAMPTZ — 30-day grace deletion marker
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'c7b3a91e4d22'
down_revision: Union[str, Sequence[str], None] = 'a9f3e1c82b44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("current_session_id", sa.String(36), nullable=True))
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "deleted_at")
    op.drop_column("users", "current_session_id")
