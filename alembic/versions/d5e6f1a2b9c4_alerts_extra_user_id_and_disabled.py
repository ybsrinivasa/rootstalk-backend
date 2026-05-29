"""alerts: extra_alert_user_id + alerts_extra_disabled

Revision ID: d5e6f1a2b9c4
Revises: c8f4e5a2b7d9
Create Date: 2026-05-29 18:00:00.000000

Alerts A + C (2026-05-29). The farmer's saved "extra alert recipient"
was being persisted on the Subscription but the alerts task was reading
from the legacy `alert_recipients` table, so the override never reached
the sender. Two columns close the gap:

  extra_alert_user_id   — FK to users.id. Set when the farmer's typed
                          phone resolves to a real Dealer / Facilitator
                          User. Routes through the existing User-based
                          SMS + FCM pipeline (User.phone, User.fcm_token).
                          NULL means no override.

  alerts_extra_disabled — boolean. When TRUE, the farmer explicitly
                          opted out of any extra recipient (including
                          the auto-promoter fallback for ASSIGNED subs).
                          Resolver returns farmer-only.

Both columns are additive and nullable / defaulted, so the migration is
non-destructive and a code-only rollback is safe.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e6f1a2b9c4'
down_revision: Union[str, Sequence[str], None] = 'c8f4e5a2b7d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "extra_alert_user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "alerts_extra_disabled", sa.Boolean(),
            nullable=False, server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "alerts_extra_disabled")
    op.drop_column("subscriptions", "extra_alert_user_id")
