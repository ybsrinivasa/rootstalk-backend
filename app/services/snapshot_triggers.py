"""Snapshot trigger helpers — Phase 2 wiring.

Thin layer on top of `app.services.snapshot.take_snapshot` that the route
handlers and the nightly Celery sweep call into. Lives separately from the
core snapshot library so the library stays unaware of *when* it is invoked.

Failures are logged but never raised. The defensive nightly sweep
(`app/tasks/snapshot_sweep.py`) is the safety net — if a synchronous lock
attempt fails for any reason the next sweep catches it.

See: /Users/ybsrinivasa/.claude/projects/-Users-ybsrinivasa-cosh-backend/memory/per_subscription_versioning.md
"""
from __future__ import annotations

import logging
from typing import Iterable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.snapshot import take_snapshot

logger = logging.getLogger(__name__)


def cca_timeline_keys_from_items(items: Iterable) -> list[tuple[str, str]]:
    """Unique (timeline_id, 'CCA') pairs from items exposing `timeline_id`.

    Order items only reference CCA practices in the current codebase, so the
    source is hardcoded. If CHA practices ever become orderable this grows a
    source-aware variant.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for it in items:
        tid = getattr(it, "timeline_id", None) if not isinstance(it, str) else it
        if not tid or tid in seen:
            continue
        seen.add(tid)
        out.append((tid, "CCA"))
    return out


async def take_snapshots_for_keys(
    db: AsyncSession,
    subscription_id: str,
    keys: Sequence[tuple[str, str]],
    lock_trigger: str,
) -> int:
    """Best-effort snapshot capture for a batch of (timeline_id, source) keys.

    Returns the number of keys for which a snapshot is now in place
    (new or pre-existing). Per-key failures are logged and skipped.
    """
    n = 0
    for timeline_id, source in keys:
        try:
            await take_snapshot(
                db, subscription_id, timeline_id, lock_trigger, source=source
            )
            n += 1
        except Exception as exc:  # noqa: BLE001 — best-effort by design
            logger.warning(
                "snapshot capture failed sub=%s tl=%s src=%s trigger=%s: %s",
                subscription_id, timeline_id, source, lock_trigger, exc,
            )
    return n
