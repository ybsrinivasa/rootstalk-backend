"""Heal stale SENT alerts piled up day-over-day per subscription.

Revision ID: b3d7e9a14c82
Revises: c4f8e2a91b73
Create Date: 2026-06-22

Pre-fix, the daily alerts task wrote one new SENT Alert row per
(subscription, alert_type) per day, and only cleared them when ALL
due input practices had been ordered (see
clear_input_alerts_if_no_due_remaining). For subscriptions where the
input window stayed open day after day, this piled up rows — the user
reported seeing 5 INPUT alerts on a single Chilli sub (DE-26-000002)
spanning 19–22 Jun 2026.

The runtime fix (in app/tasks/alerts.py) supersedes prior SENT rows
before inserting today's, so going forward each (sub, alert_type)
shows exactly one pending alert. This migration heals the existing
pile-up: for every (subscription_id, alert_type) that has more than
one row in SENT, all but the most recent are flipped to READ. Audit
trail preserved.

Non-destructive: no rows deleted, no columns touched. Read-side
endpoints already filter on status=SENT so the dealer / facilitator
PWA refresh after this migration shows the cleaned-up state.
"""
from alembic import op


revision = "b3d7e9a14c82"
down_revision = "c4f8e2a91b73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE alerts a
        SET status = 'READ'
        WHERE a.status = 'SENT'
          AND a.sent_at < (
            SELECT MAX(a2.sent_at)
            FROM alerts a2
            WHERE a2.subscription_id = a.subscription_id
              AND a2.alert_type = a.alert_type
              AND a2.status = 'SENT'
          )
        """
    )


def downgrade() -> None:
    # Lossy: we don't know which were SENT vs READ before the heal.
    # Refuse rather than risk re-piling-up by flipping every READ back.
    raise NotImplementedError(
        "Downgrade not supported — this migration is a one-off heal."
    )
