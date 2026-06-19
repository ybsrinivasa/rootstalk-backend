"""Timeline lineage_id for BL-13 step 4 (BL-05a 2-lock across publishes)

Previously, Subscription.package_id auto-migrated to the new ACTIVE
version on every SE publish, and snapshots stayed keyed on the old
timeline_id — orphaned and unreachable. The render path then walked
the new package's timelines and fresh-snapshotted them, effectively
giving the SE god-mode push to all farmers regardless of lock state.

This migration introduces per-timeline lineage:

  - `timelines.lineage_id` — UUID, NOT NULL. For pre-existing rows
    backfilled to `timelines.id` so each existing timeline is its
    own lineage origin. The advisory publish clone flow stamps
    `lineage_id = src.lineage_id` going forward so cloned timelines
    share the lineage of their source.

  - `locked_timeline_snapshots.lineage_id` — UUID, NOT NULL.
    Backfilled from the source timeline's lineage_id. The existing
    `timeline_id` column stays as an audit trail of which physical
    Timeline row produced the snapshot's content.

  - Unique constraint moves from
    `(subscription_id, timeline_id, source)` to
    `(subscription_id, lineage_id, source)` — so per-subscription
    per-lineage there's still exactly one snapshot, but it survives
    across package republishes.

Pre-fix subscriptions whose snapshots predate the migration retain
the same content (their lineage_id == their original timeline_id);
those snapshots still match queries against the same `lineage_id`
post-fix. Subscriptions that already migrated through multiple
package versions before this fix are stuck on the latest version
(the lineage chain is broken for them — no way to reconstruct it
post-hoc). New subscriptions get full lineage tracking from the
publish that follows their first view.

Revision ID: b75f3e9c1a82
Revises: e4f1a8b209c5
Create Date: 2026-06-19
"""
from alembic import op
import sqlalchemy as sa


revision = "b75f3e9c1a82"
down_revision = "e4f1a8b209c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Timeline.lineage_id ─────────────────────────────────────────
    op.add_column(
        "timelines",
        sa.Column("lineage_id", sa.String(36), nullable=True),
    )
    # Backfill: each existing timeline is its own lineage origin.
    op.execute("UPDATE timelines SET lineage_id = id WHERE lineage_id IS NULL")
    op.alter_column("timelines", "lineage_id", nullable=False)
    op.create_index(
        "ix_timelines_lineage_id", "timelines", ["lineage_id"],
    )

    # ── LockedTimelineSnapshot.lineage_id ───────────────────────────
    op.add_column(
        "locked_timeline_snapshots",
        sa.Column("lineage_id", sa.String(36), nullable=True),
    )
    # Backfill: pull lineage_id from the snapshot's timeline_id row.
    op.execute(
        """
        UPDATE locked_timeline_snapshots lts
        SET lineage_id = t.lineage_id
        FROM timelines t
        WHERE t.id = lts.timeline_id
          AND lts.lineage_id IS NULL
        """
    )
    op.alter_column("locked_timeline_snapshots", "lineage_id", nullable=False)

    # Swap the uniqueness key. Old: (sub, timeline_id, source).
    # New: (sub, lineage_id, source). Same shape; survives republishes.
    op.drop_constraint(
        "uq_lts_sub_tl_source", "locked_timeline_snapshots", type_="unique",
    )
    op.create_unique_constraint(
        "uq_lts_sub_lineage_source",
        "locked_timeline_snapshots",
        ["subscription_id", "lineage_id", "source"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_lts_sub_lineage_source",
        "locked_timeline_snapshots",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_lts_sub_tl_source",
        "locked_timeline_snapshots",
        ["subscription_id", "timeline_id", "source"],
    )
    op.drop_column("locked_timeline_snapshots", "lineage_id")
    op.drop_index("ix_timelines_lineage_id", table_name="timelines")
    op.drop_column("timelines", "lineage_id")
