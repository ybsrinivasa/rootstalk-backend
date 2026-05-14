"""Purge MEDIA_VIDEO practices (Batch 36, 2026-05-14).

MEDIA_VIDEO was removed from the L2 rule book and taxonomy in Batch
36 — direct video uploads aren't supported. Existing rows (if any)
would fail validation on next edit, so this migration deletes
practices with l2_type='MEDIA_VIDEO' and any elements that hung off
them. Almost certainly a no-op on prod and testing — the L2 had no
upload widget so authoring it produced shell rows at best.

Revision ID: b36a92e1f08c
Revises: e9a3b2f7c5d8
Create Date: 2026-05-14
"""
from alembic import op


revision = "b36a92e1f08c"
down_revision = "e9a3b2f7c5d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM elements WHERE practice_id IN "
        "(SELECT id FROM practices WHERE l2_type='MEDIA_VIDEO')"
    )
    op.execute("DELETE FROM practices WHERE l2_type='MEDIA_VIDEO'")


def downgrade() -> None:
    # Irreversible cleanup — no-op.
    pass
