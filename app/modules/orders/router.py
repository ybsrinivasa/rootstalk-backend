from datetime import datetime, timezone, timedelta
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
from app.modules.advisory.models import Practice, Element, Timeline
from app.services.bl06_volume_calc import calculate_volume
from math import radians, cos, sin, asin, sqrt
from app.services.bl07_brand_options import get_brand_options
from app.modules.advisory.models import RelationType
from app.modules.subscriptions.models import PromoterAssignment, SubscriptionPaymentRequest, AssignmentStatus

router = APIRouter(tags=["Orders"])


class OrderCreate(BaseModel):
    subscription_id: str
    client_id: str
    date_from: datetime
    date_to: datetime
    practice_ids: list[str] = []
    dealer_user_id: Optional[str] = None
    facilitator_user_id: Optional[str] = None
    farm_area_acres: Optional[float] = None
    area_unit: Optional[str] = None


# ── Farmer: Create and manage orders ─────────────────────────────────────────

@router.post("/farmer/orders", status_code=201)
async def create_order(
    request: OrderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer places an order for inputs in a date range (BL-10).

    Acreage hard-lock: This endpoint is the DAS path (buy-all-dbs is the only DBS path).
    On the first DAS order, farm_area_confirmed_at is set, locking the area for all
    subsequent volume calculations. The acreage cannot be changed afterwards.
    """
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == request.subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # ── Timeline-type integrity: orders must NOT mix DBS / DAS / CALENDAR ─
    if request.practice_ids:
        practices_with_tl = (await db.execute(
            select(Practice, Timeline)
            .join(Timeline, Timeline.id == Practice.timeline_id)
            .where(Practice.id.in_(request.practice_ids))
        )).all()
        if not practices_with_tl:
            raise HTTPException(status_code=422, detail="No valid practices selected")
        timing_types = {tl.from_type.value if hasattr(tl.from_type, 'value') else str(tl.from_type)
                        for _, tl in practices_with_tl}
        if len(timing_types) > 1:
            raise HTTPException(
                status_code=422,
                detail="Cannot mix timing types in one order. Please order DBS, DAS, and Calendar items separately.",
            )

    # ── Hard-lock acreage on first DAS order ──────────────────────────────
    if not sub.farm_area_confirmed_at:
        if not request.farm_area_acres and not sub.farm_area_acres:
            raise HTTPException(
                status_code=422,
                detail="farm_area_acres required to confirm before this order",
            )
        if request.farm_area_acres:
            sub.farm_area_acres = request.farm_area_acres
            sub.area_unit = request.area_unit or sub.area_unit or "acres"
        sub.farm_area_confirmed_at = datetime.now(timezone.utc)

    order = Order(
        subscription_id=request.subscription_id,
        farmer_user_id=current_user.id,
        client_id=request.client_id,
        dealer_user_id=request.dealer_user_id,
        facilitator_user_id=request.facilitator_user_id,
        date_from=request.date_from,
        date_to=request.date_to,
        status=OrderStatus.SENT,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    db.add(order)
    await db.flush()

    for practice_id in request.practice_ids:
        practice = (await db.execute(select(Practice).where(Practice.id == practice_id))).scalar_one_or_none()
        relation_type = None
        if practice and practice.relation_id:
            from app.modules.advisory.models import Relation
            relation_row = (await db.execute(
                select(Relation).where(Relation.id == practice.relation_id)
            )).scalar_one_or_none()
            if relation_row:
                relation_type = relation_row.relation_type.value
        db.add(OrderItem(
            order_id=order.id,
            practice_id=practice_id,
            timeline_id=practice.timeline_id if practice else "",
            relation_id=practice.relation_id if practice else None,
            relation_type=relation_type,
            relation_role=practice.relation_role if practice else None,
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
    from app.modules.advisory.models import Relation
    from app.services.relations import (
        PracticeRef, build_structure, compute_count_display,
    )

    q = select(Order).where(Order.farmer_user_id == current_user.id).order_by(Order.created_at.desc())
    if status_filter:
        q = q.where(Order.status == status_filter)
    result = await db.execute(q)
    orders = result.scalars().all()
    out = []
    for o in orders:
        items_result = await db.execute(select(OrderItem).where(OrderItem.order_id == o.id))
        items = items_result.scalars().all()

        # Group items by relation_id; standalone = no relation or missing role
        by_relation: dict[str, list[OrderItem]] = {}
        standalone_items: list[OrderItem] = []
        for item in items:
            if item.relation_id and item.relation_role:
                by_relation.setdefault(item.relation_id, []).append(item)
            else:
                standalone_items.append(item)

        structures = []
        if by_relation:
            # Batch-fetch the practices and relations referenced
            practice_ids = list({i.practice_id for rel_items in by_relation.values() for i in rel_items})
            practices = (await db.execute(
                select(Practice).where(Practice.id.in_(practice_ids))
            )).scalars().all()
            practice_map = {p.id: p for p in practices}

            relations = (await db.execute(
                select(Relation).where(Relation.id.in_(list(by_relation.keys())))
            )).scalars().all()
            rel_type_map = {r.id: (r.relation_type.value if hasattr(r.relation_type, 'value') else str(r.relation_type))
                            for r in relations}

            for rel_id, rel_items in by_relation.items():
                practice_refs = []
                for item in rel_items:
                    prac = practice_map.get(item.practice_id)
                    if prac and item.relation_role:
                        practice_refs.append(PracticeRef(
                            practice_id=item.practice_id,
                            common_name_cosh_id=prac.common_name_cosh_id,
                            is_special_input=prac.is_special_input,
                            role=item.relation_role,
                        ))
                if not practice_refs:
                    continue
                rel_type = rel_type_map.get(rel_id, "OR")
                try:
                    structures.append(build_structure(practice_refs, rel_id, rel_type))
                except ValueError:
                    # Malformed roles — fall back to literal count for these items
                    standalone_items.extend(rel_items)

        cd = compute_count_display(structures, len(standalone_items))

        out.append({
            "id": o.id,
            "status": o.status,
            "date_from": o.date_from,
            "date_to": o.date_to,
            "dealer_user_id": o.dealer_user_id,
            "created_at": o.created_at,
            "item_count": cd.count,
            "is_max_count": cd.is_max,
        })
    return out


@router.get("/farmer/orders/{order_id}")
async def get_farmer_order_detail(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await _get_farmer_order(db, order_id, current_user.id)
    items_result = await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    items = items_result.scalars().all()
    return {
        "id": order.id, "status": order.status,
        "date_from": order.date_from, "date_to": order.date_to,
        "created_at": order.created_at,
        "dealer_user_id": order.dealer_user_id,
        "facilitator_user_id": order.facilitator_user_id,
        "items": [
            {
                "id": i.id, "practice_id": i.practice_id, "status": i.status,
                "relation_id": i.relation_id, "relation_type": i.relation_type,
                "brand_name": i.brand_name if i.status == OrderItemStatus.APPROVED else None,
                "given_volume": float(i.given_volume) if i.given_volume and i.status != OrderItemStatus.PENDING else None,
                "estimated_volume": float(i.estimated_volume) if i.estimated_volume else None,
                "volume_unit": i.volume_unit,
                "price": float(i.price) if i.price and i.status != OrderItemStatus.PENDING else None,
            }
            for i in items
        ],
    }


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
    """Approved purchased items with computed application date window.

    For each item, derive practice_date_from/to using the item's timeline anchor:
      - DAS:      crop_start_date + from/to_value (days after sowing)
      - DBS:      crop_start_date - from/to_value (days before sowing — pre-sowing)
      - CALENDAR: not date-anchored; returns null/null
    If crop_start_date is not set on the subscription yet, both dates are null
    and the frontend should prompt the farmer to set it.
    """
    from app.modules.advisory.models import Timeline, Practice as AdvPractice, TimelineFromType
    from datetime import timedelta

    rows = (await db.execute(
        select(OrderItem, Order, Timeline, AdvPractice, Subscription)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Timeline, Timeline.id == OrderItem.timeline_id)
        .join(AdvPractice, AdvPractice.id == OrderItem.practice_id)
        .join(Subscription, Subscription.id == Order.subscription_id)
        .where(
            Order.farmer_user_id == current_user.id,
            OrderItem.status == OrderItemStatus.APPROVED,
        )
        .order_by(Order.date_from.desc())
    )).all()

    out: list[dict] = []
    for item, order, tl, practice, sub in rows:
        date_from_iso = None
        date_to_iso = None
        crop_start = sub.crop_start_date
        if crop_start is not None:
            from_type_value = tl.from_type.value if hasattr(tl.from_type, 'value') else str(tl.from_type)
            crop_date = crop_start.date() if hasattr(crop_start, 'date') else crop_start
            if from_type_value == "DAS":
                df = crop_date + timedelta(days=int(tl.from_value))
                dt_ = crop_date + timedelta(days=int(tl.to_value))
                date_from_iso = df.isoformat()
                date_to_iso = dt_.isoformat()
            elif from_type_value == "DBS":
                # Before sowing: subtract. from_value is the larger # of days before;
                # to_value is the smaller (closer to sowing). Order ascending.
                d1 = crop_date - timedelta(days=int(tl.from_value))
                d2 = crop_date - timedelta(days=int(tl.to_value))
                df, dt_ = (d1, d2) if d1 <= d2 else (d2, d1)
                date_from_iso = df.isoformat()
                date_to_iso = dt_.isoformat()
            # CALENDAR: leave null for now (would need absolute reference dates)

        out.append({
            "id": item.id,
            "practice_id": item.practice_id,
            "brand_name": item.brand_name,
            "l1_type": practice.l1_type,
            "l2_type": practice.l2_type,
            "given_volume": float(item.given_volume) if item.given_volume is not None else None,
            "volume_unit": item.volume_unit,
            "price": float(item.price) if item.price is not None else None,
            "scan_verified": bool(item.scan_verified),
            "order_id": item.order_id,
            "created_at": item.created_at,
            "timeline_name": tl.name,
            "timeline_from_type": tl.from_type.value if hasattr(tl.from_type, 'value') else str(tl.from_type),
            "timeline_from_value": int(tl.from_value),
            "timeline_to_value": int(tl.to_value),
            "application_date_from": date_from_iso,
            "application_date_to": date_to_iso,
        })
    return out


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
    """BL-07: Dealer selects brand and enters volume/price before marking available.
    Automatically closes other OR-group items (NOT_NEEDED) if this item has a relation_id.
    """
    item = await _get_order_item(db, item_id, order_id)
    item.brand_cosh_id = data.get("brand_cosh_id")
    item.brand_name = data.get("brand_name") or None
    if data.get("given_volume") is not None:
        item.given_volume = data["given_volume"]
        item.volume_unit = data.get("volume_unit", "")
    if data.get("price") is not None:
        item.price = data["price"]
    item.status = OrderItemStatus.AVAILABLE

    # BL-07 OR-group auto-close: mark sibling OR items as NOT_NEEDED
    if item.relation_id and item.relation_type == "OR":
        siblings_result = await db.execute(
            select(OrderItem).where(
                OrderItem.order_id == order_id,
                OrderItem.relation_id == item.relation_id,
                OrderItem.id != item.id,
                OrderItem.status == OrderItemStatus.PENDING,
            )
        )
        for sibling in siblings_result.scalars().all():
            sibling.status = OrderItemStatus.NOT_NEEDED

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


@router.put("/admin/missing-brand-reports/{report_id}")
async def update_brand_report(
    report_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CM with BRAND_HANDLING privilege: review and approve/reject a missing brand report."""
    report = (await db.execute(
        select(MissingBrandReport).where(MissingBrandReport.id == report_id)
    )).scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if "status" in data:
        report.status = data["status"]
    if "cm_notes" in data:
        report.cm_notes = data["cm_notes"]
    await db.commit()
    return {"id": report_id, "status": report.status}


# ── BL-07: Brand options for an order item ───────────────────────────────────

@router.get("/dealer/orders/{order_id}/items/{item_id}/brand-options")
async def get_item_brand_options(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-07: Returns locked or unlocked brand options for a specific order item."""
    item = await _get_order_item(db, item_id, order_id)
    result = await get_brand_options(db, item.practice_id, current_user.id)
    return result.to_dict()


# ── Farmer: Item-level actions (BL-10) ────────────────────────────────────────

@router.delete("/farmer/orders/{order_id}/items/{item_id}")
async def remove_order_item(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-10: Farmer removes an item from order before approval."""
    order = await _get_farmer_order(db, order_id, current_user.id)
    if order.status in [OrderStatus.SENT_FOR_APPROVAL, OrderStatus.COMPLETED, OrderStatus.PARTIALLY_APPROVED]:
        raise HTTPException(status_code=400, detail="Cannot remove items after order sent for approval")
    item = await _get_order_item(db, item_id, order_id)
    item.status = OrderItemStatus.REMOVED
    await db.commit()
    return {"item_id": item_id, "status": item.status}


@router.put("/farmer/orders/{order_id}/items/{item_id}/try-another-dealer")
async def try_another_dealer(
    order_id: str, item_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-10: Farmer re-routes a NOT_AVAILABLE or REJECTED item to another dealer."""
    order = await _get_farmer_order(db, order_id, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    if item.status not in [OrderItemStatus.NOT_AVAILABLE, OrderItemStatus.REJECTED]:
        raise HTTPException(status_code=400, detail="Only NOT_AVAILABLE or REJECTED items can be re-routed")
    new_dealer_id = data.get("dealer_user_id")
    if not new_dealer_id:
        raise HTTPException(status_code=422, detail="dealer_user_id required")
    item.status = OrderItemStatus.PENDING
    item.brand_cosh_id = None
    item.brand_name = None
    item.given_volume = None
    item.price = None
    order.dealer_user_id = new_dealer_id
    order.status = OrderStatus.PROCESSING
    await db.commit()
    return {"item_id": item_id, "status": item.status, "new_dealer_user_id": new_dealer_id}


@router.put("/farmer/orders/{order_id}/items/{item_id}/skip")
async def skip_order_item(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-10: Farmer skips a NOT_AVAILABLE item for this ordering cycle."""
    await _get_farmer_order(db, order_id, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    if item.status != OrderItemStatus.NOT_AVAILABLE:
        raise HTTPException(status_code=400, detail="Only NOT_AVAILABLE items can be skipped")
    item.status = OrderItemStatus.SKIPPED
    await db.commit()
    return {"item_id": item_id, "status": item.status}


@router.put("/farmer/orders/{order_id}/items/approve-all")
async def approve_all_items(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-10: Farmer approves all items awaiting approval at once."""
    await _get_farmer_order(db, order_id, current_user.id)
    result = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order_id,
            OrderItem.status == OrderItemStatus.SENT_FOR_APPROVAL,
        )
    )
    items = result.scalars().all()
    if not items:
        raise HTTPException(status_code=400, detail="No items awaiting approval")
    for item in items:
        item.status = OrderItemStatus.APPROVED
    await _update_order_status(db, order_id)
    await db.commit()
    return {"approved_count": len(items)}


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


# ── Dealer: Delete order after packing list shared ───────────────────────────

@router.delete("/dealer/orders/{order_id}")
async def delete_dealer_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer can delete order only after packing list has been shared."""
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    pl = (await db.execute(select(PackingList).where(PackingList.order_id == order_id))).scalar_one_or_none()
    if not pl or not pl.first_shared_at:
        raise HTTPException(status_code=400, detail="Packing list must be shared before deleting")
    order.status = OrderStatus.COMPLETED
    order.dealer_user_id = None
    await db.commit()
    return {"detail": "Order removed from your queue"}


# ── Dealer / Facilitator: Promoted farmers ────────────────────────────────────

@router.get("/dealer/promoted-farmers")
async def dealer_promoted_farmers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _promoted_farmers(db, current_user.id)


@router.get("/facilitator/promoted-farmers")
async def facilitator_promoted_farmers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _promoted_farmers(db, current_user.id)


async def _promoted_farmers(db, promoter_user_id: str):
    result = await db.execute(
        select(PromoterAssignment).where(
            PromoterAssignment.promoter_user_id == promoter_user_id,
            PromoterAssignment.status == AssignmentStatus.ACTIVE,
        )
    )
    assignments = result.scalars().all()
    out = []
    for a in assignments:
        sub = (await db.execute(select(Subscription).where(Subscription.id == a.subscription_id))).scalar_one_or_none()
        if not sub:
            continue
        farmer = (await db.execute(select(User).where(User.id == sub.farmer_user_id))).scalar_one_or_none()
        out.append({
            "subscription_id": sub.id,
            "farmer_user_id": sub.farmer_user_id,
            "farmer_name": farmer.name if farmer else None,
            "farmer_phone": farmer.phone if farmer else None,
            "client_id": sub.client_id,
            "package_id": sub.package_id,
            "status": sub.status,
            "reference_number": sub.reference_number,
            "crop_start_date": sub.crop_start_date,
        })
    return out


# ── Facilitator: Accept / Reject / Confirm delivery / Return to farmer ────────

@router.put("/facilitator/orders/{order_id}/accept")
async def facilitator_accept_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.facilitator_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != OrderStatus.SENT:
        raise HTTPException(status_code=400, detail="Order can only be accepted when in SENT status")
    order.status = OrderStatus.ACCEPTED
    await db.commit()
    return {"order_id": order_id, "status": order.status}


@router.put("/facilitator/orders/{order_id}/reject")
async def facilitator_reject_order(
    order_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.facilitator_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != OrderStatus.SENT:
        raise HTTPException(status_code=400, detail="Order can only be rejected when in SENT status")
    order.status = OrderStatus.CANCELLED
    order.facilitator_user_id = None
    await db.commit()
    return {"order_id": order_id, "status": order.status, "reason": data.get("reason")}


@router.put("/facilitator/orders/{order_id}/confirm-delivery")
async def confirm_delivery(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator marks delivery done. Only enabled after delivery list shared."""
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.facilitator_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    pl = (await db.execute(select(PackingList).where(PackingList.order_id == order_id))).scalar_one_or_none()
    if not pl or not pl.first_shared_at:
        raise HTTPException(status_code=400, detail="Delivery list must be shared before confirming")
    order.status = OrderStatus.COMPLETED
    await db.commit()
    return {"order_id": order_id, "status": order.status}


@router.put("/facilitator/orders/{order_id}/return-to-farmer")
async def return_to_farmer(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator returns NOT_AVAILABLE items to farmer when unable to source."""
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.facilitator_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    items = (await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order_id,
            OrderItem.status == OrderItemStatus.NOT_AVAILABLE,
        )
    )).scalars().all()
    for item in items:
        item.status = OrderItemStatus.PENDING
        item.brand_cosh_id = None
        item.brand_name = None
    order.dealer_user_id = None
    order.status = OrderStatus.SENT
    await db.commit()
    return {"order_id": order_id, "returned_items": len(items)}


# ── Facilitator: Nearby dealers for forwarding ───────────────────────────────

@router.get("/facilitator/nearby-dealers")
async def nearby_dealers(
    order_type: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns up to 5 nearest dealers filtered by order type (PESTICIDE/FERTILISER/SEED)."""
    if lat is None:
        lat = float(current_user.gps_lat) if current_user.gps_lat else 0.0
    if lng is None:
        lng = float(current_user.gps_lng) if current_user.gps_lng else 0.0

    profiles = (await db.execute(select(DealerProfile))).scalars().all()

    category_map = {"PESTICIDE": "PESTICIDES", "FERTILISER": "FERTILISERS", "SEED": "SEEDS"}
    required_cat = category_map.get(order_type or "", "") if order_type else None

    results = []
    for profile in profiles:
        if required_cat:
            cats = profile.sell_categories or []
            if required_cat not in cats:
                continue
        if not profile.shop_gps_lat or not profile.shop_gps_lng:
            continue
        dist = _haversine(lat, lng, float(profile.shop_gps_lat), float(profile.shop_gps_lng))
        dealer = (await db.execute(select(User).where(User.id == profile.user_id))).scalar_one_or_none()
        if dealer:
            results.append({
                "user_id": dealer.id,
                "name": dealer.name,
                "phone": dealer.phone,
                "shop_name": profile.shop_name,
                "shop_address": profile.shop_address,
                "sell_categories": profile.sell_categories or [],
                "distance_km": round(dist, 1),
                "shop_gps_lat": float(profile.shop_gps_lat),
                "shop_gps_lng": float(profile.shop_gps_lng),
            })

    results.sort(key=lambda x: x["distance_km"])
    return results[:5]


def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return R * 2 * asin(sqrt(a))


# ── Facilitator: Payment requests ─────────────────────────────────────────────

@router.get("/facilitator/payment-requests")
async def facilitator_payment_requests(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.requested_from_user_id == current_user.id,
        ).order_by(SubscriptionPaymentRequest.created_at.desc())
    )
    rows = result.scalars().all()
    return [{"id": r.id, "subscription_id": r.subscription_id, "farmer_user_id": r.farmer_user_id,
             "amount": float(r.amount), "status": r.status, "expires_at": r.expires_at,
             "created_at": r.created_at} for r in rows]


@router.put("/facilitator/payment-requests/{request_id}/decline")
async def facilitator_decline_payment(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    req = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.id == request_id,
            SubscriptionPaymentRequest.requested_from_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404)
    req.status = "DECLINED"
    await db.commit()
    return {"id": request_id, "status": "DECLINED"}


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
