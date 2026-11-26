"""practices: add brand_image_url

2026-11-26 — Single image per brand-locked INPUT practice. CM/CA
uploads via CP; farmer PWA renders it inline below the recommendation
card so the farmer recognises the product on the dealer's shelf.

Nullable + idempotent. Column is only meaningfully populated when
practices.is_brand_locked = true; PUT practice auto-clears this
field if the CA flips the lock off.
"""
from alembic import op
import sqlalchemy as sa


revision = "d3a9v5x18c4t"
down_revision = "c2z8u4w07b3s"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'practices' "
            "  AND column_name = 'brand_image_url'"
        )
    ).scalar()
    if exists:
        return
    op.add_column(
        "practices",
        sa.Column("brand_image_url", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("practices", "brand_image_url")
