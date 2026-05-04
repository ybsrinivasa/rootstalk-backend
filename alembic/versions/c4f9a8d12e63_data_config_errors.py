"""data_config_errors table for algorithm config-error audit (BL-01 + future)

Revision ID: c4f9a8d12e63
Revises: b2e5f8c1d473
Create Date: 2026-05-04 12:00:00.000000

Captures BL-01 (and future algorithm) configuration errors so the
Content Manager / SA team can investigate via the admin endpoint
without depending on log scrapers or email infra.
"""
from alembic import op
import sqlalchemy as sa


revision = "c4f9a8d12e63"
down_revision = "b2e5f8c1d473"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "data_config_errors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("algorithm", sa.String(20), nullable=False),
        sa.Column(
            "client_id", sa.String(36),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("crop_cosh_id", sa.String(100), nullable=True),
        sa.Column("district_cosh_id", sa.String(100), nullable=True),
        sa.Column("answers_state", sa.Text, nullable=True),
        sa.Column("details", sa.Text, nullable=True),
        sa.Column(
            "observed_by_user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_dce_algorithm_occurred_at",
        "data_config_errors",
        ["algorithm", "occurred_at"],
    )


def downgrade():
    op.drop_index(
        "ix_dce_algorithm_occurred_at", table_name="data_config_errors",
    )
    op.drop_table("data_config_errors")
