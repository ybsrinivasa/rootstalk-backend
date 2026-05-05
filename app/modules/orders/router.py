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

    # ── Pass 1: resolve practice rows + build relation_type map ──────────
    item_specs: list[dict] = []
    timeline_ids_in_order: set[str] = set()
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
        if practice and practice.timeline_id:
            timeline_ids_in_order.add(practice.timeline_id)
        item_specs.append({
            "practice_id": practice_id,
            "timeline_id": practice.timeline_id if practice else "",
            "relation_id": practice.relation_id if practice else None,
            "relation_type": relation_type,
            "relation_role": practice.relation_role if practice else None,
        })

    # ── Pass 2: take snapshots BEFORE creating items (Phase 3.2) ─────────
    # Each item carries a permanent pointer to the snapshot in force at
    # order-create time. The dealer's read path follows this pointer.
    from app.services.snapshot_triggers import take_snapshots_for_keys  # noqa: F401 (legacy import kept warm)
    from app.services.snapshot import take_snapshot
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    snap_id_by_tl: dict[str, Optional[str]] = {}
    for tl_id in timeline_ids_in_order:
        try:
            snap = await take_snapshot(
                db, request.subscription_id, tl_id, "PURCHASE_ORDER", source="CCA",
            )
            snap_id_by_tl[tl_id] = snap.id
        except Exception as exc:  # noqa: BLE001 — best-effort; nightly sweep retries
            _logger.warning(
                "PO snapshot capture failed sub=%s tl=%s: %s",
                request.subscription_id, tl_id, exc,
            )
            snap_id_by_tl[tl_id] = None

    # ── Pass 3: create OrderItems with snapshot_id pointer ───────────────
    for spec in item_specs:
        db.add(OrderItem(
            order_id=order.id,
            practice_id=spec["practice_id"],
            timeline_id=spec["timeline_id"],
            relation_id=spec["relation_id"],
            relation_type=spec["relation_type"],
            relation_role=spec["relation_role"],
            snapshot_id=snap_id_by_tl.get(spec["timeline_id"]),
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
            "frequency_days": int(practice.frequency_days) if practice.frequency_days else None,
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

    Brand discipline (BL-07 audit, 2026-05-05):
      `brand_cosh_id` is REQUIRED and MUST refer to an active row in
      `cosh_reference_cache` with entity_type='brand'. Free-text or
      unknown identifiers are rejected with stable error codes
      (BRAND_REQUIRED / BRAND_NOT_IN_SYSTEM) so downstream analytics —
      brand comparisons, sale tracking, manufacturer reports, spelling
      consistency — stay reliable. The dealer's typed `brand_name` is
      ignored; the canonical English name from cosh translations is
      stored on the row instead. If a real brand truly isn't in the
      system, the dealer should use POST /dealer/missing-brand-reports
      to flag it for the CM.

    Part-aware sibling handling (Build C):
      Same Part, different Option  -> mark sibling NOT_AVAILABLE (returned to farmer)
      Same Part, same Option       -> leave alone (compound AND group; dealer fills these)
      Different Part               -> leave alone (dealer processes that Part separately)

    Falls back to flat OR-group closure if the relation_role is missing or malformed.
    """
    from app.services.relations import decode_role
    from app.modules.sync.models import CoshReferenceCache

    item = await _get_order_item(db, item_id, order_id)

    # ── BL-07 strict brand validation ─────────────────────────────────────
    brand_cosh_id = (data.get("brand_cosh_id") or "").strip()
    if not brand_cosh_id:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "BRAND_REQUIRED",
                "message": (
                    "brand_cosh_id is required. Pick a brand from the "
                    "system list, or POST /dealer/missing-brand-reports "
                    "if the brand isn't available."
                ),
            },
        )

    brand_row = (await db.execute(
        select(CoshReferenceCache).where(
            CoshReferenceCache.cosh_id == brand_cosh_id,
            CoshReferenceCache.entity_type == "brand",
            CoshReferenceCache.status == "active",
        )
    )).scalar_one_or_none()
    if brand_row is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "BRAND_NOT_IN_SYSTEM",
                "message": (
                    f"Brand '{brand_cosh_id}' is not in the active system "
                    "list. Pick a different brand, or POST /dealer/"
                    "missing-brand-reports to flag it for the CM."
                ),
            },
        )

    # Canonicalise brand_name from cosh — dealer's typed value ignored so
    # spellings are 100% consistent across the system.
    canonical_name = (brand_row.translations or {}).get("en") or brand_cosh_id

    item.brand_cosh_id = brand_cosh_id
    item.brand_name = canonical_name
    if data.get("given_volume") is not None:
        item.given_volume = data["given_volume"]
        item.volume_unit = data.get("volume_unit", "")
    if data.get("price") is not None:
        item.price = data["price"]
    item.status = OrderItemStatus.AVAILABLE

    # Part-aware sibling handling
    if item.relation_id and item.relation_role:
        try:
            my_coords = decode_role(item.relation_role)
            siblings_result = await db.execute(
                select(OrderItem).where(
                    OrderItem.order_id == order_id,
                    OrderItem.relation_id == item.relation_id,
                    OrderItem.id != item.id,
                )
            )
            for sibling in siblings_result.scalars().all():
                if not sibling.relation_role:
                    continue
                try:
                    s_coords = decode_role(sibling.relation_role)
                except ValueError:
                    continue
                # Same Part, different Option -> mark NOT_AVAILABLE (returned to farmer)
                if (
                    s_coords.part == my_coords.part
                    and s_coords.option != my_coords.option
                    and sibling.status == OrderItemStatus.PENDING
                ):
                    sibling.status = OrderItemStatus.NOT_AVAILABLE
                # Same Part, same Option (compound AND) -> leave alone, dealer fills these
                # Different Part -> leave alone, dealer processes that Part separately
        except ValueError:
            # Malformed role: fall back to legacy flat OR closure
            if item.relation_type == "OR":
                fb_result = await db.execute(
                    select(OrderItem).where(
                        OrderItem.order_id == order_id,
                        OrderItem.relation_id == item.relation_id,
                        OrderItem.id != item.id,
                        OrderItem.status == OrderItemStatus.PENDING,
                    )
                )
                for sibling in fb_result.scalars().all():
                    sibling.status = OrderItemStatus.NOT_AVAILABLE
    elif item.relation_id and item.relation_type == "OR":
        # No relation_role at all (legacy data) — preserve original flat OR closure
        fb_result = await db.execute(
            select(OrderItem).where(
                OrderItem.order_id == order_id,
                OrderItem.relation_id == item.relation_id,
                OrderItem.id != item.id,
                OrderItem.status == OrderItemStatus.PENDING,
            )
        )
        for sibling in fb_result.scalars().all():
            sibling.status = OrderItemStatus.NOT_AVAILABLE

    await db.commit()
    return {"item_id": item_id, "status": item.status}


@router.post("/dealer/orders/{order_id}/relations/{relation_id}/parts/{part_index}/select-option")
async def select_option(
    order_id: str, relation_id: str, part_index: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer selects an Option for a Part atomically.
    All items in that Option become AVAILABLE; items in other Options of this Part
    become NOT_AVAILABLE. Brand selection then happens per item via the existing
    /available endpoint.
    Body: { option_index: int }
    """
    from app.services.relations import decode_role

    option_index = data.get("option_index")
    if option_index is None:
        raise HTTPException(status_code=422, detail="option_index required")

    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    items = (await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order_id,
            OrderItem.relation_id == relation_id,
        )
    )).scalars().all()
    if not items:
        raise HTTPException(status_code=404, detail="Relation not in this order")

    affected = {"available": 0, "not_available": 0}
    for item in items:
        if not item.relation_role:
            continue
        try:
            coords = decode_role(item.relation_role)
        except ValueError:
            continue
        if coords.part != part_index:
            continue
        if coords.option == option_index:
            item.status = OrderItemStatus.AVAILABLE
            affected["available"] += 1
        else:
            item.status = OrderItemStatus.NOT_AVAILABLE
            affected["not_available"] += 1

    # TODO(FCM): when all options in a Part end up NOT_AVAILABLE, push notification
    # to farmer that this Part of the relation could not be fulfilled.
    await db.commit()
    return {"part_index": part_index, "selected_option": option_index, **affected}


@router.post("/dealer/orders/{order_id}/relations/{relation_id}/parts/{part_index}/check-duplicate")
async def check_duplicate(
    order_id: str, relation_id: str, part_index: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Runtime duplicate check for a candidate Option.
    Compares its common_name_cosh_ids against AVAILABLE items in OTHER Parts of
    the order (any relation, plus standalone). Special inputs are exempt.
    Body: { option_index: int }
    Returns: { would_duplicate, duplicate_input_name, suggested_alternatives }
    """
    from app.services.relations import decode_role

    option_index = data.get("option_index")
    if option_index is None:
        raise HTTPException(status_code=422, detail="option_index required")

    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order_id)
    )).scalars().all()
    practice_ids = list({i.practice_id for i in items if i.practice_id})
    practices = (await db.execute(
        select(Practice).where(Practice.id.in_(practice_ids))
    )).scalars().all() if practice_ids else []
    practice_map = {p.id: p for p in practices}

    # committed_set: AVAILABLE items in OTHER Parts (excluding this Part of this relation)
    committed_cn_ids: set[str] = set()
    for item in items:
        if item.status != OrderItemStatus.AVAILABLE:
            continue
        if item.relation_id == relation_id and item.relation_role:
            try:
                c = decode_role(item.relation_role)
                if c.part == part_index:
                    continue  # same Part is what we're evaluating
            except ValueError:
                pass
        prac = practice_map.get(item.practice_id)
        if prac and prac.common_name_cosh_id and not prac.is_special_input:
            committed_cn_ids.add(prac.common_name_cosh_id)

    # Build per-Option cn_id sets for this Part
    options_in_part: dict[int, set[str]] = {}
    for item in items:
        if item.relation_id != relation_id or not item.relation_role:
            continue
        try:
            c = decode_role(item.relation_role)
        except ValueError:
            continue
        if c.part != part_index:
            continue
        prac = practice_map.get(item.practice_id)
        if prac and prac.common_name_cosh_id and not prac.is_special_input:
            options_in_part.setdefault(c.option, set()).add(prac.common_name_cosh_id)

    candidate_cn_ids = options_in_part.get(option_index, set())
    overlap = committed_cn_ids & candidate_cn_ids
    if not overlap:
        return {"would_duplicate": False, "duplicate_input_name": None, "suggested_alternatives": []}

    suggested = sorted(
        opt_idx for opt_idx, opt_cn_ids in options_in_part.items()
        if opt_idx != option_index and not (opt_cn_ids & committed_cn_ids)
    )

    return {
        "would_duplicate": True,
        "duplicate_input_name": next(iter(overlap)),
        "suggested_alternatives": suggested,
    }


@router.post("/dealer/orders/{order_id}/relations/{relation_id}/parts/{part_index}/mark-option-not-available")
async def mark_option_not_available(
    order_id: str, relation_id: str, part_index: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer marks an entire Option as not available without affecting other Options.
    All items in (Part, Option) are set to NOT_AVAILABLE. Other Options remain in
    their current state, allowing the dealer to choose another Option.
    Body: { option_index: int }
    """
    from app.services.relations import decode_role

    option_index = data.get("option_index")
    if option_index is None:
        raise HTTPException(status_code=422, detail="option_index required")

    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    items = (await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order_id,
            OrderItem.relation_id == relation_id,
        )
    )).scalars().all()
    if not items:
        raise HTTPException(status_code=404, detail="Relation not in this order")

    affected = 0
    for item in items:
        if not item.relation_role:
            continue
        try:
            coords = decode_role(item.relation_role)
        except ValueError:
            continue
        if coords.part == part_index and coords.option == option_index:
            item.status = OrderItemStatus.NOT_AVAILABLE
            affected += 1

    # TODO(FCM): if this closes the last open Option in the Part, push notification
    # to farmer that this Part of the relation could not be fulfilled.
    await db.commit()
    return {"part_index": part_index, "option_index": option_index, "not_available": affected}


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
    """Dealer order detail with Part-aware relation structure (Build C).

    Response includes both the legacy flat `items` array (unchanged for backward
    compat) and a new `relations` array. Each relation lists Parts → Options →
    items with progressive-reveal flags.
    """
    from app.services.relations import decode_role

    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    items_result = await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    items = items_result.scalars().all()

    # Helper for the flat item shape
    def item_brief(i: OrderItem) -> dict:
        return {
            "id": i.id, "practice_id": i.practice_id,
            "status": i.status.value if hasattr(i.status, "value") else i.status,
            "brand_cosh_id": i.brand_cosh_id,
            "brand_name": i.brand_name,
            "given_volume": float(i.given_volume) if i.given_volume else None,
            "estimated_volume": float(i.estimated_volume) if i.estimated_volume else None,
            "volume_unit": i.volume_unit,
            "price": float(i.price) if i.price else None,
            "relation_id": i.relation_id,
            "relation_type": i.relation_type,
            "relation_role": i.relation_role,
        }

    # Group items by relation
    by_relation: dict[str, list[OrderItem]] = {}
    standalone: list[OrderItem] = []
    for i in items:
        if i.relation_id and i.relation_role:
            by_relation.setdefault(i.relation_id, []).append(i)
        else:
            standalone.append(i)

    # Batch-load practices and elements for locked-brand detection
    all_practice_ids = list({i.practice_id for i in items if i.practice_id})
    practice_map: dict[str, Practice] = {}
    elements_by_practice: dict[str, list[Element]] = {}
    if all_practice_ids:
        practices = (await db.execute(
            select(Practice).where(Practice.id.in_(all_practice_ids))
        )).scalars().all()
        practice_map = {p.id: p for p in practices}
        elements = (await db.execute(
            select(Element).where(Element.practice_id.in_(all_practice_ids))
        )).scalars().all()
        for e in elements:
            elements_by_practice.setdefault(e.practice_id, []).append(e)

    # ── Phase 3.3: per-item snapshot resolution ──────────────────────────
    # Each item that was created post-Phase-3.2 carries a permanent pointer
    # to the locked_timeline_snapshot in force at order-create time. When
    # present, this snapshot is the source of truth for brand-lock state —
    # SE edits to master practice elements made AFTER order placement
    # cannot bleed into the dealer's view of THIS order (Rule 5).
    from app.modules.subscriptions.snapshot_models import LockedTimelineSnapshot
    from app.services.bl07_brand_options import _practice_elements_from_snapshot

    snap_ids_in_order = list({i.snapshot_id for i in items if i.snapshot_id})
    snapshots_by_id: dict[str, LockedTimelineSnapshot] = {}
    if snap_ids_in_order:
        snap_rows = (await db.execute(
            select(LockedTimelineSnapshot).where(
                LockedTimelineSnapshot.id.in_(snap_ids_in_order)
            )
        )).scalars().all()
        snapshots_by_id = {s.id: s for s in snap_rows}

    def _elements_for_item(it: OrderItem):
        """Return element list for this item — from snapshot if linked,
        else from master."""
        if it.snapshot_id and it.snapshot_id in snapshots_by_id:
            snap_els = _practice_elements_from_snapshot(
                snapshots_by_id[it.snapshot_id], it.practice_id,
            )
            if snap_els is not None:
                return snap_els
        return elements_by_practice.get(it.practice_id, [])

    def has_locked_brand_item(it: OrderItem) -> bool:
        for e in _elements_for_item(it):
            et = e.get("element_type") if isinstance(e, dict) else getattr(e, "element_type", None)
            cr = e.get("cosh_ref") if isinstance(e, dict) else getattr(e, "cosh_ref", None)
            if et == "brand" and cr:
                return True
        return False

    relations_payload: list[dict] = []
    for rel_id, rel_items in by_relation.items():
        # Group by Part -> Option, capturing positions for ordering
        parts_data: dict[int, dict[int, list[tuple[int, OrderItem]]]] = {}
        for it in rel_items:
            try:
                c = decode_role(it.relation_role)
            except ValueError:
                continue
            parts_data.setdefault(c.part, {}).setdefault(c.option, []).append((c.position, it))

        parts_out: list[dict] = []
        for part_idx in sorted(parts_data.keys()):
            option_data: list[dict] = []
            for opt_idx in sorted(parts_data[part_idx].keys()):
                sorted_items = [it for (_, it) in sorted(parts_data[part_idx][opt_idx], key=lambda x: x[0])]
                has_locked = any(has_locked_brand_item(it) for it in sorted_items)
                statuses = [
                    (it.status.value if hasattr(it.status, "value") else it.status)
                    for it in sorted_items
                ]
                if all(s == OrderItemStatus.AVAILABLE.value for s in statuses):
                    option_status = "AVAILABLE"
                elif all(s == OrderItemStatus.NOT_AVAILABLE.value for s in statuses):
                    option_status = "NOT_AVAILABLE"
                else:
                    option_status = "NEW"
                option_data.append({
                    "option_index": opt_idx,
                    "items": sorted_items,
                    "has_locked_brand": has_locked,
                    "is_compound": len(sorted_items) > 1,
                    "option_status": option_status,
                })

            # Progressive reveal: hide Unlocked-brand Options while any Locked-brand
            # Option in the same Part is still open (NEW or AVAILABLE).
            any_locked = any(o["has_locked_brand"] for o in option_data)
            any_locked_still_open = any(
                o["has_locked_brand"] and o["option_status"] in ("NEW", "AVAILABLE")
                for o in option_data
            )
            for od in option_data:
                if any_locked and not od["has_locked_brand"]:
                    od["visible"] = not any_locked_still_open
                else:
                    od["visible"] = True

            options_out = [
                {
                    "option_index": od["option_index"],
                    "is_compound": od["is_compound"],
                    "has_locked_brand": od["has_locked_brand"],
                    "visible": od["visible"],
                    "option_status": od["option_status"],
                    "items": [item_brief(it) for it in od["items"]],
                }
                for od in option_data
            ]

            any_available = any(o["option_status"] == "AVAILABLE" for o in option_data)
            all_not_available = bool(option_data) and all(
                o["option_status"] == "NOT_AVAILABLE" for o in option_data
            )
            if any_available:
                part_status = "RESOLVED"
            elif all_not_available:
                part_status = "FAILED"
            else:
                part_status = "PENDING"

            parts_out.append({
                "part_index": part_idx,
                "options": options_out,
                "part_status": part_status,
            })

        relations_payload.append({
            "relation_id": rel_id,
            "relation_type": rel_items[0].relation_type if rel_items else None,
            "parts": parts_out,
        })

    return {
        "id": order.id, "status": order.status,
        "farmer_user_id": order.farmer_user_id, "client_id": order.client_id,
        "facilitator_user_id": order.facilitator_user_id,
        "date_from": order.date_from, "date_to": order.date_to,
        "created_at": order.created_at,
        # Flat list (unchanged shape for backward compat)
        "items": [item_brief(i) for i in items],
        # New: Part-aware relation structure
        "relations": relations_payload,
        "standalone_items": [item_brief(i) for i in standalone],
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
    """BL-07: Returns locked or unlocked brand options for a specific order item.

    Phase 3.3: when item.snapshot_id is set, brand-lock state is sourced from
    the frozen snapshot — SE edits to master practice elements after order
    placement do not change what the dealer sees for THIS order.
    """
    item = await _get_order_item(db, item_id, order_id)
    snapshot = None
    if item.snapshot_id:
        from app.modules.subscriptions.snapshot_models import LockedTimelineSnapshot
        snapshot = (await db.execute(
            select(LockedTimelineSnapshot).where(
                LockedTimelineSnapshot.id == item.snapshot_id
            )
        )).scalar_one_or_none()
    result = await get_brand_options(
        db, item.practice_id, current_user.id, snapshot=snapshot,
    )
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
    brand_unit: Optional[str] = None,    # caller override (dealer's brand pick)
    dosage_unit: Optional[str] = None,   # caller override
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-06: estimated volume for a practice item.

    Phase D.2: lookup is now keyed on all 5 spec fields —
    measure + l2_practice + application_method + brand_unit + dosage_unit.
    The previous l2-only filter could pick the wrong row when several
    formulas existed for the same L2 (different application methods,
    units, etc.) — fixed.

    `measure` comes from `crop_measures` for the package's crop.
    `application_method` comes from a Practice element of that name.
    `brand_unit`/`dosage_unit` are derived from the order item or practice
    elements; callers can override via query params (e.g. when the dealer
    is mid-pick and wants a preview).
    """
    from app.modules.advisory.models import Package
    from app.services.crop_measure import get_measure

    item = await _get_order_item(db, item_id, order_id)

    # ── Farm area ──────────────────────────────────────────────────
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

    # ── Practice + Timeline + Package + Measure ────────────────────
    practice = (await db.execute(
        select(Practice).where(Practice.id == item.practice_id)
    )).scalar_one_or_none()
    if not practice or not practice.l2_type:
        return {"estimated_volume": None, "volume_unit": None, "message": "Practice data not available"}

    timeline = (await db.execute(
        select(Timeline).where(Timeline.id == practice.timeline_id)
    )).scalar_one_or_none()
    if timeline is None:
        return {"estimated_volume": None, "volume_unit": None, "message": "Timeline not found for practice"}

    package = (await db.execute(
        select(Package).where(Package.id == timeline.package_id)
    )).scalar_one_or_none()
    if package is None or not package.crop_cosh_id:
        return {"estimated_volume": None, "volume_unit": None, "message": "Package or crop not found"}

    measure = await get_measure(db, package.crop_cosh_id)
    if not measure:
        return {
            "estimated_volume": None, "volume_unit": None,
            "message": (
                f"Crop measure not configured for crop {package.crop_cosh_id}. "
                "Ask SA to set Area-wise or Plant-wise via /admin/crop-measures."
            ),
            "error_code": "CROP_MEASURE_MISSING",
        }

    # ── Practice elements: dosage + application_method (+ derived units) ─
    elements_rows = (await db.execute(
        select(Element).where(Element.practice_id == item.practice_id)
    )).scalars().all()
    elements_by_type = {e.element_type: e for e in elements_rows}

    dosage_el = elements_by_type.get("dosage")
    dosage = float(dosage_el.value) if dosage_el and dosage_el.value else None

    method_el = elements_by_type.get("application_method")
    application_method = method_el.value if method_el and method_el.value else None
    if not application_method:
        return {
            "estimated_volume": None, "volume_unit": None,
            "message": "Application method not set on practice (DATA_CONFIG_ERROR).",
            "error_code": "APPLICATION_METHOD_MISSING",
        }

    # Phase D.3: Applications can now live as a Practice element. The SE
    # confirms the count at practice-creation time and the system stores
    # it as element_type='applications'. We prefer this over re-computing
    # at render time so frequency/timeline drift can't change the count.
    applications: Optional[int] = None
    apps_el = elements_by_type.get("applications")
    if apps_el and apps_el.value:
        try:
            n = int(apps_el.value)
            if n >= 1:
                applications = n
        except (TypeError, ValueError):
            pass  # element value malformed → fall through to legacy compute

    # Derive units. Callers can override; otherwise fall back to the
    # order item (set by the dealer at fulfillment) and finally to the
    # dosage element's unit_cosh_id for dosage_unit.
    if not brand_unit:
        brand_unit = item.volume_unit or None
    if not dosage_unit and dosage_el and dosage_el.unit_cosh_id:
        dosage_unit = dosage_el.unit_cosh_id

    if not brand_unit:
        return {
            "estimated_volume": None, "volume_unit": None,
            "message": "Brand unit not yet determined — pick a brand or pass ?brand_unit=…",
            "error_code": "BRAND_UNIT_MISSING",
        }
    if not dosage_unit:
        return {
            "estimated_volume": None, "volume_unit": None,
            "message": "Dosage unit not set on dosage element (DATA_CONFIG_ERROR).",
            "error_code": "DOSAGE_UNIT_MISSING",
        }

    # ── 5-key lookup ───────────────────────────────────────────────
    formulas = (await db.execute(
        select(VolumeFormula).where(
            VolumeFormula.measure == measure,
            VolumeFormula.l2_practice == practice.l2_type,
            VolumeFormula.application_method == application_method,
            VolumeFormula.brand_unit == brand_unit,
            VolumeFormula.dosage_unit == dosage_unit,
            VolumeFormula.status == "ACTIVE",
        )
    )).scalars().all()

    if not formulas:
        return {
            "estimated_volume": None, "volume_unit": None,
            "message": (
                f"No formula found for measure={measure}, l2={practice.l2_type}, "
                f"method={application_method}, brand_unit={brand_unit}, "
                f"dosage_unit={dosage_unit}. (DATA_CONFIG_ERROR)"
            ),
            "error_code": "FORMULA_NOT_FOUND",
        }
    if len(formulas) > 1:
        return {
            "estimated_volume": None, "volume_unit": None,
            "message": (
                f"{len(formulas)} matching formulas for the same key — "
                "data integrity error in volume_formulas. (DATA_CONFIG_ERROR)"
            ),
            "error_code": "FORMULA_DUPLICATE",
        }
    formula_row = formulas[0]

    # ── Timeline duration for legacy frequency-based fallback ──────
    # (Phase D.3 will switch this to read Applications from a Practice
    # element; until then, keep the existing compute path.)
    timeline_duration_days: Optional[int] = None
    if timeline.from_type.value == "DBS":
        timeline_duration_days = timeline.from_value - timeline.to_value + 1
    else:
        timeline_duration_days = timeline.to_value - timeline.from_value + 1

    result = calculate_volume(
        formula=formula_row.formula,
        brand_unit=formula_row.brand_unit,
        dosage=dosage,
        farm_area_acres=farm_area_acres,
        frequency_days=practice.frequency_days,
        timeline_duration_days=timeline_duration_days,
        applications=applications,
    )
    if result is None:
        return {"estimated_volume": None, "volume_unit": None, "message": "Could not calculate estimate"}
    volume, unit = result
    return {
        "estimated_volume": volume, "volume_unit": unit,
        "formula_used": formula_row.formula,
        "lookup_key": {
            "measure": measure,
            "l2_practice": practice.l2_type,
            "application_method": application_method,
            "brand_unit": brand_unit,
            "dosage_unit": dosage_unit,
        },
    }


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
