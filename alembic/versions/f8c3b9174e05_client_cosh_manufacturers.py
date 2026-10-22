"""client_cosh_manufacturers

Revision ID: f8c3b9174e05
Revises: e7b8c592d104
Create Date: 2026-10-17

Phase 6a of the Soil Nutrient module rework (Decision U) — Client's
linked Cosh Manufacturers as an M:N table.

Independent of `clients.cosh_manufacturer_id` (single value, added
2026-07-05 for QR Product Authentication). The QR flow stays on the
single column; the Soil Nutrient module uses this new M:N table. The
two coexist indefinitely.

Backfill: for every Client that currently has a non-NULL
`cosh_manufacturer_id`, insert one row into `client_cosh_manufacturers`
so the Soil Nutrient module's product picker sees at least the QR
Manufacturer as a linked value by default. SA can add more via the
Client edit modal.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f8c3b9174e05'
# prod-safe-soil-2026-10-22: chain Phase 6a onto soil Phase 4
# (d5d3f574e218) now that soil Phases 1–4 are re-rooted off the
# current prod coaching tip. On main the original value
# e7b8c592d104 stays; this edit is prod-safe-soil-only.
down_revision: Union[str, Sequence[str], None] = 'd5d3f574e218'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'client_cosh_manufacturers',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('client_id', sa.String(length=36),
                  sa.ForeignKey('clients.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('cosh_manufacturer_id', sa.String(length=200),
                  nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by', sa.String(length=36),
                  sa.ForeignKey('users.id', ondelete='SET NULL'),
                  nullable=True),
        sa.UniqueConstraint('client_id', 'cosh_manufacturer_id',
                            name='uq_client_cosh_manufacturer'),
    )
    op.create_index(
        'ix_client_cosh_manufacturers_client_id',
        'client_cosh_manufacturers', ['client_id'],
    )

    # Backfill from the existing single-column clients.cosh_manufacturer_id
    # — one row per Client that has a non-NULL value. UUIDs generated in
    # Python so we don't depend on a Postgres gen_random_uuid() extension.
    import uuid
    from datetime import datetime, timezone
    conn = op.get_bind()
    rows = conn.execute(sa.text("""
        SELECT id, cosh_manufacturer_id FROM clients
        WHERE cosh_manufacturer_id IS NOT NULL
    """)).all()
    if rows:
        now = datetime.now(timezone.utc)
        for client_id, mfr_id in rows:
            conn.execute(sa.text("""
                INSERT INTO client_cosh_manufacturers
                    (id, client_id, cosh_manufacturer_id, created_at, created_by)
                VALUES (:id, :cid, :mid, :now, NULL)
            """), {
                "id": str(uuid.uuid4()),
                "cid": client_id,
                "mid": mfr_id,
                "now": now,
            })


def downgrade() -> None:
    op.drop_index('ix_client_cosh_manufacturers_client_id',
                  table_name='client_cosh_manufacturers')
    op.drop_table('client_cosh_manufacturers')
