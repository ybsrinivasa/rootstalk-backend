"""Daily task: permanently anonymise users whose 30-day grace deletion period has expired."""
import asyncio
import logging
import secrets
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.modules.platform.models import User
from app.celery_app import celery_app

logger = logging.getLogger(__name__)

GRACE_PERIOD_DAYS = 30


@celery_app.task(name="app.tasks.account_deletion.anonymise_deleted_users")
def anonymise_deleted_users():
    """Run daily: permanently anonymise users deleted more than 30 days ago."""
    return asyncio.run(_run())


async def _run() -> int:
    async with AsyncSessionLocal() as db:
        cutoff = datetime.now(timezone.utc) - timedelta(days=GRACE_PERIOD_DAYS)
        users = (await db.execute(
            select(User).where(
                User.deleted_at.is_not(None),
                User.deleted_at < cutoff,
                User.name != "Deleted User",  # not already anonymised
            )
        )).scalars().all()
        for u in users:
            u.name = "Deleted User"
            u.phone = f"deleted_{secrets.token_hex(8)}"
            u.email = None
            u.gps_lat = None
            u.gps_lng = None
            u.address_line = None
            u.locality = None
            u.town = None
            u.current_session_id = None
        if users:
            await db.commit()
            logger.info(f"Anonymised {len(users)} expired soft-deleted users")
        return len(users)
