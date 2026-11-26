"""practices + practice_acknowledgements: add lineage ids

2026-11-26 — Stable practice identity across package-clone chains.

Problem (incident SM-26-000004, prod): a farmer tapped "I've
purchased this" on a brand-locked INPUT practice. The CM later
republished the package (which clones every Timeline + Practice row
with new ids, preserving only `timelines.lineage_id`). Subscription
auto-migrated to the new package. CP Farmer Support kept showing
"Due now" because the ack's `practice_id` referenced the OLD (now
not-subscribed) practice row — the lookup against the new live
Practice came up empty and status fell through to DUE_NOW.

Fix: add `practices.lineage_id` (stable across clone generations)
+ `practice_acknowledgements.practice_lineage_id` (populated at
ack-write from the practice's lineage). Read side (CP + alert-vanish)
matches by lineage instead of by practice_id.

Backfill clustering (2B, confirmed with 2026-11-26 audit on prod):
  For INPUT practices WITH a COMMON_NAME element:
    cluster by (timeline.lineage_id, l1_type, l2_type,
                COMMON_NAME.cosh_ref) → share ONE lineage_id
    (= min(practice.id) in the cluster, so re-runs are stable).
    Audit found 394 clone clusters across 7,728 such practices,
    0 same-timeline duplicates — invariant holds.
  All other practices (INPUT without COMMON_NAME, SPECIAL_INPUT,
  NON_INPUT, INSTRUCTION, MEDIA): self-identity (lineage_id = id).
  Prod audit: 152 no-COMMON_NAME INPUTs, only 2 have a purchased
  ack — tiny risk surface, not worth NPK-element fingerprinting.

Ack backfill: for each ack row, join to practices → set
practice_lineage_id = practices.lineage_id. Rows the backfill can't
resolve (practice already hard-deleted, if any) stay NULL and fall
back to practice_id matching at read time.

Idempotent + safe to re-run (information_schema guards).
"""
from alembic import op
import sqlalchemy as sa


revision = "e4b8w6y29d5u"
down_revision = "d3a9v5x18c4t"
branch_labels = None
depends_on = None


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).scalar())


def upgrade() -> None:
    conn = op.get_bind()

    # ── practices.lineage_id ────────────────────────────────────────
    if not _column_exists(conn, "practices", "lineage_id"):
        op.add_column(
            "practices",
            sa.Column("lineage_id", sa.String(length=36), nullable=True),
        )

    # ── practice_acknowledgements.practice_lineage_id ───────────────
    if not _column_exists(
        conn, "practice_acknowledgements", "practice_lineage_id",
    ):
        op.add_column(
            "practice_acknowledgements",
            sa.Column(
                "practice_lineage_id", sa.String(length=36), nullable=True,
            ),
        )

    # ── backfill practices.lineage_id ───────────────────────────────
    # Step 1: self-identity default — every practice gets a lineage_id,
    # safe fallback for the non-INPUT / no-COMMON_NAME paths.
    conn.execute(sa.text(
        "UPDATE practices SET lineage_id = id WHERE lineage_id IS NULL"
    ))

    # Step 2: consolidate clone chains for INPUT practices WITH
    # COMMON_NAME. Within each cluster (timeline.lineage_id, l1_type,
    # l2_type, COMMON_NAME.cosh_ref) take the min(practices.id) as the
    # shared lineage_id. Stable across re-runs (min is deterministic).
    conn.execute(sa.text("""
        WITH cluster_lineage AS (
            SELECT
                t.lineage_id AS tl_lineage,
                p.l1_type,
                p.l2_type,
                e.cosh_ref AS common_name_cosh,
                MIN(p.id) AS canonical_lineage_id
            FROM practices p
            JOIN timelines t ON t.id = p.timeline_id
            JOIN elements e ON e.practice_id = p.id
                            AND e.element_type = 'COMMON_NAME'
                            AND e.cosh_ref IS NOT NULL
            WHERE p.l0_type = 'INPUT'
            GROUP BY t.lineage_id, p.l1_type, p.l2_type, e.cosh_ref
        )
        UPDATE practices p
        SET lineage_id = cl.canonical_lineage_id
        FROM cluster_lineage cl, timelines t, elements e
        WHERE t.id = p.timeline_id
          AND e.practice_id = p.id
          AND e.element_type = 'COMMON_NAME'
          AND e.cosh_ref IS NOT NULL
          AND cl.tl_lineage = t.lineage_id
          AND cl.l1_type IS NOT DISTINCT FROM p.l1_type
          AND cl.l2_type IS NOT DISTINCT FROM p.l2_type
          AND cl.common_name_cosh = e.cosh_ref
          AND p.l0_type = 'INPUT'
    """))

    # Enforce NOT NULL + index now that every row is populated.
    op.alter_column(
        "practices", "lineage_id",
        existing_type=sa.String(length=36), nullable=False,
    )
    # Idempotent index add (CREATE INDEX IF NOT EXISTS needs raw SQL).
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_practices_lineage_id "
        "ON practices (lineage_id)"
    ))

    # ── backfill practice_acknowledgements.practice_lineage_id ──────
    conn.execute(sa.text("""
        UPDATE practice_acknowledgements a
        SET practice_lineage_id = p.lineage_id
        FROM practices p
        WHERE p.id = a.practice_id
          AND a.practice_lineage_id IS NULL
    """))

    # Index on the composite lookup key (subscription + timeline lineage
    # + practice lineage). Index on just practice_lineage_id is also
    # useful for the alert-vanish path.
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_pracks_practice_lineage_id "
        "ON practice_acknowledgements (practice_lineage_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_pracks_sub_tl_lineage_prac_lineage "
        "ON practice_acknowledgements "
        "(subscription_id, timeline_lineage_id, practice_lineage_id)"
    ))


def downgrade() -> None:
    op.drop_index(
        "ix_pracks_sub_tl_lineage_prac_lineage",
        table_name="practice_acknowledgements",
    )
    op.drop_index(
        "ix_pracks_practice_lineage_id",
        table_name="practice_acknowledgements",
    )
    op.drop_column("practice_acknowledgements", "practice_lineage_id")
    op.drop_index("ix_practices_lineage_id", table_name="practices")
    op.drop_column("practices", "lineage_id")
