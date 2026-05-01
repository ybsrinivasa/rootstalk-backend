from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.orders.models import (
    Order, OrderItem, SeedOrder, PackingList, MissingBrandReport,
    OrderStatus, OrderItemStatus,
)
from app.modules.subscriptions.models import Subscription

router = APIRouter(tags=["Orders"])


class OrderCreate(BaseModel):
    subscription_id: str
    client_id: str
    date_from: datetime
    date_to: datetime
    practice_ids: list[str] = []
    dealer_user_id: Optional[str] = None
    facilitator_user_id: Optional[str] = None


# ── Farmer: Create and manage orders ─────────────────────────────────────────

@router.post("/farmer/orders", status_code=201)
async def create_order(
    request: OrderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer places an order for inputs in a date range (BL-10)."""
    order = Order(
        subscription_id=request.subscription_id,
        farmer_user_id=current_user.id,
        client_id=request.client_id,
        dealer_user_id=request.dealer_user_id,
        facilitator_user_id=request.facilitator_user_id,
        date_from=request.date_from,
        date_to=request.date_to,
        status=OrderStatus.SENT,
    )
    db.add(order)
    await db.flush()

    for practice_id in request.practice_ids:
        db.add(OrderItem(
            order_id=order.id,
            practice_id=practice_id,
            timeline_id="",  # resolved from practice in production
            status=OrderItemStatus.PENDING,
        ))

    await db.commit()
    await db.refresh(order)
    return {"id": order.id, "status": order.status}


@router.get("/farmer/orders")
async def list_farmer_orders(
    status_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(Order).where(Order.farmer_user_id == current_user.id).order_by(Order.created_at.desc())
    if status_filter:
        q = q.where(Order.status == status_filter)
    result = await db.execute(q)
    orders = result.scalars().all()
    return [{"id": o.id, "status": o.status, "date_from": o.date_from, "date_to": o.date_to,
             "dealer_user_id": o.dealer_user_id, "created_at": o.created_at} for o in orders]


@router.put("/farmer/orders/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await _get_farmer_order(db, order_id, current_user.id)
    if order.status not in [OrderStatus.SENT, OrderStatus.DRAFT]:
        raise HTTPException(status_code=400, detail="Can only cancel orders that have not been accepted by dealer")
    order.status = OrderStatus.CANCELLED
    await db.commit()
    return {"status": order.status}


@router.put("/farmer/orders/{order_id}/items/{item_id}/approve")
async def approve_order_item(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-14: Farmer approves dealer's volume and price."""
    item = await _get_order_item(db, item_id, order_id)
    if item.status != OrderItemStatus.SENT_FOR_APPROVAL:
        raise HTTPException(status_code=400, detail="Item is not awaiting approval")
    item.status = OrderItemStatus.APPROVED
    await _update_order_status(db, order_id)
    await db.commit()
    return {"item_id": item_id, "status": item.status}


@router.put("/farmer/orders/{order_id}/items/{item_id}/reject")
async def reject_order_item(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await _get_order_item(db, item_id, order_id)
    item.status = OrderItemStatus.REJECTED
    await db.commit()
    return {"item_id": item_id, "status": item.status}


@router.get("/farmer/purchased-items")
async def list_purchased_items(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.farmer_user_id == current_user.id,
            OrderItem.status == OrderItemStatus.APPROVED,
        )
        .order_by(Order.date_from.desc())
    )
    items = result.scalars().all()
    return [{"id": i.id, "practice_id": i.practice_id, "brand_name": i.brand_name,
             "given_volume": i.given_volume, "volume_unit": i.volume_unit, "price": i.price} for i in items]


# ── Dealer: Process orders ─────────────────────────────────────────────────────

@router.get("/dealer/orders")
async def list_dealer_orders(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Order).where(
            Order.dealer_user_id == current_user.id,
            Order.status.notin_([OrderStatus.CANCELLED, OrderStatus.EXPIRED]),
        ).order_by(Order.created_at.desc())
    )
    orders = result.scalars().all()
    return [{"id": o.id, "status": o.status, "farmer_user_id": o.farmer_user_id,
             "date_from": o.date_from, "date_to": o.date_to} for o in orders]


@router.put("/dealer/orders/{order_id}/items/{item_id}/available")
async def mark_item_available(
    order_id: str, item_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-07: Dealer selects brand (mandatory before marking available)."""
    if not data.get("brand_cosh_id"):
        raise HTTPException(status_code=422, detail="brand_cosh_id is required before marking available")
    item = await _get_order_item(db, item_id, order_id)
    item.brand_cosh_id = data["brand_cosh_id"]
    item.brand_name = data.get("brand_name", "")
    item.status = OrderItemStatus.AVAILABLE
    await db.commit()
    return {"item_id": item_id, "status": item.status}


@router.put("/dealer/orders/{order_id}/items/{item_id}/postpone")
async def postpone_item(
    order_id: str, item_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await _get_order_item(db, item_id, order_id)
    item.status = OrderItemStatus.POSTPONED
    item.postponed_until = data.get("postponed_until")
    await db.commit()
    return {"item_id": item_id, "status": item.status, "postponed_until": item.postponed_until}


@router.put("/dealer/orders/{order_id}/items/{item_id}/not-available")
async def mark_item_unavailable(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await _get_order_item(db, item_id, order_id)
    item.status = OrderItemStatus.NOT_AVAILABLE
    await db.commit()
    return {"item_id": item_id, "status": item.status}


@router.put("/dealer/orders/{order_id}/submit-for-approval")
async def submit_for_approval(
    order_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-14: Dealer enters volume/price and sends to farmer for approval."""
    result = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order_id,
            OrderItem.status == OrderItemStatus.AVAILABLE,
        )
    )
    available_items = result.scalars().all()
    volumes = data.get("items", {})

    for item in available_items:
        item_data = volumes.get(item.id, {})
        if not item_data.get("given_volume"):
            raise HTTPException(status_code=422, detail=f"given_volume required for item {item.id}")
        item.given_volume = item_data["given_volume"]
        item.volume_unit = item_data.get("volume_unit", "")
        item.price = item_data.get("price")
        item.status = OrderItemStatus.SENT_FOR_APPROVAL

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one()
    order.status = OrderStatus.SENT_FOR_APPROVAL
    await db.commit()
    return {"order_id": order_id, "status": order.status}


@router.put("/dealer/orders/{order_id}/abort")
async def abort_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-10: Dealer aborts — all items revert to PENDING, order reverts to SENT."""
    result = await db.execute(
        select(OrderItem).where(OrderItem.order_id == order_id)
    )
    items = result.scalars().all()
    for item in items:
        item.status = OrderItemStatus.PENDING
        item.brand_cosh_id = None
        item.brand_name = None

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one()
    order.status = OrderStatus.SENT
    await db.commit()
    return {"order_id": order_id, "status": order.status}


# ── Packing List ──────────────────────────────────────────────────────────────

@router.post("/dealer/orders/{order_id}/packing-list/generate")
async def generate_packing_list(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate and store packing list. PDF generation wired to S3 in production."""
    existing = (await db.execute(
        select(PackingList).where(PackingList.order_id == order_id)
    )).scalar_one_or_none()
    if not existing:
        pl = PackingList(order_id=order_id, pdf_url=f"/packing/{order_id}.pdf")
        db.add(pl)
        await db.commit()
        await db.refresh(pl)
        return {"packing_list_id": pl.id, "pdf_url": pl.pdf_url}
    return {"packing_list_id": existing.id, "pdf_url": existing.pdf_url}


@router.put("/dealer/orders/{order_id}/packing-list/mark-shared")
async def mark_packing_list_shared(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pl = (await db.execute(select(PackingList).where(PackingList.order_id == order_id))).scalar_one_or_none()
    if not pl:
        raise HTTPException(status_code=404, detail="Packing list not found")
    if not pl.first_shared_at:
        pl.first_shared_at = datetime.now(timezone.utc)
    await db.commit()
    return {"detail": "Marked as shared", "first_shared_at": pl.first_shared_at}


# ── Facilitator: Route and handle orders ──────────────────────────────────────

@router.get("/facilitator/orders")
async def list_facilitator_orders(
    status_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Orders routed to this facilitator for handling."""
    q = select(Order).where(Order.facilitator_user_id == current_user.id).order_by(Order.created_at.desc())
    if status_filter:
        q = q.where(Order.status == status_filter)
    result = await db.execute(q)
    orders = result.scalars().all()
    out = []
    for o in orders:
        items_result = await db.execute(select(OrderItem).where(OrderItem.order_id == o.id))
        items = items_result.scalars().all()
        out.append({
            "id": o.id, "status": o.status,
            "farmer_user_id": o.farmer_user_id, "client_id": o.client_id,
            "dealer_user_id": o.dealer_user_id,
            "date_from": o.date_from, "date_to": o.date_to,
            "created_at": o.created_at,
            "item_count": len(items),
            "pending_count": sum(1 for i in items if i.status == OrderItemStatus.PENDING),
        })
    return out


@router.put("/facilitator/orders/{order_id}/route-to-dealer")
async def route_order_to_dealer(
    order_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator assigns a dealer to handle a specific order."""
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.facilitator_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found or not assigned to you")
    if order.status not in [OrderStatus.SENT, OrderStatus.ACCEPTED]:
        raise HTTPException(status_code=400, detail="Order cannot be routed in current status")
    order.dealer_user_id = data["dealer_user_id"]
    order.status = OrderStatus.PROCESSING
    await db.commit()
    return {"id": order.id, "status": order.status, "dealer_user_id": order.dealer_user_id}


@router.get("/facilitator/orders/{order_id}")
async def get_facilitator_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.facilitator_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    items_result = await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    items = items_result.scalars().all()
    return {
        "id": order.id, "status": order.status,
        "farmer_user_id": order.farmer_user_id, "client_id": order.client_id,
        "dealer_user_id": order.dealer_user_id,
        "date_from": order.date_from, "date_to": order.date_to,
        "created_at": order.created_at,
        "items": [
            {
                "id": i.id, "practice_id": i.practice_id,
                "status": i.status, "brand_cosh_id": i.brand_cosh_id,
                "brand_name": i.brand_name, "given_volume": float(i.given_volume) if i.given_volume else None,
                "volume_unit": i.volume_unit, "price": float(i.price) if i.price else None,
            }
            for i in items
        ],
    }


# ── Dealer: Get order detail with items ────────────────────────────────────────

@router.get("/dealer/orders/{order_id}")
async def get_dealer_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    items_result = await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    items = items_result.scalars().all()
    return {
        "id": order.id, "status": order.status,
        "farmer_user_id": order.farmer_user_id, "client_id": order.client_id,
        "facilitator_user_id": order.facilitator_user_id,
        "date_from": order.date_from, "date_to": order.date_to,
        "created_at": order.created_at,
        "items": [
            {
                "id": i.id, "practice_id": i.practice_id,
                "status": i.status, "brand_cosh_id": i.brand_cosh_id,
                "brand_name": i.brand_name,
                "given_volume": float(i.given_volume) if i.given_volume else None,
                "estimated_volume": float(i.estimated_volume) if i.estimated_volume else None,
                "volume_unit": i.volume_unit, "price": float(i.price) if i.price else None,
            }
            for i in items
        ],
    }


# ── Missing Brand Reports ─────────────────────────────────────────────────────

@router.post("/dealer/missing-brand-reports", status_code=201)
async def report_missing_brand(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    report = MissingBrandReport(
        dealer_user_id=current_user.id,
        order_item_id=data["order_item_id"],
        brand_name_reported=data["brand_name_reported"],
        manufacturer_name=data.get("manufacturer_name"),
        l2_practice=data.get("l2_practice"),
        additional_info=data.get("additional_info"),
    )
    db.add(report)
    await db.commit()
    return {"id": report.id, "status": report.status}


@router.get("/admin/missing-brand-reports")
async def list_missing_brand_reports(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(MissingBrandReport).order_by(MissingBrandReport.created_at.desc()))
    return result.scalars().all()


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_farmer_order(db: AsyncSession, order_id: str, farmer_user_id: str) -> Order:
    result = await db.execute(
        select(Order).where(Order.id == order_id, Order.farmer_user_id == farmer_user_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


async def _get_order_item(db: AsyncSession, item_id: str, order_id: str) -> OrderItem:
    result = await db.execute(
        select(OrderItem).where(OrderItem.id == item_id, OrderItem.order_id == order_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Order item not found")
    return item


async def _update_order_status(db: AsyncSession, order_id: str):
    result = await db.execute(select(OrderItem).where(OrderItem.order_id == order_id))
    items = result.scalars().all()
    approval_items = [i for i in items if i.status in [OrderItemStatus.SENT_FOR_APPROVAL, OrderItemStatus.APPROVED]]
    approved = [i for i in items if i.status == OrderItemStatus.APPROVED]
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one()
    if len(approved) == len(approval_items) and len(approved) > 0:
        order.status = OrderStatus.COMPLETED
    elif len(approved) > 0:
        order.status = OrderStatus.PARTIALLY_APPROVED
