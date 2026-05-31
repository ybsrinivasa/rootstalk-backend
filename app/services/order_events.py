"""Audit log helper for Order Management V2.

Every state change on an item (or order-level event) gets a row in
`order_item_events`. Reports group by `lineage_id` to reconstruct
the full journey — "pesticide P1 went to D1 (returned) → via F1 to
D2 (postponed 3 days) → purchased".

This module deliberately exposes one small function. It does not
commit; the caller controls the transaction so events land
atomically with the state change they describe.
"""
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.modules.orders.models import OrderItemEvent


# Actor-role constants kept here so endpoint code doesn't need to
# import from the BL10 state-machine module just for the strings.
FARMER = "FARMER"
DEALER = "DEALER"
FACILITATOR = "FACILITATOR"
SYSTEM = "SYSTEM"


async def record_event(
    db: AsyncSession,
    *,
    lineage_id: str,
    event_type: str,
    actor_user_id: Optional[str] = None,
    actor_role: Optional[str] = None,
    order_id: Optional[str] = None,
    order_item_id: Optional[str] = None,
    seed_order_id: Optional[str] = None,
    prev_status: Optional[str] = None,
    new_status: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> OrderItemEvent:
    """Append a row to order_item_events.

    `lineage_id` is required even for order-level events: pass the
    order's own id when there's no item context (e.g. CANCELLED_BY_FARMER
    on the husk). Order ids and item lineage ids are all UUIDs from
    the same namespace so collisions don't happen in practice.
    """
    event = OrderItemEvent(
        lineage_id=lineage_id,
        event_type=event_type,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        order_id=order_id,
        order_item_id=order_item_id,
        seed_order_id=seed_order_id,
        prev_status=prev_status,
        new_status=new_status,
        event_metadata=metadata,
    )
    db.add(event)
    return event
