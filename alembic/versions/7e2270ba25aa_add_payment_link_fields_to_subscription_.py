"""add payment link fields to subscription_payment_requests

Revision ID: 7e2270ba25aa
Revises: bf27c207ed07
Create Date: 2026-05-29 11:59:21.271716

V1.1 share-payment-link feature (2026-05-29): a farmer can generate
a Razorpay Payment Link / QR code and share it with anyone (e.g.
their son in a city). The recipient pays via any UPI app; Razorpay
fires a server-to-server webhook on success; our handler activates
the subscription.

The lifecycle reuses `subscription_payment_requests` rather than
introducing a parallel table — same PENDING → PAID | CANCELLED
arc, just a different `method`:

  DELEGATE   — existing flow: farmer asks a specific person; that
               person opens Razorpay checkout from inside the PWA.
  SHARE_LINK — new flow: farmer generates a Razorpay Payment Link;
               anyone with the link/QR can pay; webhook confirms.

`requested_from_user_id` becomes nullable so SHARE_LINK rows can
exist without a designated payer.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7e2270ba25aa'
down_revision: Union[str, Sequence[str], None] = 'bf27c207ed07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscription_payment_requests",
        sa.Column(
            "method", sa.String(length=20),
            nullable=False, server_default="DELEGATE",
        ),
    )
    op.add_column(
        "subscription_payment_requests",
        sa.Column("razorpay_payment_link_id", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "subscription_payment_requests",
        sa.Column("payment_link_short_url", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "subscription_payment_requests",
        sa.Column("paid_by_vpa", sa.String(length=100), nullable=True),
    )

    # requested_from_user_id → nullable. SHARE_LINK rows have no
    # designated payer.
    op.alter_column(
        "subscription_payment_requests", "requested_from_user_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "subscription_payment_requests", "requested_from_user_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.drop_column("subscription_payment_requests", "paid_by_vpa")
    op.drop_column("subscription_payment_requests", "payment_link_short_url")
    op.drop_column("subscription_payment_requests", "razorpay_payment_link_id")
    op.drop_column("subscription_payment_requests", "method")
