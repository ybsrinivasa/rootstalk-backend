"""farmer call logs — Farmer Support Phase 4

Revision ID: o7l2g0i63n9e
Revises: n6k1f9h52m8d
Create Date: 2026-11-10

New table `farmer_call_logs` for the CLIENT_RM's manually-logged
phone contact with each farmer. Design: Farmer Support doc §5.

Shape (locked 2026-11-10):
  - call_type: OUTBOUND | INBOUND (enum)
  - call_at: tz-aware timestamp of when the call happened (can be
    back-dated at log time per spec §5.3)
  - logged_by_user_id: the RM who logged — auto-filled from the
    authenticated user, not editable
  - No notes, no duration, no outcome, no updated_at. Entries are
  permanent; no edit or delete endpoint will be added.

Index: (client_id, farmer_user_id, call_at) covers the only read
pattern — the 360° page's chronological list for one farmer.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'o7l2g0i63n9e'
down_revision: Union[str, Sequence[str], None] = 'n6k1f9h52m8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    call_type = sa.Enum('OUTBOUND', 'INBOUND', name='call_type_enum')
    call_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        'farmer_call_logs',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column(
            'client_id', sa.String(length=36),
            sa.ForeignKey('clients.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'farmer_user_id', sa.String(length=36),
            sa.ForeignKey('users.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('call_type', call_type, nullable=False),
        sa.Column(
            'call_at', sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            'logged_by_user_id', sa.String(length=36),
            sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text('now()'),
        ),
    )
    op.create_index(
        'ix_farmer_call_logs_client_farmer_call_at', 'farmer_call_logs',
        ['client_id', 'farmer_user_id', 'call_at'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_farmer_call_logs_client_farmer_call_at',
        table_name='farmer_call_logs',
    )
    op.drop_table('farmer_call_logs')
    sa.Enum(name='call_type_enum').drop(op.get_bind(), checkfirst=True)
