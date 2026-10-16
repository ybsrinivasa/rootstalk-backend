"""coaching_workspace_snapshot_url

Revision ID: e7b8c592d104
Revises: e6a7b4c13f29
Create Date: 2026-10-16

Adds workspace_snapshot_pdf_url to coaching_students. Set at
certification time (same moment the certificate PDF is generated).
Mirror of certificate_pdf_url pattern — SA + coach can retrieve.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7b8c592d104'
down_revision: Union[str, Sequence[str], None] = 'e6a7b4c13f29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'coaching_students',
        sa.Column('workspace_snapshot_pdf_url', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('coaching_students', 'workspace_snapshot_pdf_url')
