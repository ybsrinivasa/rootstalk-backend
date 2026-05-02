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
    DealerProfile, DealerRelationship,
    OrderStatus, OrderItemStatus,
)
from app.modules.subscriptions.models import Subscription
from app.modules.sync.models import VolumeFormula
from app.modules.advisory.models import Practice, Element
from app.services.bl06_volume_calc import calculate_volume

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
    """BL-07: Dealer selects brand and enters volume/price before marking available."""
    item = await _get_order_item(db, item_id, order_id)
    item.brand_cosh_id = data.get("brand_cosh_id")
    item.brand_name = data.get("brand_name") or None
    if data.get("given_volume") is not None:
        item.given_volume = data["given_volume"]
        item.volume_unit = data.get("volume_unit", "")
    if data.get("price") is not None:
        item.price = data["price"]
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
    """BL-14: Sends all AVAILABLE items to farmer for approval."""
    result = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order_id,
            OrderItem.status == OrderItemStatus.AVAILABLE,
        )
    )
    available_items = result.scalars().all()
    if not available_items:
        raise HTTPException(status_code=400, detail="No available items to submit")

    volumes = data.get("items", {})
    for item in available_items:
        item_data = volumes.get(item.id, {})
        if item_data.get("given_volume"):
            item.given_volume = item_data["given_volume"]
            item.volume_unit = item_data.get("volume_unit", "")
        if item_data.get("price") is not None:
            item.price = item_data["price"]
        if not item.given_volume:
            raise HTTPException(status_code=422, detail=f"given_volume missing for item {item.id}")
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


# ── Dealer: Accept order ──────────────────────────────────────────────────────

@router.put("/dealer/orders/{order_id}/accept")
async def accept_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-10: Dealer accepts order, transitions SENT → PROCESSING."""
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != OrderStatus.SENT:
        raise HTTPException(status_code=400, detail="Order can only be accepted when in SENT status")
    order.status = OrderStatus.PROCESSING
    await db.commit()
    return {"order_id": order_id, "status": order.status}


# ── Dealer: Packing list structured content ────────────────────────────────────

@router.get("/dealer/orders/{order_id}/packing-list")
async def get_packing_list(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns structured packing list content for approved/completed orders."""
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status not in [OrderStatus.COMPLETED, OrderStatus.PARTIALLY_APPROVED, OrderStatus.SENT_FOR_APPROVAL]:
        raise HTTPException(status_code=400, detail="Packing list not available in current order status")

    items_result = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order_id,
            OrderItem.status.in_([OrderItemStatus.SENT_FOR_APPROVAL, OrderItemStatus.APPROVED]),
        )
    )
    items = items_result.scalars().all()

    farmer = (await db.execute(select(User).where(User.id == order.farmer_user_id))).scalar_one_or_none()

    return {
        "order_id": order.id,
        "status": order.status,
        "date_from": order.date_from,
        "date_to": order.date_to,
        "farmer_name": farmer.name if farmer else None,
        "farmer_phone": farmer.phone if farmer else None,
        "items": [
            {
                "id": i.id,
                "practice_id": i.practice_id,
                "brand_name": i.brand_name,
                "given_volume": float(i.given_volume) if i.given_volume else None,
                "volume_unit": i.volume_unit,
                "price": float(i.price) if i.price else None,
                "status": i.status,
            }
            for i in items
        ],
        "total_amount": sum(float(i.price) for i in items if i.price),
    }


# ── Dealer: Volume estimate (BL-06) ───────────────────────────────────────────

@router.get("/dealer/orders/{order_id}/items/{item_id}/volume-estimate")
async def get_volume_estimate(
    order_id: str,
    item_id: str,
    farm_area_acres: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-06: Returns estimated volume for a practice item based on farm area."""
    item = await _get_order_item(db, item_id, order_id)

    if farm_area_acres is None:
        sub = (await db.execute(
            select(Subscription).where(Subscription.id == (
                select(Order.subscription_id).where(Order.id == order_id).scalar_subquery()
            ))
        )).scalar_one_or_none()
        if sub:
            farm_area_acres = float(sub.farm_area_acres) if sub.farm_area_acres else None

    if not farm_area_acres:
        return {"estimated_volume": None, "volume_unit": None, "message": "Farm area not set on subscription"}

    practice = (await db.execute(
        select(Practice).where(Practice.id == item.practice_id)
    )).scalar_one_or_none()

    if not practice or not practice.l2_type:
        return {"estimated_volume": None, "volume_unit": None, "message": "Practice data not available"}

    formulas = (await db.execute(
        select(VolumeFormula).where(
            VolumeFormula.l2_practice == practice.l2_type,
            VolumeFormula.status == "ACTIVE",
        )
    )).scalars().all()

    if not formulas:
        return {"estimated_volume": None, "volume_unit": None, "message": "No formula found for this practice type"}

    formula_row = formulas[0]

    elements_result = await db.execute(
        select(Element).where(Element.practice_id == item.practice_id)
    )
    elements = {e.element_type: e.value for e in elements_result.scalars().all()}
    dosage = float(elements.get("dosage", 0)) if elements.get("dosage") else None

    result = calculate_volume(
        formula=formula_row.formula,
        brand_unit=formula_row.brand_unit,
        dosage=dosage,
        farm_area_acres=farm_area_acres,
    )
    if result is None:
        return {"estimated_volume": None, "volume_unit": None, "message": "Could not calculate estimate"}
    volume, unit = result
    return {"estimated_volume": volume, "volume_unit": unit, "formula_used": formula_row.formula}


# ── Dealer: Profile (what do you sell, shop details) ─────────────────────────

@router.get("/dealer/profile")
async def get_dealer_profile(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = (await db.execute(
        select(DealerProfile).where(DealerProfile.user_id == current_user.id)
    )).scalar_one_or_none()
    if not profile:
        return {"user_id": current_user.id, "sell_categories": [], "shop_name": None}
    return {
        "user_id": profile.user_id,
        "shop_name": profile.shop_name,
        "shop_address": profile.shop_address,
        "sell_categories": profile.sell_categories or [],
        "pesticide_licence_url": profile.pesticide_licence_url,
        "fertiliser_licence_url": profile.fertiliser_licence_url,
        "shop_registration_url": profile.shop_registration_url,
        "shop_photo_url": profile.shop_photo_url,
        "shop_gps_lat": float(profile.shop_gps_lat) if profile.shop_gps_lat else None,
        "shop_gps_lng": float(profile.shop_gps_lng) if profile.shop_gps_lng else None,
    }


@router.put("/dealer/profile")
async def upsert_dealer_profile(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = (await db.execute(
        select(DealerProfile).where(DealerProfile.user_id == current_user.id)
    )).scalar_one_or_none()

    if not profile:
        profile = DealerProfile(user_id=current_user.id)
        db.add(profile)

    allowed = ["shop_name", "shop_address", "sell_categories", "pesticide_licence_url",
               "fertiliser_licence_url", "shop_registration_url", "shop_photo_url",
               "shop_gps_lat", "shop_gps_lng"]
    for field in allowed:
        if field in data:
            setattr(profile, field, data[field])

    await db.commit()
    return {"detail": "Profile saved"}


# ── Dealer: Dealerships (manufacturer relationships) ─────────────────────────

@router.get("/dealer/dealerships")
async def list_dealerships(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(DealerRelationship).where(
            DealerRelationship.dealer_user_id == current_user.id,
            DealerRelationship.status == "ACTIVE",
        ).order_by(DealerRelationship.manufacturer_name)
    )
    rows = result.scalars().all()
    return [{"id": r.id, "manufacturer_name": r.manufacturer_name,
             "manufacturer_client_id": r.manufacturer_client_id} for r in rows]


@router.post("/dealer/dealerships", status_code=201)
async def add_dealership(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rel = DealerRelationship(
        dealer_user_id=current_user.id,
        manufacturer_name=data["manufacturer_name"],
        manufacturer_client_id=data.get("manufacturer_client_id"),
    )
    db.add(rel)
    await db.commit()
    return {"id": rel.id, "manufacturer_name": rel.manufacturer_name}


@router.delete("/dealer/dealerships/{rel_id}")
async def remove_dealership(
    rel_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rel = (await db.execute(
        select(DealerRelationship).where(
            DealerRelationship.id == rel_id,
            DealerRelationship.dealer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not rel:
        raise HTTPException(status_code=404, detail="Dealership not found")
    rel.status = "INACTIVE"
    await db.commit()
    return {"detail": "Removed"}


# ── Farmer: Set farm area on subscription ─────────────────────────────────────

@router.put("/farmer/subscriptions/{sub_id}/farm-area")
async def set_farm_area(
    sub_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == sub_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    sub.farm_area_acres = data.get("farm_area_acres")
    sub.area_unit = data.get("area_unit", "acres")
    await db.commit()
    return {"sub_id": sub_id, "farm_area_acres": sub.farm_area_acres, "area_unit": sub.area_unit}


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
