"""crop_measures table — Area-wise vs Plant-wise per crop

Revision ID: f6a12b8c4d97
Revises: e8c4a7d62b91
Create Date: 2026-05-05 09:00:00.000000

The Measure (AREA_WISE / PLANT_WISE) classification of each crop drives
BL-06 volume-formula lookup AND the SE practice-creation UI (whether
Volume_per_plant is shown). Today the SA seeds rows manually via the
admin endpoint; long-term the source is Cosh, with `synced_from_cosh_at`
as the integration placeholder.
"""
from alembic import op
import sqlalchemy as sa


revision = "f6a12b8c4d97"
down_revision = "e8c4a7d62b91"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "crop_measures",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("crop_cosh_id", sa.String(100), nullable=False, unique=True),
        sa.Column("measure", sa.String(20), nullable=False),
        sa.Column(
            "updated_by_user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("synced_from_cosh_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("crop_measures")
