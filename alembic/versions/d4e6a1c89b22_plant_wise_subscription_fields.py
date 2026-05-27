"""plant-wise subscription fields

Adds the columns the Crop Dashboard needs to capture plant-wise
crop context: number_of_plants + planting_year (integer year only)
+ plant_count_confirmed_at (mirror of farm_area_confirmed_at for
the plant-wise lock-in).

All nullable — existing area-wise subscriptions keep working
unchanged. Lenient by design (user direction 2026-05-27): the
backend refuses to write to the wrong-measure column going
forward, but does NOT auto-null any pre-existing value. A
subscription whose crop later flips measure on Cosh's side keeps
its old value alongside the new one; the read endpoint surfaces
both and the PWA shows whichever applies per the live measure.

Revision ID: d4e6a1c89b22
Revises: c5d2a8e4f137
Create Date: 2026-05-27
"""
from alembic import op
import sqlalchemy as sa


revision = "d4e6a1c89b22"
down_revision = "c5d2a8e4f137"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("number_of_plants", sa.Integer(), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("planting_year", sa.Integer(), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "plant_count_confirmed_at",
            sa.DateTime(timezone=True), nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "plant_count_confirmed_at")
    op.drop_column("subscriptions", "planting_year")
    op.drop_column("subscriptions", "number_of_plants")
