"""ClientCrop attribute snapshot (name, scientific name, area/plant)

Revision ID: c0e2bf1da3a4
Revises: b5e91d4827af
Create Date: 2026-05-06 18:00:00.000000

CCA Step 1 / Batch 1B — captures a per-client snapshot of crop
attributes at CA-add time so the company's CCA configuration doesn't
silently drift if Cosh-side data later changes. System-level
data (area/plant typing in particular) remains canonical in
`crop_measures`; the per-client snapshot duplicates it as
defense-in-depth + audit trail.

Columns are nullable so the migration is forward-compatible. A
backfill script (`scripts/backfill_clientcrop_snapshots.py`) walks
existing rows and populates from `cosh_reference_cache` +
`crop_measures`. Rows whose source data is missing remain NULL
until SA seeds the references.
"""
from alembic import op
import sqlalchemy as sa


revision = "c0e2bf1da3a4"
down_revision = "b5e91d4827af"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "client_crops",
        sa.Column("crop_name_en", sa.Text(), nullable=True),
    )
    op.add_column(
        "client_crops",
        sa.Column("crop_scientific_name", sa.Text(), nullable=True),
    )
    op.add_column(
        "client_crops",
        sa.Column("crop_area_or_plant", sa.String(length=20), nullable=True),
    )


def downgrade():
    op.drop_column("client_crops", "crop_area_or_plant")
    op.drop_column("client_crops", "crop_scientific_name")
    op.drop_column("client_crops", "crop_name_en")
