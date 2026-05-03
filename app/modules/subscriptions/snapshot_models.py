"""
SQLAlchemy model for `locked_timeline_snapshots`.

Per-Subscription Content Versioning — Phase 1 (library only, no callers).

A snapshot is an immutable JSONB capture of a Timeline (CCA) or PG/SP timeline (CHA)
plus all its dependent rows (practices, elements, relations, conditional questions,
practice-conditional links). Once written, it is the source of truth for that
(subscription_id, timeline_id, source) triple forever — even if SE later edits the
master tables.

See `app/services/snapshot.py` for the serialiser/deserialiser, and
`/Users/ybsrinivasa/.claude/projects/-Users-ybsrinivasa-cosh-backend/memory/per_subscription_versioning.md`
for the full architectural spec.
"""
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.modules.subscriptions.models import new_uuid, utcnow


class LockedTimelineSnapshot(Base):
    __tablename__ = "locked_timeline_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subscription_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # timeline_id is intentionally NOT a FK — it can reference timelines (CCA),
    # pg_timelines, or sp_timelines. The `source` column disambiguates.
    timeline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="CCA")
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    lock_trigger: Mapped[str] = mapped_column(String(20), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "timeline_id", "source", name="uq_lts_sub_tl_source"
        ),
    )
