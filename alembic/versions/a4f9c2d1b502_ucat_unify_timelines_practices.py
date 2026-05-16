"""Batch 39O — UCAT unification of timelines/practices/elements.

Collapses three parallel sets of advisory-content tables into one
shared shape (locked 2026-05-16):

  • `timelines` (CCA-only before) gains nullable FKs for
    `pg_recommendation_id`, `sp_recommendation_id`,
    `standard_response_id`. `package_id` becomes nullable. A CHECK
    constraint enforces exactly one parent per row. The
    `from_type` column is widened from the CCA-only enum to a
    plain `VARCHAR(30)` so CHA / Q&A anchor units (`DAYS_AFTER_
    DETECTION`, `DAYS_AFTER_RESPONSE`) coexist with `DAS / DBS /
    CALENDAR`. The legacy `(package_id, name)` unique constraint
    is replaced with four partial unique indexes — one per
    parent FK.

  • The legacy tables `pg_timelines / pg_practices / pg_elements
    / sp_timelines / sp_practices / sp_elements` are wiped (and
    so are their parent recommendation rows, per the user's
    "wipe testing-server PG/QA data" sign-off) and dropped.

After this migration every advisory pipe (CCA / CHA-PG / CHA-SP
/ Q&A) shares one Timeline → Practice → Element → Relation →
Conditional-Question schema. UI work that was CCA-only (Brand
Lock, frequency, Relations UI, CQ authoring, clone-to-draft,
publish gates, version history) automatically applies to the
other pipes once their endpoints are pointed at the unified
tables.

Revision ID: a4f9c2d1b502
Revises: d7b3e4f1c2a8
Create Date: 2026-05-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a4f9c2d1b502"
down_revision = "d7b3e4f1c2a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Wipe legacy data in FK-dependency order ──────────────────
    # No-op on a fresh DB; safety net on testing.
    for tbl in (
        "pg_elements", "pg_practices", "pg_timelines", "pg_recommendations",
        "sp_elements", "sp_practices", "sp_timelines", "sp_recommendations",
        "standard_responses",
    ):
        op.execute(f"DELETE FROM {tbl}")

    # ── 2. Drop legacy tables (FKs go with them) ────────────────────
    op.drop_table("pg_elements")
    op.drop_table("pg_practices")
    op.drop_table("pg_timelines")
    op.drop_table("sp_elements")
    op.drop_table("sp_practices")
    op.drop_table("sp_timelines")

    # ── 3. Extend the unified `timelines` table ─────────────────────
    op.add_column(
        "timelines",
        sa.Column("pg_recommendation_id", sa.String(36),
                  sa.ForeignKey("pg_recommendations.id"), nullable=True),
    )
    op.add_column(
        "timelines",
        sa.Column("sp_recommendation_id", sa.String(36),
                  sa.ForeignKey("sp_recommendations.id"), nullable=True),
    )
    op.add_column(
        "timelines",
        sa.Column("standard_response_id", sa.String(36),
                  sa.ForeignKey("standard_responses.id"), nullable=True),
    )

    # Relax package_id to nullable so non-CCA timelines can exist.
    op.alter_column("timelines", "package_id", nullable=True)

    # Convert from_type from the CCA-only postgres enum
    # `timelinefromtype` to a plain VARCHAR(30) so CHA/QA strings
    # (DAYS_AFTER_DETECTION / DAYS_AFTER_RESPONSE) coexist with
    # DAS / DBS / CALENDAR.
    op.alter_column(
        "timelines", "from_type",
        type_=sa.String(30),
        existing_type=sa.Enum(
            "DBS", "DAS", "CALENDAR", name="timelinefromtype",
        ),
        postgresql_using="from_type::text",
    )
    # Drop the now-unused postgres enum type.
    op.execute("DROP TYPE IF EXISTS timelinefromtype")

    # Drop the old (package_id, name) UNIQUE constraint — uniqueness
    # is now scoped per parent kind via partial indexes below.
    op.drop_constraint("timelines_package_id_name_key", "timelines", type_="unique")

    # Partial unique indexes — one per parent FK so name uniqueness
    # is scoped to each (parent, name) tuple.
    op.create_index(
        "uq_timelines_package_name", "timelines",
        ["package_id", "name"], unique=True,
        postgresql_where=sa.text("package_id IS NOT NULL"),
    )
    op.create_index(
        "uq_timelines_pg_rec_name", "timelines",
        ["pg_recommendation_id", "name"], unique=True,
        postgresql_where=sa.text("pg_recommendation_id IS NOT NULL"),
    )
    op.create_index(
        "uq_timelines_sp_rec_name", "timelines",
        ["sp_recommendation_id", "name"], unique=True,
        postgresql_where=sa.text("sp_recommendation_id IS NOT NULL"),
    )
    op.create_index(
        "uq_timelines_sr_name", "timelines",
        ["standard_response_id", "name"], unique=True,
        postgresql_where=sa.text("standard_response_id IS NOT NULL"),
    )

    # CHECK: exactly one parent FK set.
    op.create_check_constraint(
        "timelines_one_parent_chk", "timelines",
        "(CASE WHEN package_id IS NOT NULL THEN 1 ELSE 0 END) + "
        "(CASE WHEN pg_recommendation_id IS NOT NULL THEN 1 ELSE 0 END) + "
        "(CASE WHEN sp_recommendation_id IS NOT NULL THEN 1 ELSE 0 END) + "
        "(CASE WHEN standard_response_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
    )


def downgrade() -> None:
    """Pre-launch destructive migration; no useful downgrade path
    given the legacy tables + data are gone. If recovery is ever
    needed, restore from backup before re-running."""
    raise NotImplementedError(
        "Batch 39O is a one-way pre-launch consolidation. Restore "
        "from a backup taken before 2026-05-16 if you need to roll back."
    )
