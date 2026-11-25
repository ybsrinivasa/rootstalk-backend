"""locked_timeline_snapshots: add force_pushed_at

2026-11-25 — AOM content-versioning policy: when a CM/CA explicitly
force-pushes a Timeline edit over existing VIEWED snapshots (e.g.
critical safety correction), the snapshots' content is rewritten
AND we stamp `force_pushed_at = now()` on each rewritten row. The
PWA uses this timestamp to render a "Recently updated" banner for
5 days (matching the delete-account grace window — same underlying
"recent human attention" heuristic).

PURCHASE_ACK snapshots are NEVER rewritten by force-push — the
farmer's offline-purchase commitment is sacrosanct. force_pushed_at
stays NULL on those rows (banner never fires).

Nullable column + idempotent — safe to apply on an empty-snapshot
database (prod state before SMS Grow backfill) and after the
backfill alike.
"""
from alembic import op
import sqlalchemy as sa


revision = "c2z8u4w07b3s"
down_revision = "b1y7t3v96a2r"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: tolerate prior partial application / manual pre-add.
    conn = op.get_bind()
    exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'locked_timeline_snapshots' "
            "  AND column_name = 'force_pushed_at'"
        )
    ).scalar()
    if exists:
        return
    op.add_column(
        "locked_timeline_snapshots",
        sa.Column("force_pushed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("locked_timeline_snapshots", "force_pushed_at")
