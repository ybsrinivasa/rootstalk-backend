"""client_feature_access — per-client time-bounded feature toggles.

Revision ID: z8w5r1t74y0p
Revises: y7v4q0s63x9o
Create Date: 2026-11-22

Creates the `client_feature_access` table. Append-only — one row per
activation / extension / turn-off per (client, feature). Current state
is the most recent row. Feature active now iff `access_until > now()`.

Readable from the SA Portal only (SA sets the flags, SA sees the
history). CA reads the derived state indirectly through feature
gating in CP endpoints.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "z8w5r1t74y0p"
down_revision: Union[str, Sequence[str], None] = "y7v4q0s63x9o"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(sa.text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'client_feature_access'
    """)).first()
    if exists:
        return
    op.create_table(
        "client_feature_access",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "client_id", sa.String(36),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feature", sa.String(40), nullable=False),
        sa.Column("access_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "activated_by_user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_cfa_client_feature_created",
        "client_feature_access", ["client_id", "feature", "created_at"],
    )
    op.create_index(
        "ix_cfa_access_until",
        "client_feature_access", ["access_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_cfa_access_until", table_name="client_feature_access")
    op.drop_index("ix_cfa_client_feature_created", table_name="client_feature_access")
    op.drop_table("client_feature_access")
