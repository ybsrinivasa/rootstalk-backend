"""Add lineage columns to pg_recommendations (mirror Package).

Per user 2026-05-18: CA-PG gains full multi-row versioning so that
each Import-from-Global creates a new DRAFT under the existing
lineage. Mirror the Package columns:

  source_version_id  — FK to another pg_recommendations row this
                       DRAFT was cloned/imported from.
  created_via        — audit/lineage marker (SE_PULL_DRAFT for
                       imports, SE_EDIT_DRAFT for clone-to-draft,
                       SE_ROLLBACK_PUBLISH reserved).
  published_at       — set on publish so the version-history panel
                       can show when each lineage row went live.
  published_by       — FK to the user who published it.

Revision ID: c4f6e2a91d3b
Revises: b8d7e3f5a219
Create Date: 2026-05-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "c4f6e2a91d3b"
down_revision = "b8d7e3f5a219"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # String columns + plain FKs — match the shape of Package's
    # lineage columns rather than introducing a Postgres enum
    # (Package's PackageCreatedVia is a SAEnum; we leave PG as a
    # plain VARCHAR for forward-compat with PG-specific values).
    op.add_column(
        "pg_recommendations",
        sa.Column(
            "source_version_id", sa.String(length=36), nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_pg_recommendations_source_version_id",
        "pg_recommendations", "pg_recommendations",
        ["source_version_id"], ["id"],
    )
    op.add_column(
        "pg_recommendations",
        sa.Column("created_via", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "pg_recommendations",
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "pg_recommendations",
        sa.Column("published_by", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_pg_recommendations_published_by",
        "pg_recommendations", "users",
        ["published_by"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_pg_recommendations_published_by",
        "pg_recommendations", type_="foreignkey",
    )
    op.drop_column("pg_recommendations", "published_by")
    op.drop_column("pg_recommendations", "published_at")
    op.drop_column("pg_recommendations", "created_via")
    op.drop_constraint(
        "fk_pg_recommendations_source_version_id",
        "pg_recommendations", type_="foreignkey",
    )
    op.drop_column("pg_recommendations", "source_version_id")
