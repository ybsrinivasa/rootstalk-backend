"""Per-client, time-bounded feature access.

Append-only history table. One row per activation / extension /
turn-off. The "current state" for a (client, feature) pair is the
most recent row. The feature is active now iff that row's
`access_until > now()`.

Used by the SA Portal to turn paid features ON for a client with a
specific end date. Extensible via the open-string `feature` column —
v1 ships with values "messaging" and "touchpoints"; future paid
features reuse the same table with their own feature slug.

Why append-only (not update-in-place):
- Compliance audit: "show me the full history of this client's
  Messaging access" is a single query.
- No race conditions on concurrent SA edits.
- Matches the `report_csv_downloads` audit pattern we already run.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


def _new_uuid():
    return str(uuid.uuid4())


# Known feature slugs. Open string on the column so new features can
# ship without a schema change; the enum here is for static lookup.
FEATURE_MESSAGING = "messaging"
FEATURE_TOUCHPOINTS = "touchpoints"

KNOWN_FEATURES = frozenset({FEATURE_MESSAGING, FEATURE_TOUCHPOINTS})


class ClientFeatureAccess(Base):
    __tablename__ = "client_feature_access"
    __table_args__ = (
        Index("ix_cfa_client_feature_created", "client_id", "feature", "created_at"),
        Index("ix_cfa_access_until", "access_until"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid,
    )
    client_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Open string so new paid features can ship without a migration.
    # Code-level KNOWN_FEATURES is the vocabulary check.
    feature: Mapped[str] = mapped_column(String(40), nullable=False)
    # End of the paid access window. NOT NULL — v1 doesn't carry a
    # perpetual-access concept. A "turn off before expiry" write is
    # modeled as a new row whose access_until = now(), which makes
    # the derived "is-active" check (access_until > now()) consistent
    # for both natural expiry and early manual turn-off.
    access_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    # SA user who performed the activation / extension / turn-off.
    # SET NULL so deleting a historic SA account doesn't break the
    # audit chain.
    activated_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
