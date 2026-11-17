"""pundit_exclusivity_agreements — CA↔Pundit exclusivity opt-in.

Revision ID: w5t2o8q41v7m
Revises: v4s9n7p30u6l
Create Date: 2026-11-17

Lets a Client's CA request that a Primary/Panel Pundit serve them
exclusively; Pundit accepts or declines via their PWA. Once ACTIVE,
the Pundit is filtered out of every other CA Portal's pundit search
(freeze semantics — existing ClientFarmPundit rows at other clients
continue to serve their farmers; new onboardings get refused).

Two partial unique indexes enforce the business rules:
  - One ACTIVE agreement per Pundit globally (enforces "single-client
    exclusivity")
  - One PENDING request per Pundit+Client (prevents duplicate CA
    taps spawning parallel requests)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "w5t2o8q41v7m"
down_revision: Union[str, Sequence[str], None] = "v4s9n7p30u6l"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(sa.text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'pundit_exclusivity_agreements'
    """)).first()
    if exists:
        return

    op.create_table(
        "pundit_exclusivity_agreements",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "pundit_profile_id", sa.String(length=36), nullable=False,
        ),
        sa.Column(
            "client_id", sa.String(length=36), nullable=False,
        ),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "requested_by_user_id", sa.String(length=36), nullable=False,
        ),
        sa.Column(
            "responded_by_user_id", sa.String(length=36), nullable=True,
        ),
        sa.Column(
            "ended_by_user_id", sa.String(length=36), nullable=True,
        ),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.Column(
            "pundit_end_requested_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["pundit_profile_id"], ["farm_pundit_profiles.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["responded_by_user_id"], ["users.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["ended_by_user_id"], ["users.id"], ondelete="SET NULL",
        ),
    )

    # One ACTIVE per Pundit (enforces "single-client exclusivity")
    bind.execute(sa.text("""
        CREATE UNIQUE INDEX uq_pundit_exclusivity_one_active
        ON pundit_exclusivity_agreements (pundit_profile_id)
        WHERE status = 'ACTIVE'
    """))
    # One PENDING per (Pundit, Client) — prevents duplicate CA taps
    bind.execute(sa.text("""
        CREATE UNIQUE INDEX uq_pundit_exclusivity_one_pending_per_client
        ON pundit_exclusivity_agreements (pundit_profile_id, client_id)
        WHERE status = 'PENDING_PUNDIT_APPROVAL'
    """))
    # Lookup index for the auto-approve celery task + CA inbox
    bind.execute(sa.text("""
        CREATE INDEX ix_pundit_exclusivity_end_requested_at
        ON pundit_exclusivity_agreements (pundit_end_requested_at)
        WHERE pundit_end_requested_at IS NOT NULL AND status = 'ACTIVE'
    """))


def downgrade() -> None:
    op.drop_table("pundit_exclusivity_agreements")
