"""BL-10: Daily order expiry — mark stale unprocessed orders as EXPIRED."""
import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.modules.orders.models import Order, OrderStatus
from app.modules.platform.models import User
from app.services.fcm_service import send_fcm
from app.celery_app import celery_app

logger = logging.getLogger(__name__)

ORDER_EXPIRY_DAYS = 14

# 2026-07-16 — Push the farmer that their order timed out. Without this
# the DRAFT quietly disappears from their Manage tab and the farmer
# only discovers the loss on their next visit.
EXPIRY_FCM_TITLE = "Your order has expired"
EXPIRY_FCM_BODY = (
    "It's been more than 14 days without dealer action. "
    "Re-route the items in RootsTalk to keep them moving."
)


@celery_app.task(name="app.tasks.order_expiry.expire_stale_orders")
def expire_stale_orders():
    asyncio.run(_run())


async def _run():
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(Order).where(
                Order.expires_at.isnot(None),
                Order.expires_at < now,
                Order.status.notin_([
                    OrderStatus.COMPLETED, OrderStatus.CANCELLED,
                    OrderStatus.EXPIRED,
                ]),
            )
        )
        expired = result.scalars().all()
        for order in expired:
            order.status = OrderStatus.EXPIRED
            logger.info(f"Expired order {order.id}")
        if expired:
            await db.commit()
            logger.info(f"Expired {len(expired)} stale orders")
            # Push each affected farmer. Loop is small (rare batch) so
            # sequential is fine; fire-and-forget so one bad token
            # doesn't stall the sweep.
            farmer_ids = list({o.farmer_user_id for o in expired})
            farmers = (await db.execute(
                select(User).where(User.id.in_(farmer_ids))
            )).scalars().all()
            farmers_by_id = {f.id: f for f in farmers}
            for order in expired:
                farmer = farmers_by_id.get(order.farmer_user_id)
                if not farmer or not farmer.fcm_token:
                    continue
                try:
                    ref = order.reference_number or ""
                    body = (
                        f"Order {ref} has expired after 14 days without dealer action. "
                        "Re-route the items in RootsTalk to keep them moving."
                        if ref else EXPIRY_FCM_BODY
                    )
                    await send_fcm(
                        token=farmer.fcm_token,
                        title=EXPIRY_FCM_TITLE,
                        body=body,
                        data={
                            "type": "ORDER_EXPIRED",
                            "order_id": order.id,
                            "click_action": f"/crop-detail/{order.subscription_id}/orders",
                        },
                    )
                except Exception as e:
                    logger.error(
                        f"FCM send raised unexpectedly for farmer {farmer.id}: {e}"
                    )
