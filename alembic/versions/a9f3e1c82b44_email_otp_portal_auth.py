"""email_otp_portal_auth

Revision ID: a9f3e1c82b44
Revises: e3a7f2c91d55
Create Date: 2026-05-02 16:00:00.000000

Adds email_otps table for portal (Admin + Client Portal) email OTP login
and password reset flow.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'a9f3e1c82b44'
down_revision: Union[str, Sequence[str], None] = 'e3a7f2c91d55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'email_otps',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('otp_code', sa.String(6), nullable=False),
        sa.Column('purpose', sa.String(20), nullable=False, server_default='LOGIN'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used', sa.Boolean, nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_email_otps_email', 'email_otps', ['email'])


def downgrade() -> None:
    op.drop_index('ix_email_otps_email', 'email_otps')
    op.drop_table('email_otps')
