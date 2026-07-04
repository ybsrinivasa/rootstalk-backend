"""Brand Forms — extend missing_brand_reports for standalone dashboard submissions.

Revision ID: f8b6d10c2e75
Revises: e3b6f28c1a45
Create Date: 2026-07-04

Background:
The existing SA /brand-handling surface reads `missing_brand_reports`,
which were designed for the during-order dealer flow (order_item_id
required). The 2026-07-04 Brand Forms rework adds a dashboard-launched
standalone flow: dealer picks L1/L2, types brand + manufacturer,
uploads 2-4 product photos, submits. SA reviews from the same portal
page.

Column changes:
- `order_item_id` becomes nullable (standalone submissions have none).
- New `l1_type` VARCHAR(100) nullable — legacy rows had only l2_practice.
- New `photos` JSONB nullable=False, default '[]' — S3 URLs.
- New `dealer_seen_status_at` TIMESTAMP nullable — drives unread badge.
- New `hidden_from_dealer_at` TIMESTAMP nullable — soft-delete for the
  dealer's history view; SA always sees the row.
- New `reviewed_at` TIMESTAMP nullable — stamped when SA sets status
  to APPROVED / REJECTED; feeds the "unseen update" comparison
  against `dealer_seen_status_at`.

No enum changes — existing PENDING / REVIEWED / APPROVED / REJECTED
carry through, with UI relabelling PENDING→Submitted and
APPROVED→Included per user's terminology.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f8b6d10c2e75"
down_revision: Union[str, Sequence[str], None] = "e3b6f28c1a45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "missing_brand_reports", "order_item_id",
        existing_type=sa.String(length=36), nullable=True,
    )
    op.add_column(
        "missing_brand_reports",
        sa.Column("l1_type", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "missing_brand_reports",
        sa.Column(
            "photos", sa.JSON(),
            nullable=False, server_default=sa.text("'[]'::json"),
        ),
    )
    op.add_column(
        "missing_brand_reports",
        sa.Column("dealer_seen_status_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "missing_brand_reports",
        sa.Column("hidden_from_dealer_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "missing_brand_reports",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("missing_brand_reports", "reviewed_at")
    op.drop_column("missing_brand_reports", "hidden_from_dealer_at")
    op.drop_column("missing_brand_reports", "dealer_seen_status_at")
    op.drop_column("missing_brand_reports", "photos")
    op.drop_column("missing_brand_reports", "l1_type")
    op.alter_column(
        "missing_brand_reports", "order_item_id",
        existing_type=sa.String(length=36), nullable=False,
    )
