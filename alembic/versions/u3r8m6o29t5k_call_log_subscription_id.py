"""farmer_call_logs: add nullable subscription_id for per-sub attribution.

Revision ID: u3r8m6o29t5k
Revises: t2q7l5n18s4j
Create Date: 2026-11-16

Bundle B of the Farmer Support list redesign (2026-11-16). Calls
logged from a sub-detail page get stamped with that sub's id;
historical calls (pre-this-migration) stay unattached and remain
visible on every sub page as fallback.

Column is nullable so existing rows are valid without backfill.
ON DELETE SET NULL on the FK so a Subscription removal (soft-delete
guard already hides the row; this covers hard deletes too) leaves
the call log entry intact with subscription_id → NULL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "u3r8m6o29t5k"
down_revision: Union[str, Sequence[str], None] = "t2q7l5n18s4j"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'farmer_call_logs' AND column_name = 'subscription_id'
    """)).first()
    if not exists:
        op.add_column(
            "farmer_call_logs",
            sa.Column(
                "subscription_id", sa.String(length=36), nullable=True,
            ),
        )
        op.create_foreign_key(
            "fk_farmer_call_logs_subscription_id",
            "farmer_call_logs", "subscriptions",
            ["subscription_id"], ["id"],
            ondelete="SET NULL",
        )

    bind.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_farmer_call_logs_subscription_id
        ON farmer_call_logs (subscription_id)
        WHERE subscription_id IS NOT NULL
    """))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_farmer_call_logs_subscription_id"))
    op.drop_constraint(
        "fk_farmer_call_logs_subscription_id",
        "farmer_call_logs",
        type_="foreignkey",
    )
    op.drop_column("farmer_call_logs", "subscription_id")
