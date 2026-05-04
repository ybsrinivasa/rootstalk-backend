"""subscription_pools payment audit columns (Phase B Razorpay)

Revision ID: d5e7b1a93f28
Revises: c4f9a8d12e63
Create Date: 2026-05-04 14:00:00.000000

Adds payment-trail columns to subscription_pools so every CA pool top-up
carries a permanent record of the Razorpay order, payment, amount paid,
and the user who initiated it. Required for the Phase B payment flow —
new pool rows are now created only after a successful Razorpay
verification.

All columns nullable so legacy rows (created via the now-removed free
purchase endpoint) are preserved without backfill.
"""
from alembic import op
import sqlalchemy as sa


revision = "d5e7b1a93f28"
down_revision = "c4f9a8d12e63"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "subscription_pools",
        sa.Column("razorpay_order_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "subscription_pools",
        sa.Column("razorpay_payment_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "subscription_pools",
        sa.Column("amount_paid_paise", sa.Integer, nullable=True),
    )
    op.add_column(
        "subscription_pools",
        sa.Column("purchased_by_user_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscription_pools_purchased_by_user_id",
        "subscription_pools",
        "users",
        ["purchased_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_subscription_pools_razorpay_order_id",
        "subscription_pools",
        ["razorpay_order_id"],
        unique=True,
        postgresql_where=sa.text("razorpay_order_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index(
        "ix_subscription_pools_razorpay_order_id",
        table_name="subscription_pools",
    )
    op.drop_constraint(
        "fk_subscription_pools_purchased_by_user_id",
        "subscription_pools", type_="foreignkey",
    )
    op.drop_column("subscription_pools", "purchased_by_user_id")
    op.drop_column("subscription_pools", "amount_paid_paise")
    op.drop_column("subscription_pools", "razorpay_payment_id")
    op.drop_column("subscription_pools", "razorpay_order_id")
