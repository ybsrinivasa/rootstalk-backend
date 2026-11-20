"""clients.query_expire_days — per-client query SLA override.

Revision ID: x6u3p9r52w8n
Revises: w5t2o8q41v7m
Create Date: 2026-11-20

Adds a nullable `query_expire_days` column on `clients`. NULL means
fall back to the global default (`QUERY_EXPIRE_DAYS = 2` in
app/modules/farmpundit/router.py). Set by the CA via
PUT /client/{cid}/settings/query-expiry, bounded 1..14. Only affects
NEW queries created after the change; existing in-flight queries
keep the `expires_at` stamped at create time.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "x6u3p9r52w8n"
down_revision: Union[str, Sequence[str], None] = "w5t2o8q41v7m"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'clients' AND column_name = 'query_expire_days'
    """)).first()
    if not exists:
        op.add_column(
            "clients",
            sa.Column("query_expire_days", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("clients", "query_expire_days")
