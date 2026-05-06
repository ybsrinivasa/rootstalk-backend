"""BL-12b — Query Expiry Background Job (runs hourly).
Closes all queries that have exceeded their 7-day window.
"""
import asyncio
import logging
from datetime import datetime, timezone
from celery import shared_task
from sqlalchemy import select
from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.farmpundit.models import Query, QueryRemark, QueryStatus, QueryRemarkAction
from app.modules.platform.models import User
from app.modules.clients.models import Client, ClientUser, ClientUserRole

logger = logging.getLogger(__name__)


async def _expire_queries_with_session(db, now=None) -> int:
    """Inner sweep: closes every Query past its 7-day expiry window
    that is still in a non-terminal status. Split out so integration
    tests can inject the testcontainer session and assert on the rows
    the task commits."""
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        select(Query).where(
            Query.status.notin_([
                QueryStatus.RESPONDED,
                QueryStatus.REJECTED,
                QueryStatus.EXPIRED,
            ]),
            Query.expires_at <= now,
        )
    )
    queries = result.scalars().all()

    for query in queries:
        query.status = QueryStatus.EXPIRED
        query.current_holder_id = None

        db.add(QueryRemark(
            query_id=query.id,
            pundit_id=None,
            # The QueryRemarkAction enum has no EXPIRED value — REJECTED
            # is the closest neighbour. The remark text marks this as
            # auto-expired so downstream readers can disambiguate.
            # Adding EXPIRED to the enum requires an Alembic migration
            # and is tracked as a deferred follow-up.
            action=QueryRemarkAction.REJECTED,
            remark="Auto-expired: 7-day resolution window elapsed.",
        ))

        # Get farmer for FCM notification
        farmer = (await db.execute(
            select(User).where(User.id == query.farmer_user_id)
        )).scalar_one_or_none()

        # TODO: Send FCM to farmer when Firebase key is available
        # FCM payload: "Your query could not be answered within 7 days. The company has been notified."
        logger.info(f"Query {query.id} expired. Farmer: {farmer.phone if farmer else 'unknown'}")

        # Get CA email for notification
        ca_user_row = (await db.execute(
            select(ClientUser, User)
            .join(User, User.id == ClientUser.user_id)
            .where(ClientUser.client_id == query.client_id, ClientUser.role == ClientUserRole.CA)
        )).first()

        if ca_user_row:
            _cu, ca_user = ca_user_row
            # TODO: Send email to CA with full query details + remarks chain
            # when email service is wired
            logger.info(f"Should email CA {ca_user.email} about expired query {query.id}")

    if queries:
        await db.commit()
        logger.info(f"BL-12b: Expired {len(queries)} queries")
    return len(queries)


async def _expire_queries():
    """Production entry point: opens its own session and runs the
    inner sweep."""
    async with AsyncSessionLocal() as db:
        return await _expire_queries_with_session(db)


@celery_app.task(name="app.tasks.query_expiry.expire_queries")
def expire_queries():
    """BL-12b: Hourly check for expired queries.

    BL-12 audit (2026-05-06): migrated from
    asyncio.get_event_loop().run_until_complete(...) to asyncio.run.
    Same family as the BL-09 alerts fix — the old form is deprecated
    and raises in Python 3.12+.
    """
    asyncio.run(_expire_queries())
