"""Add fcm_token to users

Revision ID: a3d7f9c41e22
Revises: f6a12b8c4d97
Create Date: 2026-05-06 12:00:00.000000

FCM Batch 1 — single-device push-notification token storage on the
User row. PWA registers the device's FCM token via
POST /platform/fcm-token; the BL-09 alerts task / BL-12 query expiry
task / BL-14 facilitator-approval prompt then read this column to
push notifications. Multi-device support deferred to V2.
"""
from alembic import op
import sqlalchemy as sa


revision = "a3d7f9c41e22"
down_revision = "f6a12b8c4d97"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("fcm_token", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("users", "fcm_token")
