"""Daily task: permanently anonymise users whose grace deletion period has expired.

2026-11-17 — Grace window is now configurable via settings
(`account_deletion_grace_days`, default 30). Production keeps 30 days;
staging sets it to 0 so the full post-anonymisation state is reachable
in-session for testers. The actual scrub shape (which fields are
NULL'd / scrambled) is factored into `anonymise_user_row` so both the
daily task and the synchronous same-request anonymise from the delete
endpoint (grace=0 path) apply identical mutations.
"""
import asyncio
import logging
import secrets
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.modules.platform.models import User
from app.celery_app import celery_app
from app.config import settings

logger = logging.getLogger(__name__)


def anonymise_user_row(user: User) -> None:
    """In-place anonymisation of a single User row. The row is NOT
    deleted — FK references across the system (Subscription, Order,
    Query, FarmerCallLog, PromoterAssignment, etc.) stay valid and
    just resolve to "Deleted User" in joins. Idempotent — running
    on an already-anonymised row is a no-op (phone would already be
    the scrambled form, name would already be "Deleted User").

    2026-11-17 — Scrambled phone format shortened from
    `deleted_<16hex>` (24 chars) → `del_<10hex>` (14 chars) to fit
    in User.phone's String(15) column. The original 24-char pattern
    was a latent bug in the daily task since account-deletion
    shipped — every prod anonymisation would have StringDataRight-
    TruncationError'd on first attempt. Surfaced today when the
    staging synchronous-anonymise path (grace=0) hit it on every
    delete. 40 bits of entropy from 10 hex chars = 1-in-a-trillion
    collision per million deleted users, plenty for anonymisation
    purposes."""
    user.name = "Deleted User"
    user.phone = f"del_{secrets.token_hex(5)}"
    user.email = None
    user.gps_lat = None
    user.gps_lng = None
    user.address_line = None
    user.locality = None
    user.town = None
    user.current_session_id = None


@celery_app.task(name="app.tasks.account_deletion.anonymise_deleted_users")
def anonymise_deleted_users():
    """Run daily: permanently anonymise users deleted more than the
    grace period ago. On staging (grace=0) the daily task becomes a
    safety net — the delete endpoint already scrubs synchronously."""
    return asyncio.run(_run())


async def _run() -> int:
    async with AsyncSessionLocal() as db:
        grace_days = settings.account_deletion_grace_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
        users = (await db.execute(
            select(User).where(
                User.deleted_at.is_not(None),
                User.deleted_at < cutoff,
                User.name != "Deleted User",  # not already anonymised
            )
        )).scalars().all()
        for u in users:
            anonymise_user_row(u)
        if users:
            await db.commit()
            logger.info(f"Anonymised {len(users)} expired soft-deleted users")
        return len(users)
