"""add_packing_list_packing_code

Revision ID: e8f4c205a917
Revises: d5b71429c0a3
Create Date: 2026-06-06

Adds packing_lists.packing_code — a 6-char paper-friendly identifier
(alphabet ABCDEFGHJKMNPQRSTUVWXYZ23456789; no 0/O/1/I/L) shared by
dealer, farmer, and facilitator to cross-reference a specific packing
batch. Generated on first creation of the PackingList row. Backfill
stamps codes on existing rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import secrets


revision: str = 'e8f4c205a917'
down_revision: Union[str, Sequence[str], None] = 'd5b71429c0a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def _gen() -> str:
    return ''.join(secrets.choice(_ALPHABET) for _ in range(6))


def upgrade() -> None:
    op.add_column(
        'packing_lists',
        sa.Column('packing_code', sa.String(length=12), nullable=True),
    )
    op.create_index(
        'ix_packing_lists_packing_code',
        'packing_lists', ['packing_code'], unique=True,
    )
    # Backfill in Python so the collision check uses live state.
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id FROM packing_lists WHERE packing_code IS NULL"))
    seen: set[str] = set(
        r[0] for r in conn.execute(sa.text(
            "SELECT packing_code FROM packing_lists WHERE packing_code IS NOT NULL"
        ))
    )
    for (pl_id,) in rows.fetchall():
        for _ in range(8):
            code = _gen()
            if code not in seen:
                seen.add(code)
                conn.execute(
                    sa.text("UPDATE packing_lists SET packing_code = :c WHERE id = :i"),
                    {"c": code, "i": pl_id},
                )
                break


def downgrade() -> None:
    op.drop_index('ix_packing_lists_packing_code', table_name='packing_lists')
    op.drop_column('packing_lists', 'packing_code')
