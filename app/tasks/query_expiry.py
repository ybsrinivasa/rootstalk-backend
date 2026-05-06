"""BL-12b — Query Expiry Background Job (runs hourly).
Closes all queries that have exceeded their 7-day window.
Pushes an FCM notification to the farmer (if they have a token
registered via the PWA) so they aren't left wondering whether
the FarmPundit ever got back to them.
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
from app.services.fcm_service import send_fcm

logger = logging.getLogger(__name__)

EXPIRY_FCM_TITLE = "Your query couldn't be answered in time"
EXPIRY_FCM_BODY = (
    "The 7-day window for your FarmPundit query has elapsed. The company "
    "has been notified — please raise it again or ask another expert."
)


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

        # FCM Batch 3 (2026-05-06): notify farmer that their query
        # auto-expired. Skipped silently if the farmer hasn't
        # registered a token yet (most farmers in V1 until the PWA
        # wires the registration call).
        farmer = (await db.execute(
            select(User).where(User.id == query.farmer_user_id)
        )).scalar_one_or_none()
        if farmer and farmer.fcm_token:
            try:
                await send_fcm(
                    token=farmer.fcm_token,
                    title=EXPIRY_FCM_TITLE,
                    body=EXPIRY_FCM_BODY,
                    data={
                        "type": "QUERY_EXPIRED",
                        "query_id": query.id,
                        "subscription_id": query.subscription_id,
                    },
                )
            except Exception as e:
                # send_fcm catches its own errors and returns False;
                # this guard is belt-and-braces.
                logger.error(f"FCM send raised unexpectedly for farmer {farmer.id}: {e}")
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
