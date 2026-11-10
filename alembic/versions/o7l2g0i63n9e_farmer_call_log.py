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
    # Create the enum defensively — a half-run of this migration on
    # staging 2026-11-10 left the enum created but the table missing;
    # SQLAlchemy's `checkfirst=True` on the Enum does not always
    # suppress CREATE TYPE in alembic contexts. Wrap in DO/EXCEPTION
    # so re-running is a no-op if the enum already exists.
    op.execute("""
    DO $$ BEGIN
        CREATE TYPE call_type_enum AS ENUM ('OUTBOUND', 'INBOUND');
    EXCEPTION
        WHEN duplicate_object THEN null;
    END $$;
    """)

    # Tell SQLAlchemy NOT to attempt CREATE TYPE again inside
    # create_table (we already did it above, or it already existed).
    from sqlalchemy.dialects import postgresql
    call_type = postgresql.ENUM(
        'OUTBOUND', 'INBOUND', name='call_type_enum', create_type=False,
    )

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
    op.execute("DROP TYPE IF EXISTS call_type_enum")
