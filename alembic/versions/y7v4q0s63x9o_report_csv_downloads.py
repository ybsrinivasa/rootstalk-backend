"""report_csv_downloads — audit log for CSV exports.

Revision ID: y7v4q0s63x9o
Revises: x6u3p9r52w8n
Create Date: 2026-11-21

Creates the `report_csv_downloads` table. Generic audit sink used by
every CSV export endpoint — identifies the report via `report_name`
and captures the filter set via `filters_json`. Readable from the SA
Portal only. No automatic retention; evidence for compliance review.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "y7v4q0s63x9o"
down_revision: Union[str, Sequence[str], None] = "x6u3p9r52w8n"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(sa.text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'report_csv_downloads'
    """)).first()
    if exists:
        return
    op.create_table(
        "report_csv_downloads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "client_id", sa.String(36),
            sa.ForeignKey("clients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("report_name", sa.String(80), nullable=False),
        sa.Column("filters_json", sa.JSON(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column(
            "downloaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_report_csv_downloads_downloaded_at",
        "report_csv_downloads", ["downloaded_at"],
    )
    op.create_index(
        "ix_report_csv_downloads_client_id",
        "report_csv_downloads", ["client_id"],
    )
    op.create_index(
        "ix_report_csv_downloads_report_name",
        "report_csv_downloads", ["report_name"],
    )
    op.create_index(
        "ix_report_csv_downloads_user_id",
        "report_csv_downloads", ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_report_csv_downloads_user_id", table_name="report_csv_downloads")
    op.drop_index("ix_report_csv_downloads_report_name", table_name="report_csv_downloads")
    op.drop_index("ix_report_csv_downloads_client_id", table_name="report_csv_downloads")
    op.drop_index("ix_report_csv_downloads_downloaded_at", table_name="report_csv_downloads")
    op.drop_table("report_csv_downloads")
