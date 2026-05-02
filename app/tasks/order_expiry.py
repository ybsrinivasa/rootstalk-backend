"""BL-10: Daily order expiry — mark stale unprocessed orders as EXPIRED."""
import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.modules.orders.models import Order, OrderStatus
from app.celery_app import celery_app

logger = logging.getLogger(__name__)

ORDER_EXPIRY_DAYS = 14


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
