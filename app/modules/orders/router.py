from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.orders.models import (
    Order, OrderItem, SeedOrder, PackingList, MissingBrandReport,
    DealerProfile, DealerRelationship, DealerManufacturerCatalog,
    OrderItemEvent,
    OrderStatus, OrderItemStatus,
)
from app.services.order_events import record_event as _record_event
from app.modules.subscriptions.models import Subscription
from app.modules.sync.models import VolumeFormula
from app.modules.advisory.models import Package, Practice, Element, Timeline
from app.services.bl06_volume_calc import calculate_volume
from math import radians, cos, sin, asin, sqrt
from app.services.bl07_brand_options import get_brand_options
from app.services.bl10_order_state import (
    DEALER, FARMER,
    is_item_abortable, is_order_abortable,
    validate_item_transition, validate_order_transition,
)
from app.services.bl14_approval import is_brand_visible_to_farmer
from app.services.fcm_service import send_fcm
import logging
from app.modules.advisory.models import RelationType
from app.modules.subscriptions.models import PromoterAssignment, SubscriptionPaymentRequest, AssignmentStatus

_orders_logger = logging.getLogger(__name__)

# BL-14 spec: facilitator gets the FCM "your farmer needs to approve"
# alert when the dealer submits volumes/prices for farmer approval.
# The farmer drives the actual approve/reject decision; the
# facilitator's role is to nudge the farmer if they delay.
SUBMIT_FOR_APPROVAL_FCM_TITLE = "Your farmer needs to approve"
SUBMIT_FOR_APPROVAL_FCM_BODY = (
    "The dealer has sent volume and pricing for an order. Open RootsTalk "
    "to nudge the farmer if needed."
)

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
    # Orders V2 (2026-05-31). PESTICIDE / FERTILIZER. Optional only
    # for backward compat with legacy callers; new PWA flows always
    # pass it. When absent we derive from the first practice's
    # l1_type so the locked-brand gate has something to bite on.
    category: Optional[str] = None


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

    # ── One-practice-per-order rule (2026-05-21) ──────────────────────────
    # Date-range bundling on the PWA can race with a concurrent
    # order. Even without the race, an old caller might post the same
    # practice twice. Refuse here so we never write a duplicate
    # OrderItem; the PWA's preview is supposed to have filtered these
    # out already.
    if request.practice_ids:
        from app.services.order_bundle import (
            already_ordered_practice_ids, conflicts_with_existing_orders,
        )
        already = await already_ordered_practice_ids(db, request.subscription_id)
        conflicts = conflicts_with_existing_orders(request.practice_ids, already)
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "practices_already_ordered",
                    "message": (
                        "Some of these practices are already in an active "
                        "order. Open the existing order or cancel it first."
                    ),
                    "practice_ids": conflicts,
                },
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

    # ── Locked-brand gate (Orders V2 Batch 9, mirrors Batch 5 send) ──
    # The cancel→re-send path runs this same check; doing it here too
    # closes the consistency gap where a stale /order/new picker
    # could put a brand-locked order in front of a non-onboarded
    # dealer or a facilitator.
    if request.practice_ids:
        has_locked = await _practice_ids_have_locked_brand(db, request.practice_ids)
        if has_locked:
            if request.facilitator_user_id:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "locked_brand_requires_onboarded_dealer",
                        "message": "This order has a brand-locked item. It can only be sent to a dealer onboarded by the company.",
                    },
                )
            if request.dealer_user_id and not await _is_dealer_onboarded_by_client(
                db, request.dealer_user_id, request.client_id,
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "locked_brand_requires_onboarded_dealer",
                        "message": "This order has a brand-locked item. It can only be sent to a dealer onboarded by the company.",
                    },
                )

    # ── Derive Order.category if the caller didn't pass it ───────────
    # New PWA flows always pass it; legacy / tests can omit.
    resolved_category = (request.category or "").upper() or None
    if not resolved_category and request.practice_ids:
        first_l1 = (await db.execute(
            select(Practice.l1_type).where(Practice.id.in_(request.practice_ids)).limit(1)
        )).scalar_one_or_none()
        if first_l1 in ("PESTICIDE", "SPECIAL_INPUT"):
            resolved_category = "PESTICIDE"
        elif first_l1 == "FERTILIZER":
            resolved_category = "FERTILIZER"

    order = Order(
        subscription_id=request.subscription_id,
        farmer_user_id=current_user.id,
        client_id=request.client_id,
        category=resolved_category,
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
    # Batch 15 — also emit a CREATED event per item so a brand-new
    # order shows up in the lineage report immediately. Without this
    # the journey only becomes visible to reports once the dealer
    # acts on it, which is too late for "what was just placed today?"
    # dashboards.
    new_items: list[OrderItem] = []
    for spec in item_specs:
        new_item = OrderItem(
            order_id=order.id,
            practice_id=spec["practice_id"],
            timeline_id=spec["timeline_id"],
            relation_id=spec["relation_id"],
            relation_type=spec["relation_type"],
            relation_role=spec["relation_role"],
            snapshot_id=snap_id_by_tl.get(spec["timeline_id"]),
            status=OrderItemStatus.PENDING,
        )
        db.add(new_item)
        new_items.append(new_item)
    await db.flush()
    for it in new_items:
        await _record_event(
            db,
            lineage_id=it.lineage_id,
            event_type="CREATED",
            actor_user_id=current_user.id, actor_role="FARMER",
            order_id=order.id,
            order_item_id=it.id,
            prev_status=None,
            new_status=OrderItemStatus.PENDING.value,
            metadata={
                "practice_id": it.practice_id,
                "category": order.category,
            },
        )

    await db.commit()
    await db.refresh(order)

    # Clear any lingering SENT INPUT alerts for this sub if the new
    # order covers the last outstanding due-today practice. Matches
    # the rule "alert disappears once the order has been placed"
    # (2026-05-31). Best-effort — if it errors, the daily-alerts
    # sweep will catch up on the next run.
    try:
        from app.tasks.alerts import clear_input_alerts_if_no_due_remaining
        await clear_input_alerts_if_no_due_remaining(db, order.subscription_id)
        await db.commit()
    except Exception:
        pass

    return {"id": order.id, "status": order.status}


@router.get("/farmer/subscriptions/{subscription_id}/order-preview")
async def order_preview(
    subscription_id: str,
    category: str,
    to_date: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Preview the bundle for a date-range order before it's
    committed. Drives the PWA's "Choose date range" sheet — the
    farmer adjusts the TO date, this endpoint returns the count
    and list of practices that would go into the order.

    Bundling rule (locked 2026-05-21):
      - category PESTICIDE → L1 in {PESTICIDE, SPECIAL_INPUT}
      - category FERTILIZER → L1 in {FERTILIZER}, minus NPK-dosage L2s
      - practice's timeline window must overlap [today, to_date]
        by at least one day (inclusive)
      - practice must NOT already be in any non-CANCELLED order
        for this subscription (one-practice-per-order rule)

    Response also returns `package_end_date` so the PWA can clamp
    the date picker.
    """
    from datetime import date as _date
    from app.services.order_bundle import (
        compute_bundle, ALL_CATEGORIES, package_end_date,
    )

    if category.upper() not in ALL_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown category {category!r}. Use PESTICIDE or FERTILIZER.",
        )

    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    try:
        to_d = _date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"to_date must be ISO YYYY-MM-DD, got {to_date!r}",
        )

    today = _date.today()
    if to_d < today:
        raise HTTPException(
            status_code=422,
            detail="to_date cannot be before today.",
        )

    # Cap to_date at package_end so a stale PWA picker can't try
    # to extend beyond the subscription's life.
    pkg = (await db.execute(
        select(Package).where(Package.id == sub.package_id)
    )).scalar_one_or_none()
    pkg_end = package_end_date(sub, pkg.duration_days) if pkg else None
    if pkg_end and to_d > pkg_end:
        to_d = pkg_end

    bundle = await compute_bundle(
        db, subscription=sub, category=category, to_date=to_d, today=today,
    )
    return {
        "subscription_id": subscription_id,
        "category": category.upper(),
        "to_date": to_d.isoformat(),
        "package_end_date": pkg_end.isoformat() if pkg_end else None,
        "today": today.isoformat(),
        "count": len(bundle["practices"]),
        "practices": bundle["practices"],
        "excluded_already_ordered": bundle["excluded_already_ordered"],
    }


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
        # Active items only — archived (timeline-expired) rows are
        # off the live order surface; they live in History.
        items_result = await db.execute(
            select(OrderItem).where(
                OrderItem.order_id == o.id,
                OrderItem.archived_at.is_(None),
            )
        )
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
    # Batch 8: only the active (non-timeline-archived) items belong
    # on the farmer's live order detail. History lives elsewhere.
    items_result = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.archived_at.is_(None),
        )
    )
    items = items_result.scalars().all()
    return {
        "id": order.id, "status": order.status,
        "date_from": order.date_from, "date_to": order.date_to,
        "created_at": order.created_at,
        "dealer_user_id": order.dealer_user_id,
        "facilitator_user_id": order.facilitator_user_id,
        # Surface subscription_id + category so the DRAFT picker on
        # /orders/[orderId] can fetch eligible recipients without an
        # extra round-trip.
        "subscription_id": order.subscription_id,
        "category": order.category,
        "items": [
            {
                "id": i.id, "practice_id": i.practice_id, "status": i.status,
                "relation_id": i.relation_id, "relation_type": i.relation_type,
                "brand_name": i.brand_name if is_brand_visible_to_farmer(i.status) else None,
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
    """Farmer cancels an order.

    Allowed at any non-terminal status (DRAFT, SENT, ACCEPTED,
    PROCESSING, SENT_FOR_APPROVAL, PARTIALLY_APPROVED). The narrative
    is explicit (2026-05-31): "It can be cancelled even if the dealer
    has accepted it. The farmer cannot be left at the dealer's mercy."

    The only block is a *live* presence lease: if the dealer's app is
    heartbeating against this order right now (dealer_viewing_until in
    the future), we 409. The dealer's lease extends ~30 s past their
    last heartbeat, so a closed screen frees the lock quickly.

    Cancel-and-migrate semantics (Orders V2 Batch 3):
    On a successful cancel, every active OrderItem on this order is
    *migrated* to a fresh DRAFT order — new OrderItem rows, same
    `lineage_id`, dealer/facilitator cleared so the farmer picks a new
    recipient before re-sending. The original rows stay in place with
    status REROUTED so the husk still tells the historical story.
    The cancelled order is returned alongside the new draft's id;
    the PWA navigates the farmer to the draft to pick a new dealer.
    """
    order = await _get_farmer_order(db, order_id, current_user.id)

    terminal = {OrderStatus.COMPLETED, OrderStatus.CANCELLED, OrderStatus.EXPIRED}
    if order.status in terminal:
        raise HTTPException(
            status_code=400,
            detail=f"Order is already {order.status.value if hasattr(order.status, 'value') else order.status}; nothing to cancel.",
        )

    now = datetime.now(timezone.utc)
    if order.dealer_viewing_until and order.dealer_viewing_until > now:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "dealer_currently_viewing",
                "message": "The dealer is reviewing this order right now. Try again in a minute.",
            },
        )

    prev_order_status = order.status.value if hasattr(order.status, "value") else order.status

    # Snapshot the live items (i.e. anything that isn't already
    # archived from a prior partial flow) and migrate them onto a
    # fresh DRAFT order. REROUTED rows stay behind as historical
    # pointers on the husk.
    skip_statuses = {OrderItemStatus.REROUTED, OrderItemStatus.REMOVED}
    # Don't migrate timeline-archived items either — their advisory
    # context is gone, re-routing them would just leak ghost rows.
    items_q = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.archived_at.is_(None),
        )
    )
    items_to_migrate = [it for it in items_q.scalars().all() if it.status not in skip_statuses]

    new_draft = Order(
        subscription_id=order.subscription_id,
        farmer_user_id=order.farmer_user_id,
        client_id=order.client_id,
        category=order.category,
        date_from=order.date_from,
        date_to=order.date_to,
        status=OrderStatus.DRAFT,
        dealer_user_id=None,
        facilitator_user_id=None,
        locked_timelines=order.locked_timelines,
        # Inherit the original 14-day expiry; the farmer needs to
        # actually send this draft before then.
        expires_at=order.expires_at,
    )
    db.add(new_draft)
    await db.flush()

    for it in items_to_migrate:
        prev_item_status = it.status.value if hasattr(it.status, "value") else it.status

        new_item = OrderItem(
            order_id=new_draft.id,
            practice_id=it.practice_id,
            timeline_id=it.timeline_id,
            brand_cosh_id=it.brand_cosh_id,
            brand_name=it.brand_name,
            given_volume=it.given_volume,
            volume_unit=it.volume_unit,
            price=it.price,
            estimated_volume=it.estimated_volume,
            relation_id=it.relation_id,
            relation_type=it.relation_type,
            relation_role=it.relation_role,
            scan_verified=False,  # reset for the new leg
            status=OrderItemStatus.PENDING,
            snapshot_id=it.snapshot_id,
            lineage_id=it.lineage_id,  # ← key: preserves the journey
        )
        db.add(new_item)
        await db.flush()

        # Audit pair: leg-out on the husk, leg-in on the draft.
        # Recorded before we flip the old row so its FK is still valid.
        await _record_event(
            db,
            lineage_id=it.lineage_id,
            event_type="REROUTED_FROM",
            actor_user_id=current_user.id,
            actor_role="FARMER",
            order_id=order.id,
            order_item_id=it.id,
            prev_status=prev_item_status,
            new_status=OrderItemStatus.REROUTED.value,
            metadata={"to_order_id": new_draft.id, "to_order_item_id": new_item.id},
        )
        await _record_event(
            db,
            lineage_id=it.lineage_id,
            event_type="REROUTED_TO",
            actor_user_id=current_user.id,
            actor_role="FARMER",
            order_id=new_draft.id,
            order_item_id=new_item.id,
            prev_status=OrderItemStatus.REROUTED.value,
            new_status=OrderItemStatus.PENDING.value,
            metadata={"from_order_id": order.id, "from_order_item_id": it.id},
        )

        it.status = OrderItemStatus.REROUTED

    # Husk goes terminal.
    order.status = OrderStatus.CANCELLED

    # Order-level audit. `lineage_id = order.id` keeps the schema
    # happy; order ids and item lineage ids share the UUID namespace
    # so this can't collide with an item lineage in practice.
    await _record_event(
        db,
        lineage_id=order.id,
        event_type="CANCELLED_BY_FARMER",
        actor_user_id=current_user.id,
        actor_role="FARMER",
        order_id=order.id,
        prev_status=prev_order_status,
        new_status=OrderStatus.CANCELLED.value,
        metadata={
            "new_draft_order_id": new_draft.id,
            "migrated_item_count": len(items_to_migrate),
        },
    )

    await db.commit()
    return {
        "status": order.status,
        "new_draft_order_id": new_draft.id,
        "migrated_item_count": len(items_to_migrate),
    }


@router.delete("/farmer/orders/{order_id}")
async def delete_cancelled_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer deletes a CANCELLED order husk.

    Only allowed once the order is in CANCELLED status — i.e. the
    server has acknowledged the cancel and the farmer has chosen to
    discard the husk instead of (or in addition to) re-sending the
    migrated draft.

    Items already migrated under their REROUTED status; they sit on
    this order as historical pointers and get removed here. The
    `order_item_events` rows that referenced them keep their
    `lineage_id` populated; their `order_id` / `order_item_id` FKs
    SET NULL via the migration so the trail survives row deletion.

    Deleting a DRAFT or any non-terminal status is refused — the
    farmer must cancel first.
    """
    order = await _get_farmer_order(db, order_id, current_user.id)
    if order.status != OrderStatus.CANCELLED:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "must_cancel_first",
                "message": "Only cancelled orders can be deleted. Cancel this order first.",
            },
        )

    # Wipe the REROUTED husk-items so the Order row can drop without
    # tripping the order_items.order_id FK. The events table keeps
    # the lineage trail via SET NULL.
    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(OrderItem).where(OrderItem.order_id == order.id))

    await _record_event(
        db,
        lineage_id=order.id,
        event_type="HUSK_DELETED_BY_FARMER",
        actor_user_id=current_user.id,
        actor_role="FARMER",
        order_id=None,  # set NULL since we're about to delete the order row
        prev_status=OrderStatus.CANCELLED.value,
        new_status=None,
        metadata={"deleted_order_id": order.id},
    )

    await db.delete(order)
    await db.commit()
    return {"deleted": True}


class OrderSend(BaseModel):
    dealer_user_id: Optional[str] = None
    facilitator_user_id: Optional[str] = None


class DBSBulkCreate(BaseModel):
    """DBS bulk order — the farmer doesn't pick items or a date
    range. Server resolves every DBS practice of the chosen category
    in the package, filters out already-ordered ones, and creates
    the order under a synthesised date window the farmer never sees."""
    subscription_id: str
    client_id: str
    category: str  # PESTICIDE | FERTILIZER
    dealer_user_id: Optional[str] = None
    facilitator_user_id: Optional[str] = None
    farm_area_acres: Optional[float] = None
    area_unit: Optional[str] = None


@router.get("/farmer/subscriptions/{subscription_id}/dbs-bulk-preview")
async def dbs_bulk_preview(
    subscription_id: str,
    category: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Tells the PWA whether a DBS bulk order for this category is
    available right now + how many items it would contain. Drives
    the "Pre-sowing pesticides / fertilizers" buttons on the
    advisory strip — disable when count is 0; show locked-brand
    explainer when applicable.

    Mirrors the validation in create_dbs_bulk_order so the PWA
    doesn't surface a button the server would 400 on.
    """
    from datetime import date as _date
    from app.services.order_bundle import (
        resolve_dbs_practices_for_category, already_ordered_practice_ids,
    )

    if category.upper() not in ("PESTICIDE", "FERTILIZER"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown category {category!r}. Use PESTICIDE or FERTILIZER.",
        )
    category = category.upper()

    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    package = (await db.execute(
        select(Package).where(Package.id == sub.package_id)
    )).scalar_one_or_none()
    pkg_type = (package.package_type.value if (package and hasattr(package.package_type, "value"))
                else str(package.package_type) if package else None)

    today = _date.today()
    window_open = sub.crop_start_date is None or (
        (sub.crop_start_date.date() if hasattr(sub.crop_start_date, "date") else sub.crop_start_date) > today
    )

    if pkg_type != "ANNUAL" or not window_open:
        return {
            "category": category,
            "count": 0,
            "available": False,
            "reason": "not_annual" if pkg_type != "ANNUAL" else "window_closed",
            "has_locked_brand": False,
        }

    all_dbs = await resolve_dbs_practices_for_category(
        db, subscription=sub, category=category,
    )
    already = await already_ordered_practice_ids(db, sub.id)
    remaining = [pid for pid in all_dbs if pid not in already]
    has_locked = await _practice_ids_have_locked_brand(db, remaining) if remaining else False
    return {
        "category": category,
        "count": len(remaining),
        "available": bool(remaining),
        "reason": "nothing_to_order" if not remaining else None,
        "has_locked_brand": has_locked,
        # Practice IDs are returned so the PWA can pipe them straight
        # into the existing /eligible-recipients-for-new-order endpoint
        # without us building a parallel DBS picker. The farmer never
        # sees these — they're opaque identifiers used only in URL
        # params on the next request.
        "practice_ids": remaining,
        "client_id": sub.client_id,
    }


@router.post("/farmer/orders/dbs-bulk", status_code=201)
async def create_dbs_bulk_order(
    request: DBSBulkCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer places a DBS bulk order — pre-sowing pesticides or
    fertilizers, no date range, no item selection.

    Per the DBS V1 carve-out (2026-05-31):
      - Annual packages only (Perennial has no DBS units).
      - `crop_start_date IS NULL OR crop_start_date > today`
        (today/past closes DBS — BL-04a step 5).
      - One bulk order per category unless the package has DBS
        practices not yet covered by an existing non-terminal
        order on this subscription.
      - Synthesised date range: today → (crop_start − 1) if start
        is set, else today + 365 days. Not surfaced anywhere.
      - Reuses every other piece — locked-brand gate, CREATED
        events, snapshots, lock cascades — same as
        `create_order`.
    """
    from datetime import date as _date
    from app.services.order_bundle import (
        resolve_dbs_practices_for_category, already_ordered_practice_ids,
    )

    if request.category.upper() not in ("PESTICIDE", "FERTILIZER"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown category {request.category!r}. Use PESTICIDE or FERTILIZER.",
        )
    category = request.category.upper()

    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == request.subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # Annual-only gate.
    package = (await db.execute(
        select(Package).where(Package.id == sub.package_id)
    )).scalar_one_or_none()
    if not package:
        raise HTTPException(status_code=404, detail="Package not found")
    pkg_type = package.package_type.value if hasattr(package.package_type, "value") else str(package.package_type)
    if pkg_type != "ANNUAL":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "dbs_not_supported_for_perennial",
                "message": "Pre-sowing inputs are only available for Annual packages.",
            },
        )

    # DBS window check — BL-04a step 5.
    today = _date.today()
    if sub.crop_start_date is not None:
        crop_start_d = sub.crop_start_date.date() if hasattr(sub.crop_start_date, "date") else sub.crop_start_date
        if crop_start_d <= today:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "dbs_window_closed",
                    "message": "Pre-sowing input ordering closes on the crop start date.",
                },
            )

    # Resolve practices. Drop the ones already in non-terminal orders.
    all_dbs = await resolve_dbs_practices_for_category(
        db, subscription=sub, category=category,
    )
    if not all_dbs:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "no_dbs_practices_in_package",
                "message": f"This package has no pre-sowing {category.lower()} practices.",
            },
        )
    already = await already_ordered_practice_ids(db, sub.id)
    practice_ids = [pid for pid in all_dbs if pid not in already]
    if not practice_ids:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "nothing_to_order",
                "message": "All pre-sowing inputs in this category are already in another order.",
            },
        )

    # Locked-brand gate.
    has_locked = await _practice_ids_have_locked_brand(db, practice_ids)
    if has_locked:
        if request.facilitator_user_id:
            # User's 2026-05-31 correction: farmer CAN send locked-brand
            # to a facilitator (farmer can't see what's locked anyway).
            # The facilitator's onward picker enforces the onboarded-
            # dealer rule. So this branch stays permissive.
            pass
        elif request.dealer_user_id and not await _is_dealer_onboarded_by_client(
            db, request.dealer_user_id, request.client_id,
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "locked_brand_requires_onboarded_dealer",
                    "message": "This order has a brand-locked item. It can only be sent to a dealer onboarded by the company.",
                },
            )

    # Acreage hard-lock on first DAS order doesn't apply here —
    # DBS doesn't depend on area for volume calc. But we still
    # honour an incoming acreage value when supplied.
    if request.farm_area_acres and not sub.farm_area_acres:
        sub.farm_area_acres = request.farm_area_acres
        sub.area_unit = request.area_unit or sub.area_unit or "acres"

    # Synthesised date range. Hidden from the farmer + facilitator;
    # the dealer eventually sees it as a fallback window. See memory
    # `project_rootstalk_dbs_v1.md` for the carve-out rationale.
    synth_from = datetime.now(timezone.utc)
    if sub.crop_start_date is not None:
        crop_start_d = sub.crop_start_date.date() if hasattr(sub.crop_start_date, "date") else sub.crop_start_date
        synth_to = datetime.combine(
            crop_start_d - timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
    else:
        synth_to = synth_from + timedelta(days=365)

    order = Order(
        subscription_id=request.subscription_id,
        farmer_user_id=current_user.id,
        client_id=request.client_id,
        category=category,
        dealer_user_id=request.dealer_user_id,
        facilitator_user_id=request.facilitator_user_id,
        date_from=synth_from,
        date_to=synth_to,
        status=OrderStatus.SENT,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    db.add(order)
    await db.flush()

    # Snapshot the DBS practices' timelines so SE edits after order
    # placement don't leak into the dealer's view (same Phase 3.2
    # contract as create_order).
    practice_rows = (await db.execute(
        select(Practice).where(Practice.id.in_(practice_ids))
    )).scalars().all()
    tl_ids = {p.timeline_id for p in practice_rows if p.timeline_id}

    from app.services.snapshot import take_snapshot
    snap_id_by_tl: dict[str, Optional[str]] = {}
    for tl_id in tl_ids:
        try:
            snap = await take_snapshot(
                db, request.subscription_id, tl_id, "PURCHASE_ORDER", source="CCA",
            )
            snap_id_by_tl[tl_id] = snap.id
        except Exception:
            snap_id_by_tl[tl_id] = None

    practice_by_id = {p.id: p for p in practice_rows}
    new_items: list[OrderItem] = []
    for pid in practice_ids:
        p = practice_by_id.get(pid)
        if not p:
            continue
        relation_type = None
        if p.relation_id:
            from app.modules.advisory.models import Relation
            rel = (await db.execute(
                select(Relation).where(Relation.id == p.relation_id)
            )).scalar_one_or_none()
            if rel:
                relation_type = rel.relation_type.value if hasattr(rel.relation_type, "value") else str(rel.relation_type)
        new_item = OrderItem(
            order_id=order.id,
            practice_id=pid,
            timeline_id=p.timeline_id,
            relation_id=p.relation_id,
            relation_type=relation_type,
            relation_role=p.relation_role,
            snapshot_id=snap_id_by_tl.get(p.timeline_id),
            status=OrderItemStatus.PENDING,
        )
        db.add(new_item)
        new_items.append(new_item)
    await db.flush()

    for it in new_items:
        await _record_event(
            db,
            lineage_id=it.lineage_id,
            event_type="CREATED",
            actor_user_id=current_user.id, actor_role="FARMER",
            order_id=order.id, order_item_id=it.id,
            prev_status=None, new_status=OrderItemStatus.PENDING.value,
            metadata={
                "practice_id": it.practice_id,
                "category": category,
                "dbs_bulk": True,
            },
        )

    await db.commit()
    await db.refresh(order)
    return {
        "id": order.id,
        "status": order.status,
        "item_count": len(new_items),
        "category": category,
        "is_dbs_bulk": True,
    }


@router.put("/farmer/orders/{order_id}/send")
async def send_draft_order(
    order_id: str,
    body: OrderSend,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer assigns a recipient to a DRAFT order and sends it.

    The Orders V2 (2026-05-31) cancel flow leaves the farmer holding
    a DRAFT with no recipient — the migrated items keep their
    `lineage_id`, the farmer picks a new dealer or facilitator here,
    and the order flips DRAFT → SENT.

    Validation (Batch 4 — locked-brand gate lands in Batch 5):
      - Order must be DRAFT.
      - Exactly one of `dealer_user_id` / `facilitator_user_id`.
      - Dealer: ACTIVE + sell_categories includes order.category.
      - Facilitator: ACTIVE. No category check (they only route).

    Emits a SENT event per item plus an order-level SENT event so
    reports can date the leg without joining item events.
    """
    order = await _get_farmer_order(db, order_id, current_user.id)
    if order.status != OrderStatus.DRAFT:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "not_a_draft",
                "message": "Only DRAFT orders can be sent. Cancel the order first if you want to re-route it.",
            },
        )

    if bool(body.dealer_user_id) == bool(body.facilitator_user_id):
        raise HTTPException(
            status_code=422,
            detail="Pick exactly one — dealer or facilitator.",
        )

    # ── Locked-brand gate (Orders V2 Batch 5) ────────────────────
    # If even one item is brand-locked, the order must go to a
    # dealer onboarded by this client — no facilitators, no
    # non-onboarded dealers. Phrased as one rule (rather than two
    # separate gates) so the picker can also refuse cleanly with
    # one error code.
    has_locked = await _order_has_locked_brand_items(db, order.id)
    if has_locked:
        if body.facilitator_user_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "locked_brand_requires_onboarded_dealer",
                    "message": "This order has a brand-locked item. It can only be sent to a dealer onboarded by the company.",
                },
            )
        if not await _is_dealer_onboarded_by_client(
            db, body.dealer_user_id, order.client_id,
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "locked_brand_requires_onboarded_dealer",
                    "message": "This order has a brand-locked item. It can only be sent to a dealer onboarded by the company.",
                },
            )

    # ── Recipient gates ──────────────────────────────────────────
    if body.dealer_user_id:
        await _assert_active_dealer(db, body.dealer_user_id)
        profile = (await db.execute(
            select(DealerProfile).where(DealerProfile.user_id == body.dealer_user_id)
        )).scalar_one_or_none()
        if not profile:
            raise HTTPException(status_code=404, detail="Dealer profile not found")
        # Category match. sell_categories stores PLURALS
        # (PESTICIDES / FERTILISERS / SEEDS) per legacy convention.
        cat_to_plural = {"PESTICIDE": "PESTICIDES", "FERTILIZER": "FERTILISERS"}
        required = cat_to_plural.get(order.category or "")
        if required and required not in (profile.sell_categories or []):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "dealer_licence_mismatch",
                    "message": f"This dealer doesn't hold a {order.category} licence.",
                },
            )
        order.dealer_user_id = body.dealer_user_id
        order.facilitator_user_id = None
    else:
        await _assert_active_facilitator(db, body.facilitator_user_id)
        order.facilitator_user_id = body.facilitator_user_id
        order.dealer_user_id = None

    # ── Flip + audit ─────────────────────────────────────────────
    order.status = OrderStatus.SENT
    # Refresh the 14-day expiry from the moment of send — the
    # original draft's clock isn't fair to a recipient who only
    # just got the order.
    order.expires_at = datetime.now(timezone.utc) + timedelta(days=14)

    items_q = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.status == OrderItemStatus.PENDING,
        )
    )
    items = items_q.scalars().all()
    for it in items:
        await _record_event(
            db,
            lineage_id=it.lineage_id,
            event_type="SENT",
            actor_user_id=current_user.id,
            actor_role="FARMER",
            order_id=order.id,
            order_item_id=it.id,
            prev_status=OrderItemStatus.PENDING.value,
            new_status=OrderItemStatus.PENDING.value,
            metadata={
                "dealer_user_id": order.dealer_user_id,
                "facilitator_user_id": order.facilitator_user_id,
            },
        )

    await _record_event(
        db,
        lineage_id=order.id,
        event_type="SENT",
        actor_user_id=current_user.id,
        actor_role="FARMER",
        order_id=order.id,
        prev_status=OrderStatus.DRAFT.value,
        new_status=OrderStatus.SENT.value,
        metadata={
            "dealer_user_id": order.dealer_user_id,
            "facilitator_user_id": order.facilitator_user_id,
            "item_count": len(items),
        },
    )

    await db.commit()
    return {
        "status": order.status,
        "dealer_user_id": order.dealer_user_id,
        "facilitator_user_id": order.facilitator_user_id,
    }


@router.get("/farmer/orders/{order_id}/eligible-recipients")
async def list_eligible_recipients(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pre-filtered picker payload for the farmer's DRAFT page.

    Server-side filtering enforces:
      - Dealer licence-category match (`sell_categories` includes
        the order's category, mapped to its plural form).
      - Locked-brand: if any item is brand-locked, only dealers
        onboarded by the order's client appear and the facilitators
        list is empty.
      - Active status for both roles.

    Ranked by haversine distance from the farmer's saved GPS, cap
    at 10 each. Promoter pinning is left to the existing
    `nearby-*` endpoints; this surface is about *eligibility*,
    not ranking.
    """
    order = await _get_farmer_order(db, order_id, current_user.id)
    has_locked = await _order_has_locked_brand_items(db, order.id)
    return await _build_eligible_recipients_payload(
        db,
        current_user=current_user,
        client_id=order.client_id,
        category=order.category,
        has_locked=has_locked,
    )


@router.get("/farmer/subscriptions/{subscription_id}/eligible-recipients-for-new-order")
async def list_eligible_recipients_for_new_order(
    subscription_id: str,
    category: str,
    practice_ids: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pre-creation variant of `list_eligible_recipients`. The
    /order/new PWA page calls this BEFORE the order is created so
    the picker is filtered identically to the cancel→re-send picker:
    licence-category + locked-brand awareness all server-side.

    `practice_ids` is a comma-separated string (matches the existing
    URL convention from the advisory's BundleOrderSheet handoff).
    """
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    pids = [p for p in (practice_ids or "").split(",") if p]
    has_locked = await _practice_ids_have_locked_brand(db, pids)
    return await _build_eligible_recipients_payload(
        db,
        current_user=current_user,
        client_id=sub.client_id,
        category=category,
        has_locked=has_locked,
    )


@router.put("/farmer/orders/{order_id}/items/{item_id}/approve")
async def approve_order_item(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-14: Farmer approves dealer's volume and price."""
    await _get_farmer_order(db, order_id, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    res = validate_item_transition(item.status, OrderItemStatus.APPROVED.value, FARMER)
    if not res.allowed:
        _raise_transition(res)
    prev = item.status.value if hasattr(item.status, "value") else item.status
    item.status = OrderItemStatus.APPROVED
    await _record_event(
        db, lineage_id=item.lineage_id,
        event_type="PURCHASE_RECORDED",
        actor_user_id=current_user.id, actor_role="FARMER",
        order_id=order_id, order_item_id=item.id,
        prev_status=prev, new_status=OrderItemStatus.APPROVED.value,
        metadata={
            "brand_name": item.brand_name,
            "price": float(item.price) if item.price else None,
            "given_volume": float(item.given_volume) if item.given_volume else None,
            "volume_unit": item.volume_unit,
        },
    )
    await _update_order_status(db, order_id)
    await db.commit()
    return {"item_id": item_id, "status": item.status}


@router.put("/farmer/orders/{order_id}/items/{item_id}/reject")
async def reject_order_item(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_farmer_order(db, order_id, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    res = validate_item_transition(item.status, OrderItemStatus.REJECTED.value, FARMER)
    if not res.allowed:
        _raise_transition(res)
    prev = item.status.value if hasattr(item.status, "value") else item.status
    item.status = OrderItemStatus.REJECTED
    await _record_event(
        db, lineage_id=item.lineage_id,
        event_type="REJECTED",
        actor_user_id=current_user.id, actor_role="FARMER",
        order_id=order_id, order_item_id=item.id,
        prev_status=prev, new_status=OrderItemStatus.REJECTED.value,
    )
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
    from app.modules.advisory.models import Timeline, Practice as AdvPractice
    from app.services.snapshot_render import (
        TimelineMetadata, cca_calendar_dates,
    )

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
        # BL-17 audit (2026-05-06): replaced inline DAS/DBS date
        # arithmetic with the canonical helper. Pre-audit this branch
        # duplicated cca_calendar_dates' logic — drift risk if BL-17
        # boundary semantics change.
        date_from_iso = None
        date_to_iso = None
        crop_start = sub.crop_start_date
        if crop_start is not None:
            from_type_value = tl.from_type.value if hasattr(tl.from_type, 'value') else str(tl.from_type)
            crop_date = crop_start.date() if hasattr(crop_start, 'date') else crop_start
            if from_type_value in ("DAS", "DBS"):
                meta = TimelineMetadata(
                    from_type=from_type_value,
                    from_value=int(tl.from_value),
                    to_value=int(tl.to_value),
                )
                df, dt_ = cca_calendar_dates(meta, crop_date)
                # DBS production convention is from > to, so cca_calendar_dates
                # already returns (earlier, later). DAS is naturally ordered.
                date_from_iso = df.isoformat()
                date_to_iso = dt_.isoformat()
            # CALENDAR: leave null for now (no absolute reference dates).

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
    await _assert_active_dealer(db, current_user.id)
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
      `cosh_core_items` with core_type='brand'. Free-text or unknown
      identifiers are rejected with stable error codes
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
    from app.modules.sync.models import CoshCoreItem

    await _assert_active_dealer(db, current_user.id)
    await _get_dealer_order(db, order_id, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    res = validate_item_transition(item.status, OrderItemStatus.AVAILABLE.value, DEALER)
    if not res.allowed:
        _raise_transition(res)

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
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id == brand_cosh_id,
            CoshCoreItem.core_type == "brand",
            CoshCoreItem.status == "active",
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
    prev_status = item.status.value if hasattr(item.status, "value") else item.status
    item.status = OrderItemStatus.AVAILABLE
    await _record_event(
        db, lineage_id=item.lineage_id,
        event_type="MARKED_AVAILABLE",
        actor_user_id=current_user.id, actor_role="DEALER",
        order_id=order_id, order_item_id=item.id,
        prev_status=prev_status, new_status=OrderItemStatus.AVAILABLE.value,
        metadata={
            "brand_cosh_id": brand_cosh_id,
            "brand_name": canonical_name,
            "given_volume": float(item.given_volume) if item.given_volume else None,
            "volume_unit": item.volume_unit,
            "price": float(item.price) if item.price else None,
        },
    )

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
    await _assert_active_dealer(db, current_user.id)
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
    await _assert_active_dealer(db, current_user.id)
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
    await _assert_active_dealer(db, current_user.id)
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


def _timeline_end_date(timeline, sub_crop_start):
    """Date the timeline's window closes.

    DAS: crop_start_date + to_value
    DBS: crop_start_date - to_value (pre-sowing window — end is the
         smaller-magnitude offset, which mathematically is `start - to`)
    CALENDAR: not date-anchored — caller has to fall back on `date_to`
              from the order itself.
    """
    from datetime import timedelta
    if not sub_crop_start:
        return None
    start = sub_crop_start.date() if hasattr(sub_crop_start, "date") else sub_crop_start
    ft = timeline.from_type.value if hasattr(timeline.from_type, "value") else str(timeline.from_type)
    if ft == "DAS":
        return start + timedelta(days=int(timeline.to_value or 0))
    if ft == "DBS":
        return start - timedelta(days=int(timeline.to_value or 0))
    return None


async def _postpone_window_for_item(
    db: AsyncSession, order: Order, item: OrderItem,
) -> dict:
    """Compute max postpone days for an item per the 2026-05-31
    narrative: max = (timeline_end - today) - 1.

    The -1 guarantees the farmer ≥1 clear day to re-route after a
    postpone elapses naturally. Returns a dict the endpoint can
    serialise without further work.
    """
    from datetime import date as _date, timedelta as _timedelta
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == order.subscription_id)
    )).scalar_one_or_none()
    tl = (await db.execute(
        select(Timeline).where(Timeline.id == item.timeline_id)
    )).scalar_one_or_none()

    # IST today — matches the alerts pipeline (memory:
    # feedback_ist_for_scheduled_tasks). UTC midnight would let the
    # window slip a day for postpones taken during IST-evening.
    ist_today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()

    window_end = _timeline_end_date(tl, sub.crop_start_date) if (tl and sub) else None
    if window_end is None:
        # Fall back to order.date_to for CALENDAR timelines / unset
        # crop_start_date. Lets the dealer still postpone, just bounded
        # by the order's own range.
        window_end = order.date_to.date() if hasattr(order.date_to, "date") else order.date_to

    remaining_days = (window_end - ist_today).days if window_end else 0
    max_days = max(0, remaining_days - 1)
    return {
        "today": ist_today.isoformat(),
        "timeline_end": window_end.isoformat() if window_end else None,
        "remaining_days": remaining_days,
        "max_days": max_days,
        "can_postpone": max_days >= 1,
    }


@router.get("/dealer/orders/{order_id}/items/{item_id}/postpone-window")
async def get_postpone_window(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """How many days can the dealer push this item by?

    Per the 2026-05-31 narrative, the picker on the dealer's PWA
    shows options 1 … max_days where max_days = remaining timeline
    days - 1. This endpoint computes that number authoritatively so
    the picker doesn't have to know about timelines or crop dates.
    """
    await _assert_active_dealer(db, current_user.id)
    order = await _get_dealer_order(db, order_id, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    return await _postpone_window_for_item(db, order, item)


@router.put("/dealer/orders/{order_id}/items/{item_id}/postpone")
async def postpone_item(
    order_id: str, item_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer postpones an item.

    Body shape (Batch 7):
      `days`: int — preferred. Server computes
        `postponed_until = IST today + days` and validates against
        the window.
      `postponed_until`: ISO timestamp — legacy / system path.
        No window validation; used by tests + migrations.

    Picking neither is fine for back-compat with the pre-Batch-7
    PWA but the new picker always sends `days`.
    """
    from datetime import date as _date, timedelta as _timedelta

    await _assert_active_dealer(db, current_user.id)
    order = await _get_dealer_order(db, order_id, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    res = validate_item_transition(item.status, OrderItemStatus.POSTPONED.value, DEALER)
    if not res.allowed:
        _raise_transition(res)
    prev = item.status.value if hasattr(item.status, "value") else item.status

    days = data.get("days")
    if days is not None:
        if not isinstance(days, int) or days < 1:
            raise HTTPException(
                status_code=422,
                detail={"code": "postpone_days_invalid", "message": "days must be a positive integer."},
            )
        window = await _postpone_window_for_item(db, order, item)
        if days > window["max_days"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "postpone_days_out_of_range",
                    "message": f"Pick between 1 and {window['max_days']} day(s).",
                    "max_days": window["max_days"],
                },
            )
        # IST today + days, expressed at IST midnight in UTC.
        ist_today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
        target = ist_today + _timedelta(days=days)
        # Convert IST midnight → UTC = target 00:00 IST = target -5h30m UTC
        item.postponed_until = datetime(
            target.year, target.month, target.day, 0, 0,
            tzinfo=timezone.utc,
        ) - timedelta(hours=5, minutes=30)
    else:
        item.postponed_until = data.get("postponed_until")

    item.status = OrderItemStatus.POSTPONED
    await _record_event(
        db, lineage_id=item.lineage_id,
        event_type="MARKED_POSTPONED",
        actor_user_id=current_user.id, actor_role="DEALER",
        order_id=order_id, order_item_id=item.id,
        prev_status=prev, new_status=OrderItemStatus.POSTPONED.value,
        metadata={
            "postponed_until": item.postponed_until.isoformat() if item.postponed_until else None,
            "days": days,
        },
    )
    await db.commit()
    return {"item_id": item_id, "status": item.status, "postponed_until": item.postponed_until}


@router.put("/dealer/orders/{order_id}/items/{item_id}/not-available")
async def mark_item_unavailable(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_active_dealer(db, current_user.id)
    await _get_dealer_order(db, order_id, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    res = validate_item_transition(item.status, OrderItemStatus.NOT_AVAILABLE.value, DEALER)
    if not res.allowed:
        _raise_transition(res)
    prev = item.status.value if hasattr(item.status, "value") else item.status
    item.status = OrderItemStatus.NOT_AVAILABLE
    await _record_event(
        db, lineage_id=item.lineage_id,
        event_type="MARKED_NOT_AVAILABLE",
        actor_user_id=current_user.id, actor_role="DEALER",
        order_id=order_id, order_item_id=item.id,
        prev_status=prev, new_status=OrderItemStatus.NOT_AVAILABLE.value,
    )
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
    await _assert_active_dealer(db, current_user.id)
    order = await _get_dealer_order(db, order_id, current_user.id)
    res = validate_order_transition(order.status, OrderStatus.SENT_FOR_APPROVAL.value, DEALER)
    if not res.allowed:
        _raise_transition(res)
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

    order.status = OrderStatus.SENT_FOR_APPROVAL
    await db.commit()

    # BL-14 / FCM Batch 4 (2026-05-06): notify the facilitator that
    # the farmer needs to approve. Skipped silently if no facilitator
    # is assigned to the order (direct dealer ↔ farmer flow with no
    # intermediary), or if the facilitator hasn't registered an
    # fcm_token. Spec is explicit that the FCM goes to the
    # facilitator only — the farmer drives the actual approve /
    # reject through the PWA.
    if order.facilitator_user_id:
        facilitator = (await db.execute(
            select(User).where(User.id == order.facilitator_user_id)
        )).scalar_one_or_none()
        if facilitator and facilitator.fcm_token:
            try:
                await send_fcm(
                    token=facilitator.fcm_token,
                    title=SUBMIT_FOR_APPROVAL_FCM_TITLE,
                    body=SUBMIT_FOR_APPROVAL_FCM_BODY,
                    data={
                        "type": "ORDER_AWAITING_FARMER_APPROVAL",
                        "order_id": order.id,
                        "farmer_user_id": order.farmer_user_id,
                    },
                )
            except Exception as e:
                _orders_logger.error(
                    f"FCM send raised unexpectedly for facilitator "
                    f"{facilitator.id}: {e}"
                )

    return {"order_id": order_id, "status": order.status}


@router.put("/dealer/orders/{order_id}/abort")
async def abort_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-10: Dealer aborts an in-flight order — items in dealer-owned
    states revert to PENDING with their fulfilment fields cleared, and
    the order goes back to SENT so a different dealer can pick it up.

    Audit fixes (2026-05-05):
    - dealer ownership check (was missing — any dealer could abort
      anyone's order)
    - is_order_abortable guard so CANCELLED / COMPLETED / EXPIRED
      orders cannot be resurrected
    - per-item is_item_abortable guard so APPROVED / REJECTED /
      REMOVED / SKIPPED items survive the abort (was wiping prior
      farmer approvals)
    - clears all fulfilment fields, not just brand (given_volume,
      volume_unit, price, postponed_until, scan_verified — were
      stale on the resulting PENDING item).
    """
    await _assert_active_dealer(db, current_user.id)
    order = await _get_dealer_order(db, order_id, current_user.id)
    if not is_order_abortable(order.status):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ORDER_NOT_ABORTABLE",
                "message": (
                    f"Order in status '{order.status}' cannot be aborted. "
                    "Abort is only valid for PROCESSING / SENT_FOR_APPROVAL / "
                    "PARTIALLY_APPROVED orders."
                ),
            },
        )

    items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order_id)
    )).scalars().all()
    for item in items:
        if not is_item_abortable(item.status):
            continue
        item.status = OrderItemStatus.PENDING
        item.brand_cosh_id = None
        item.brand_name = None
        item.given_volume = None
        item.volume_unit = None
        item.price = None
        item.postponed_until = None
        item.scan_verified = False

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
    await _assert_active_dealer(db, current_user.id)
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
    await _assert_active_dealer(db, current_user.id)
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
    await _assert_active_facilitator(db, current_user.id)
    q = select(Order).where(Order.facilitator_user_id == current_user.id).order_by(Order.created_at.desc())
    if status_filter:
        q = q.where(Order.status == status_filter)
    result = await db.execute(q)
    orders = result.scalars().all()
    out = []
    for o in orders:
        # Active items only (Batch 8 — exclude timeline-archived).
        items_result = await db.execute(
            select(OrderItem).where(
                OrderItem.order_id == o.id,
                OrderItem.archived_at.is_(None),
            )
        )
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
    await _assert_active_facilitator(db, current_user.id)
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.facilitator_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found or not assigned to you")
    if order.status not in [OrderStatus.SENT, OrderStatus.ACCEPTED]:
        raise HTTPException(status_code=400, detail="Order cannot be routed in current status")
    prev_status = order.status.value if hasattr(order.status, "value") else order.status
    order.dealer_user_id = data["dealer_user_id"]
    order.status = OrderStatus.PROCESSING
    await _record_event(
        db, lineage_id=order.id,
        event_type="ROUTED_TO_DEALER",
        actor_user_id=current_user.id, actor_role="FACILITATOR",
        order_id=order.id,
        prev_status=prev_status, new_status=OrderStatus.PROCESSING.value,
        metadata={"dealer_user_id": order.dealer_user_id},
    )
    await db.commit()
    return {"id": order.id, "status": order.status, "dealer_user_id": order.dealer_user_id}


@router.get("/facilitator/orders/{order_id}")
async def get_facilitator_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_active_facilitator(db, current_user.id)
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.facilitator_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    items_result = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.archived_at.is_(None),
        )
    )
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


# ── Dealer: presence heartbeat (Orders V2 Batch 2) ─────────────────────────────
#
# The dealer's app calls this every ~20 s while the order detail
# screen is mounted. Each call extends `dealer_viewing_until` by
# ~30 s. The farmer's cancel endpoint refuses while that lease is
# in the future — implementing the "farmer can't cancel while the
# dealer is actively working with the order" rule from the
# 2026-05-31 narrative. A closed screen frees the lease within 30 s.
#
# No FCM, no notification — silent on both sides; this is a backend
# coordination signal only.

_DEALER_VIEWING_LEASE_SECONDS = 30

@router.put("/dealer/orders/{order_id}/heartbeat")
async def dealer_heartbeat(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await _get_dealer_order(db, order_id, current_user.id)
    order.dealer_viewing_until = (
        datetime.now(timezone.utc) + timedelta(seconds=_DEALER_VIEWING_LEASE_SECONDS)
    )
    await db.commit()
    return {
        "viewing_until": order.dealer_viewing_until.isoformat(),
        "lease_seconds": _DEALER_VIEWING_LEASE_SECONDS,
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
    await _assert_active_dealer(db, current_user.id)
    from app.services.relations import decode_role

    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    items_result = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.archived_at.is_(None),
        )
    )
    items = items_result.scalars().all()

    # Helper for the flat item shape. NOTE: relies on
    # `element_block_for_item` being defined further down — items
    # are iterated only after the element batch-resolution pass.
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
            # Batch 26 — SE's authored guidance the dealer reads after
            # picking a brand: recommended dosage + unit, application
            # method, volume per plant (plant-wise only).
            "element_block": element_block_for_item(i.id),
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

    # ── Batch 26 — Post-brand-selection element block ──────────────
    # User narrative (2026-05-31): "After he selects the brand he
    # wishes to give, then we need to show him the Recommended
    # Dosage+Unit, Application Method, Volume per Plant+Unit (if it
    # is Plant-wise)."  These come straight from the SE's practice
    # elements; the dealer reads them as guidance for the volume
    # entry below.
    from app.modules.sync.models import CoshCoreItem as _CoshCore

    def _el_get(el, name):
        return getattr(el, name) if hasattr(el, name) else (el.get(name) if isinstance(el, dict) else None)

    # First pass — collect all cosh refs we need to resolve in one
    # round so we don't fan out into N lookups.
    cosh_refs_needed: set[str] = set()
    item_element_specs: dict[str, dict] = {}
    for it in items:
        els = _elements_for_item(it)
        spec = {
            "dosage_value": None,
            "dosage_unit_ref": None,
            "application_method_ref": None,
            "vol_per_plant_value": None,
            "vol_per_plant_unit_ref": None,
        }
        for el in els:
            et = (_el_get(el, "element_type") or "").upper()
            if et == "DOSAGE":
                v = _el_get(el, "value")
                try:
                    spec["dosage_value"] = float(v) if v is not None else None
                except (TypeError, ValueError):
                    spec["dosage_value"] = None
            elif et == "DOSAGE_UNIT":
                spec["dosage_unit_ref"] = _el_get(el, "cosh_ref")
            elif et == "APPLICATION_METHOD":
                spec["application_method_ref"] = _el_get(el, "cosh_ref")
            elif et == "VOLUME_PER_PLANT":
                v = _el_get(el, "value")
                try:
                    spec["vol_per_plant_value"] = float(v) if v is not None else None
                except (TypeError, ValueError):
                    spec["vol_per_plant_value"] = None
            elif et == "VOLUME_PER_PLANT_UNIT":
                spec["vol_per_plant_unit_ref"] = _el_get(el, "cosh_ref")
        item_element_specs[it.id] = spec
        for k in ("dosage_unit_ref", "application_method_ref", "vol_per_plant_unit_ref"):
            if spec[k]:
                cosh_refs_needed.add(spec[k])

    cosh_name_by_id: dict[str, str] = {}
    if cosh_refs_needed:
        cosh_rows = (await db.execute(
            select(_CoshCore).where(_CoshCore.cosh_id.in_(cosh_refs_needed))
        )).scalars().all()
        for cc in cosh_rows:
            tr = cc.translations or {}
            if isinstance(tr, dict):
                cosh_name_by_id[cc.cosh_id] = tr.get("en") or cc.cosh_id

    def _resolve_name(ref):
        return cosh_name_by_id.get(ref) if ref else None

    def element_block_for_item(item_id: str) -> dict:
        s = item_element_specs.get(item_id, {})
        return {
            "dosage_value": s.get("dosage_value"),
            "dosage_unit_cosh_id": s.get("dosage_unit_ref"),
            "dosage_unit_name": _resolve_name(s.get("dosage_unit_ref")),
            "application_method_cosh_id": s.get("application_method_ref"),
            "application_method_name": _resolve_name(s.get("application_method_ref")),
            "vol_per_plant_value": s.get("vol_per_plant_value"),
            "vol_per_plant_unit_cosh_id": s.get("vol_per_plant_unit_ref"),
            "vol_per_plant_unit_name": _resolve_name(s.get("vol_per_plant_unit_ref")),
        }

    def has_locked_brand_item(it: OrderItem) -> bool:
        # Batch 39I-b (2026-05-16) — read the authoritative
        # Practice.is_brand_locked flag. The SE opts in to Brand Lock
        # at authoring time; element presence on its own no longer
        # implies a lock. Practices not loaded (defensive) are treated
        # as unlocked.
        practice = practice_map.get(it.practice_id) if it.practice_id else None
        return bool(practice and practice.is_brand_locked)

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

    # Batch 24 — farmer-context block. Per the user's narrative
    # (2026-05-31): "The dealer needs to know the farmer name, be
    # able to make a call, crop name, crop age (if it is area-wise
    # you will derive it from the difference between the Start date
    # and Today's date, if it is plant-wise you will derive it from
    # the difference between the Planting year and current year),
    # Number of acres/Number of plants (as the case may be)."
    farmer_context = await _build_farmer_context(db, order)

    return {
        "id": order.id, "status": order.status,
        "farmer_user_id": order.farmer_user_id, "client_id": order.client_id,
        "facilitator_user_id": order.facilitator_user_id,
        "date_from": order.date_from, "date_to": order.date_to,
        "created_at": order.created_at,
        # Batch 24 — context the dealer needs to make a call about
        # the order. Hidden from the farmer's view by living on a
        # dealer-side endpoint only.
        "farmer_context": farmer_context,
        # Flat list (unchanged shape for backward compat)
        "items": [item_brief(i) for i in items],
        # New: Part-aware relation structure
        "relations": relations_payload,
        "standalone_items": [item_brief(i) for i in standalone],
    }


async def _build_farmer_context(db: AsyncSession, order: Order) -> dict:
    """Resolve farmer + crop + measure context for the dealer order
    detail. Returns the block the dealer screen renders at the top.

    Crop-age semantics:
      - Plant-wise (subscription.planting_year set): age in YEARS =
        today.year - planting_year.
      - Area-wise (subscription.crop_start_date set, planting_year
        NULL): age in DAYS = today - crop_start_date.
      - Both NULL: no age — farmer hasn't entered the required data
        before placing the order (shouldn't happen given the
        acreage hard-lock on first order; surface as null).
    """
    from datetime import date as _date

    farmer = (await db.execute(
        select(User).where(User.id == order.farmer_user_id)
    )).scalar_one_or_none()
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == order.subscription_id)
    )).scalar_one_or_none()

    crop_name: str | None = None
    if sub is not None:
        package = (await db.execute(
            select(Package).where(Package.id == sub.package_id)
        )).scalar_one_or_none()
        if package is not None:
            from app.modules.sync.models import CoshCoreItem
            crop_row = (await db.execute(
                select(CoshCoreItem).where(CoshCoreItem.cosh_id == package.crop_cosh_id)
            )).scalar_one_or_none()
            if crop_row is not None:
                tr = crop_row.translations or {}
                crop_name = tr.get("en") if isinstance(tr, dict) else None

    measure: str | None = None
    age_value: int | None = None
    age_unit: str | None = None
    today = _date.today()
    if sub is not None:
        if sub.planting_year is not None:
            measure = "PLANT_WISE"
            age_value = today.year - int(sub.planting_year)
            age_unit = "years"
        elif sub.crop_start_date is not None:
            measure = "AREA_WISE"
            cs = sub.crop_start_date.date() if hasattr(sub.crop_start_date, "date") else sub.crop_start_date
            age_value = (today - cs).days
            age_unit = "days"

    return {
        "farmer_name": farmer.name if farmer else None,
        "farmer_phone": farmer.phone if farmer else None,
        "crop_name": crop_name,
        "measure": measure,
        "age_value": age_value,
        "age_unit": age_unit,
        "farm_area_acres": float(sub.farm_area_acres) if (sub and sub.farm_area_acres) else None,
        "number_of_plants": int(sub.number_of_plants) if (sub and sub.number_of_plants) else None,
    }


# ── Missing Brand Reports ─────────────────────────────────────────────────────

@router.post("/dealer/missing-brand-reports", status_code=201)
async def report_missing_brand(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_active_dealer(db, current_user.id)
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


# ── Orders V2 Batch 15 — lineage reporting ─────────────────────────────────
#
# The audit table populated by Batches 3-12 is finally readable. Two
# endpoints power client reports — "show me the full journey of this
# item" and "show me lineages that match these filters". Auth is the
# same SA-or-privileged-CM gate the missing-brand reports use, so the
# CA portal can drop a lineage browser onto its dashboard without a
# new privilege.


def _outcome_from_status(status: str | None) -> str:
    """Coarse-grained categorisation for report dashboards. The
    granular status stays in the events / current.status fields."""
    if status == "APPROVED":
        return "PURCHASED"
    if status in ("NOT_AVAILABLE", "REJECTED"):
        return "RETURNED"
    if status == "POSTPONED":
        return "POSTPONED"
    if status in ("REMOVED", "SKIPPED", "NOT_NEEDED"):
        return "DROPPED"
    if status in ("REROUTED",):
        return "REROUTED"
    if status is None:
        return "UNKNOWN"
    return "IN_FLIGHT"


@router.get("/admin/order-lineage/{lineage_id}")
async def admin_order_lineage(
    lineage_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full journey of one item across dealer hops.

    Returns every event in chronological order + the latest active
    item snapshot + a summary block reports can graph against.
    Item-level events for both OrderItem (pesticide / fertiliser)
    and SeedOrderFull (seed) share the lineage_id namespace; events
    keyed on `order.id` for husk-level transitions (CANCELLED,
    HUSK_DELETED) are folded in automatically when the lineage_id
    happens to equal an order id.
    """
    from app.modules.advisory.router import _assert_sa_or_privileged_cm
    await _assert_sa_or_privileged_cm(db, current_user, "BRAND_HANDLING")

    events = (await db.execute(
        select(OrderItemEvent)
        .where(OrderItemEvent.lineage_id == lineage_id)
        .order_by(OrderItemEvent.created_at.asc())
    )).scalars().all()

    if not events:
        raise HTTPException(status_code=404, detail="Lineage not found")

    # Walk events to derive the current OrderItem (most recent
    # order_item_id with a non-NULL FK). REROUTED rows on a cancelled
    # husk are skipped — the live row is on the latest DRAFT/SENT
    # order downstream of the last REROUTED_TO.
    current_item: OrderItem | None = None
    last_item_id = None
    for ev in events:
        if ev.order_item_id:
            last_item_id = ev.order_item_id
    if last_item_id:
        current_item = (await db.execute(
            select(OrderItem).where(OrderItem.id == last_item_id)
        )).scalar_one_or_none()

    # Dealer hops = distinct order_ids that ever held a non-REROUTED
    # event_type. Each migration creates a new order; counting these
    # gives a useful "how many dealers did this item see?" figure.
    dealer_hop_orders: set[str] = set()
    for ev in events:
        if ev.event_type in ("REROUTED_FROM", "REROUTED_TO"):
            continue
        if ev.order_id:
            dealer_hop_orders.add(ev.order_id)

    current_status = None
    current_payload = None
    if current_item:
        current_status = current_item.status.value if hasattr(current_item.status, "value") else current_item.status
        current_payload = {
            "order_id": current_item.order_id,
            "order_item_id": current_item.id,
            "status": current_status,
            "brand_name": current_item.brand_name,
            "given_volume": float(current_item.given_volume) if current_item.given_volume else None,
            "volume_unit": current_item.volume_unit,
            "price": float(current_item.price) if current_item.price else None,
            "is_archived": current_item.archived_at is not None,
            "archived_at": current_item.archived_at,
        }
    else:
        # Seed lineage — try the seed table.
        from app.modules.seed_mgmt.models import SeedOrderFull
        # Find the latest seed_order_id on the chain.
        last_seed_id = None
        for ev in events:
            if ev.seed_order_id:
                last_seed_id = ev.seed_order_id
        if last_seed_id:
            so = (await db.execute(
                select(SeedOrderFull).where(SeedOrderFull.id == last_seed_id)
            )).scalar_one_or_none()
            if so:
                current_status = so.status
                current_payload = {
                    "seed_order_id": so.id,
                    "status": so.status,
                    "variety_id": so.variety_id,
                    "unit": so.unit,
                    "quantity": float(so.quantity) if so.quantity else None,
                    "total_price": float(so.total_price) if so.total_price else None,
                }

    return {
        "lineage_id": lineage_id,
        "events": [
            {
                "event_type": ev.event_type,
                "actor_role": ev.actor_role,
                "actor_user_id": ev.actor_user_id,
                "order_id": ev.order_id,
                "order_item_id": ev.order_item_id,
                "seed_order_id": ev.seed_order_id,
                "prev_status": ev.prev_status,
                "new_status": ev.new_status,
                "metadata": ev.event_metadata,
                "created_at": ev.created_at,
            }
            for ev in events
        ],
        "current": current_payload,
        "summary": {
            "dealer_hops": len(dealer_hop_orders),
            "total_events": len(events),
            "first_event_at": events[0].created_at,
            "latest_event_at": events[-1].created_at,
            "outcome": _outcome_from_status(current_status),
        },
    }


@router.get("/admin/orders/lineages")
async def admin_list_lineages(
    client_id: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List lineages — one row per distinct lineage_id seen across
    `order_item_events`. Filterable by:
      - `client_id` — joins through the order/seed chain.
      - `outcome` — PURCHASED / RETURNED / POSTPONED / IN_FLIGHT /
        REROUTED / DROPPED — categorisation of the current status.
    Sorted by most-recent event first. Pagination is offset/limit.

    Designed for the CA-portal dashboard's "recent journeys" table.
    """
    from app.modules.advisory.router import _assert_sa_or_privileged_cm
    await _assert_sa_or_privileged_cm(db, current_user, "BRAND_HANDLING")

    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be 1..200")

    # One row per lineage with the latest-event timestamp. The
    # current_status / outcome are computed via a follow-up join on
    # OrderItem (most lineages live there). Seed lineages are folded
    # in via a second pass.
    from sqlalchemy import func as _func
    lineage_rows = (await db.execute(
        select(
            OrderItemEvent.lineage_id,
            _func.max(OrderItemEvent.created_at).label("latest_at"),
        )
        .group_by(OrderItemEvent.lineage_id)
        .order_by(_func.max(OrderItemEvent.created_at).desc())
        .limit(limit * 4)  # over-fetch — filtering happens in Python
        .offset(offset)
    )).all()

    out: list[dict] = []
    from app.modules.seed_mgmt.models import SeedOrderFull
    for lid, latest_at in lineage_rows:
        # Resolve current state via latest OrderItem (preferred) or
        # latest SeedOrderFull on the lineage.
        item = (await db.execute(
            select(OrderItem)
            .where(OrderItem.lineage_id == lid)
            .order_by(OrderItem.updated_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        so = None
        if item is None:
            so = (await db.execute(
                select(SeedOrderFull)
                .where(SeedOrderFull.lineage_id == lid)
                .order_by(SeedOrderFull.updated_at.desc())
                .limit(1)
            )).scalar_one_or_none()

        if item is None and so is None:
            # lineage_id keyed only on an order's own id (husk-level
            # events) — skip; these aren't journeys, they're
            # bookkeeping.
            continue

        # Resolve the parent order to filter by client_id.
        order_client_id = None
        if item:
            ord_row = (await db.execute(
                select(Order).where(Order.id == item.order_id)
            )).scalar_one_or_none()
            if ord_row:
                order_client_id = ord_row.client_id
        elif so:
            order_client_id = so.client_id

        if client_id and order_client_id != client_id:
            continue

        status = (item.status.value if (item and hasattr(item.status, "value")) else
                  (item.status if item else (so.status if so else None)))
        out_outcome = _outcome_from_status(status)
        if outcome and out_outcome != outcome:
            continue

        out.append({
            "lineage_id": lid,
            "latest_event_at": latest_at,
            "current_status": status,
            "outcome": out_outcome,
            "client_id": order_client_id,
            "kind": "seed" if so is not None else "input_item",
        })

        if len(out) >= limit:
            break

    return out


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
    """SA or the CM with BRAND_HANDLING privilege: review and
    approve/reject a missing brand report. Batch U (2026-05-18) —
    privilege actually enforced now (previously the docstring
    claimed it but the function body didn't check)."""
    from app.modules.advisory.router import _assert_sa_or_privileged_cm
    await _assert_sa_or_privileged_cm(db, current_user, "BRAND_HANDLING")
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
    await _assert_active_dealer(db, current_user.id)
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


@router.post("/farmer/orders/{order_id}/reroute-returned", status_code=201)
async def reroute_returned_items(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Orders V2 Batch 10 — bundled re-route of Returned/Rejected/
    Postponed items into a fresh DRAFT.

    The 2026-05-31 narrative (FU-9): farmer doesn't see item names,
    only the dealer does. Three returned items = ONE re-route
    action, not three. The picker on the DRAFT lets the farmer
    pick a new recipient and ship them all together.

    Eligible item statuses:
      - NOT_AVAILABLE (dealer said no, including postpone-expiry
        auto-flips)
      - REJECTED (farmer rejected the dealer's brand/price)
      - POSTPONED (farmer can pull the plug early — narrative:
        "Postponed items can also be cancelled and forwarded")

    Each migrated item keeps its `lineage_id` so the audit trail is
    a single thread through the dealer hops. Original rows stay on
    the source order as REROUTED so reports still see what each
    leg's dealer did. The source order's status recomputes via
    `_update_order_status` so a fully-fulfilled-minus-returns order
    can move on cleanly.
    """
    order = await _get_farmer_order(db, order_id, current_user.id)

    reroutable = {
        OrderItemStatus.NOT_AVAILABLE,
        OrderItemStatus.REJECTED,
        OrderItemStatus.POSTPONED,
    }
    items_q = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.archived_at.is_(None),
        )
    )
    items_to_reroute = [it for it in items_q.scalars().all() if it.status in reroutable]
    if not items_to_reroute:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "nothing_to_reroute",
                "message": "No items in this order need re-routing.",
            },
        )

    new_draft = Order(
        subscription_id=order.subscription_id,
        farmer_user_id=order.farmer_user_id,
        client_id=order.client_id,
        category=order.category,
        date_from=order.date_from,
        date_to=order.date_to,
        status=OrderStatus.DRAFT,
        dealer_user_id=None,
        facilitator_user_id=None,
        locked_timelines=order.locked_timelines,
        expires_at=order.expires_at,
    )
    db.add(new_draft)
    await db.flush()

    for it in items_to_reroute:
        prev_status = it.status.value if hasattr(it.status, "value") else it.status

        # Reset brand/volume/price — the next dealer will fill these
        # afresh. Keep timeline/snapshot/relation so the advisory
        # context survives the leg.
        new_item = OrderItem(
            order_id=new_draft.id,
            practice_id=it.practice_id,
            timeline_id=it.timeline_id,
            brand_cosh_id=None,
            brand_name=None,
            given_volume=None,
            volume_unit=it.volume_unit,
            price=None,
            estimated_volume=it.estimated_volume,
            relation_id=it.relation_id,
            relation_type=it.relation_type,
            relation_role=it.relation_role,
            scan_verified=False,
            status=OrderItemStatus.PENDING,
            snapshot_id=it.snapshot_id,
            lineage_id=it.lineage_id,
        )
        db.add(new_item)
        await db.flush()

        await _record_event(
            db, lineage_id=it.lineage_id,
            event_type="REROUTED_FROM",
            actor_user_id=current_user.id, actor_role="FARMER",
            order_id=order.id, order_item_id=it.id,
            prev_status=prev_status,
            new_status=OrderItemStatus.REROUTED.value,
            metadata={
                "to_order_id": new_draft.id,
                "to_order_item_id": new_item.id,
                "reason": "bundled_reroute",
            },
        )
        await _record_event(
            db, lineage_id=it.lineage_id,
            event_type="REROUTED_TO",
            actor_user_id=current_user.id, actor_role="FARMER",
            order_id=new_draft.id, order_item_id=new_item.id,
            prev_status=OrderItemStatus.REROUTED.value,
            new_status=OrderItemStatus.PENDING.value,
            metadata={
                "from_order_id": order.id,
                "from_order_item_id": it.id,
                "reason": "bundled_reroute",
            },
        )

        it.status = OrderItemStatus.REROUTED

    # Source order's status may now be e.g. COMPLETED if every
    # remaining (non-REROUTED) item is APPROVED.
    await _update_order_status(db, order.id)

    await db.commit()
    return {
        "new_draft_order_id": new_draft.id,
        "rerouted_count": len(items_to_reroute),
    }


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
    """BL-10: Farmer approves all items awaiting approval at once.

    BL-14 audit (2026-05-06): swapped the inline status flip for a
    validate_item_transition pass per item, matching the pattern
    used in approve_order_item / reject_order_item from the BL-10
    rewire. The SQL filter on SENT_FOR_APPROVAL was already
    sufficient defence-in-depth; this adds parity so a future
    refactor doesn't drift between the two approval paths.
    """
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
        res = validate_item_transition(item.status, OrderItemStatus.APPROVED.value, FARMER)
        if not res.allowed:
            _raise_transition(res)
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
    await _assert_active_dealer(db, current_user.id)
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != OrderStatus.SENT:
        raise HTTPException(status_code=400, detail="Order can only be accepted when in SENT status")
    order.status = OrderStatus.PROCESSING
    await _record_event(
        db, lineage_id=order.id,
        event_type="ACCEPTED",
        actor_user_id=current_user.id, actor_role="DEALER",
        order_id=order.id,
        prev_status=OrderStatus.SENT.value,
        new_status=OrderStatus.PROCESSING.value,
    )
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
    await _assert_active_dealer(db, current_user.id)
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
    await _assert_active_dealer(db, current_user.id)
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

def _dealer_profile_complete(profile) -> bool:
    """All shop fields a dealer must capture before the PWA lets
    them into /dealer/home. Drives both UI gating and the
    `is_profile_complete` flag on the GET response.

    Note: licence URLs are intentionally NOT required (2026-05-20
    rule — RootsTalk does not collect dealer licences; verification
    is the client's responsibility, see project_rootstalk_dealer_
    profile_rules.md).
    """
    if profile is None:
        return False
    return (
        bool(profile.shop_name and profile.shop_name.strip())
        and bool(profile.shop_address and profile.shop_address.strip())
        and bool(profile.sell_categories)
        and profile.shop_gps_lat is not None
        and profile.shop_gps_lng is not None
        and bool(profile.shop_registration_url and profile.shop_registration_url.strip())
        and bool(profile.shop_photo_url and profile.shop_photo_url.strip())
    )


@router.get("/dealer/profile")
async def get_dealer_profile(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = (await db.execute(
        select(DealerProfile).where(DealerProfile.user_id == current_user.id)
    )).scalar_one_or_none()
    if not profile:
        return {
            "user_id": current_user.id,
            "sell_categories": [],
            "shop_name": None,
            "is_profile_complete": False,
        }
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
        "is_profile_complete": _dealer_profile_complete(profile),
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

# ── Dealer: Manufacturer catalog (Cosh-driven) ────────────────────────────────

# L2 → category mapping. Pesticides + Special Inputs go under PESTICIDE
# per user 2026-05-21; NPK-dosage L2s are excluded (no trade names →
# no manufacturers, per cosh_options_view.L2_TYPES_WITHOUT_TRADE_NAMES).
_PESTICIDE_L2S = [
    "CHEMICAL_PESTICIDES", "MICROBIAL_PESTICIDES", "BOTANICAL_PESTICIDES",
    "INSECT_BIOCONTROL_AGENTS", "INSECT_TRAPS", "CHEMICAL_HERBICIDES",
    "OTHER_PESTICIDES",
    "ADJUVANTS",  # L1: Special Inputs
]
_FERTILIZER_L2S = [
    "MANURES", "CHEMICAL_FERTILIZER_PRODUCTS",
    "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS", "BIOFERTILIZERS",
    "PGR_TONICS", "SOIL_AMENDMENTS",
]
_CATEGORY_TO_L2S = {
    "PESTICIDE": _PESTICIDE_L2S,
    "FERTILIZER": _FERTILIZER_L2S,
}


async def _walk_cosh_manufacturers(db: AsyncSession, category: str) -> dict[str, str]:
    """Returns {manufacturer_cosh_id: name} for every manufacturer
    that makes a Trade Name under any L2 in the category.

    Three bulk passes over Connect rows, NOT per-CN re-walks. The
    earlier naive implementation called `list_manufacturers_for_
    common_name` once per CN — that re-scanned the entire
    tradename_manufacturer table for every common name, producing
    O(L2 × CN × table-size) row touches and timing out at ~14 min
    on testing's full Cosh data (user report 2026-05-21).

    Bypasses cosh_options_view._complete_trade_names_for_l2 on
    purpose: that filter exists for the SE Add-Practice modal so
    incomplete catalogue entries don't show up; for the dealer
    contract list, an incomplete CN→MFR chain is still a real
    manufacturer worth offering as a contract option.
    """
    from app.services.cosh_constants import (
        COSH_COMMON_NAMES_CORE, COSH_COMMONNAMES_L2_CONNECT,
        COSH_INPUT_MANUFACTURERS_CORE, COSH_L2_DATA_CORE,
        COSH_TRADE_NAMES_CORE, COSH_TRADENAME_COMMONNAME_CONNECT,
        COSH_TRADENAME_MANUFACTURER_CONNECT, PYTHON_L2_TO_COSH_UUID,
    )
    from app.services.cosh_options_view import _resolve_names, _walk_connect

    l2_list = _CATEGORY_TO_L2S.get(category.upper(), [])
    l2_uuids = {PYTHON_L2_TO_COSH_UUID.get(l2) for l2 in l2_list}
    l2_uuids.discard(None)
    if not l2_uuids:
        return {}

    # Pass 1 — commonnames_l2: collect CN cosh_ids belonging to any
    # of the category's L2s.
    eligible_cns: set[str] = set()
    for r in await _walk_connect(db, connect_type=COSH_COMMONNAMES_L2_CONNECT):
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get(COSH_L2_DATA_CORE) in l2_uuids:
            cn = ep.get(COSH_COMMON_NAMES_CORE)
            if cn:
                eligible_cns.add(cn)
    if not eligible_cns:
        return {}

    # Pass 2 — tradename_commonname: collect TN cosh_ids whose CN is
    # in the eligible set.
    eligible_tns: set[str] = set()
    for r in await _walk_connect(db, connect_type=COSH_TRADENAME_COMMONNAME_CONNECT):
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get(COSH_COMMON_NAMES_CORE) in eligible_cns:
            tn = ep.get(COSH_TRADE_NAMES_CORE)
            if tn:
                eligible_tns.add(tn)
    if not eligible_tns:
        return {}

    # Pass 3 — tradename_manufacturer: collect MFR cosh_ids whose
    # TN is in the eligible set.
    mfr_ids: set[str] = set()
    for r in await _walk_connect(db, connect_type=COSH_TRADENAME_MANUFACTURER_CONNECT):
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get(COSH_TRADE_NAMES_CORE) in eligible_tns:
            m = ep.get(COSH_INPUT_MANUFACTURERS_CORE)
            if m:
                mfr_ids.add(m)
    if not mfr_ids:
        return {}

    items = await _resolve_names(
        db, core_type=COSH_INPUT_MANUFACTURERS_CORE, cosh_ids=mfr_ids,
    )
    return {i["cosh_id"]: i["name"] for i in items}


async def _rebuild_manufacturer_catalog(
    db: AsyncSession, *, only_category: str | None = None,
) -> int:
    """Truncate-and-reload the materialised catalog. Returns the
    total number of rows written. Pass `only_category` to refresh
    just one half (the other half's rows are untouched)."""
    from datetime import datetime, timezone
    cats = [only_category.upper()] if only_category else list(_CATEGORY_TO_L2S.keys())
    total = 0
    for cat in cats:
        await db.execute(
            DealerManufacturerCatalog.__table__.delete().where(
                DealerManufacturerCatalog.category == cat,
            )
        )
        mfrs = await _walk_cosh_manufacturers(db, cat)
        now = datetime.now(timezone.utc)
        for cosh_id, name in mfrs.items():
            db.add(DealerManufacturerCatalog(
                category=cat, manufacturer_cosh_id=cosh_id,
                manufacturer_name=name, refreshed_at=now,
            ))
            total += 1
    await db.commit()
    return total


@router.get("/dealer/manufacturers-catalog")
async def dealer_manufacturers_catalog(
    category: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reads from the materialised catalog table — sub-second even
    as Cosh data grows. Lazy-populates on first request per category.

    Refresh-on-Cosh-update is manual for now (admin endpoint
    POST /admin/dealer/manufacturers-catalog/refresh). Staleness
    between Cosh sync and next refresh is acceptable; the catalog
    is a list of manufacturer NAMES, not stock or prices.
    """
    cat = category.upper()
    if cat not in _CATEGORY_TO_L2S:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown category {category!r}. Use PESTICIDE or FERTILIZER.",
        )
    rows = (await db.execute(
        select(DealerManufacturerCatalog).where(
            DealerManufacturerCatalog.category == cat,
        ).order_by(DealerManufacturerCatalog.manufacturer_name)
    )).scalars().all()
    if not rows:
        # Lazy bootstrap. First hit per category after deploy /
        # migration pays the walk cost; everyone afterwards is
        # sub-second.
        await _rebuild_manufacturer_catalog(db, only_category=cat)
        rows = (await db.execute(
            select(DealerManufacturerCatalog).where(
                DealerManufacturerCatalog.category == cat,
            ).order_by(DealerManufacturerCatalog.manufacturer_name)
        )).scalars().all()
    return [
        {"cosh_id": r.manufacturer_cosh_id, "name": r.manufacturer_name}
        for r in rows
    ]


@router.post("/admin/dealer/manufacturers-catalog/refresh")
async def admin_refresh_manufacturer_catalog(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Force-rebuild the dealer manufacturer catalog from Cosh.
    Call after a Cosh sync that added/renamed manufacturers in
    scope. Optional `?category=PESTICIDE` (or FERTILIZER) refreshes
    just one half; omit to refresh both.

    SA-or-CONTENT_MANAGER only — same gate as other admin routes.
    """
    from app.modules.advisory.router import _assert_sa_or_cm
    await _assert_sa_or_cm(db, current_user)
    total = await _rebuild_manufacturer_catalog(
        db, only_category=category if category else None,
    )
    return {"rows_written": total, "category": category or "ALL"}


@router.get("/dealer/dealerships")
async def list_dealerships(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the dealer's selected dealerships. Optional `category`
    (PESTICIDE | FERTILIZER) narrows the response so the PWA tab
    can fetch just what it needs. Legacy free-text rows have no
    `category` and surface only when no filter is applied."""
    q = select(DealerRelationship).where(
        DealerRelationship.dealer_user_id == current_user.id,
        DealerRelationship.status == "ACTIVE",
    )
    if category:
        q = q.where(DealerRelationship.category == category.upper())
    result = await db.execute(q.order_by(DealerRelationship.manufacturer_name))
    rows = result.scalars().all()
    return [{
        "id": r.id,
        "manufacturer_name": r.manufacturer_name,
        "manufacturer_cosh_id": r.manufacturer_cosh_id,
        "manufacturer_client_id": r.manufacturer_client_id,
        "category": r.category,
    } for r in rows]


@router.post("/dealer/dealerships", status_code=201)
async def add_dealership(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a dealership. Cosh-driven path: pass `manufacturer_cosh_id`
    + `manufacturer_name` + `category`. Idempotent — re-adding the
    same (dealer, cosh_id, category) returns the existing row instead
    of creating a duplicate."""
    cosh_id = data.get("manufacturer_cosh_id")
    category = (data.get("category") or "").upper() or None
    if cosh_id and category:
        existing = (await db.execute(
            select(DealerRelationship).where(
                DealerRelationship.dealer_user_id == current_user.id,
                DealerRelationship.manufacturer_cosh_id == cosh_id,
                DealerRelationship.category == category,
                DealerRelationship.status == "ACTIVE",
            )
        )).scalar_one_or_none()
        if existing:
            return {"id": existing.id, "manufacturer_name": existing.manufacturer_name}
    rel = DealerRelationship(
        dealer_user_id=current_user.id,
        manufacturer_name=data["manufacturer_name"],
        manufacturer_cosh_id=cosh_id,
        category=category,
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
    await _assert_active_dealer(db, current_user.id)
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


# ── Dealer: lifecycle status (PWA gate signal, 2026-05-30) ────────────────────

@router.get("/dealer/me/onboarding-status")
async def dealer_onboarding_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """V1.1 Item 5 (2026-05-09 spec) PWA signal.

    Returns whether the current user is *functionally* a Dealer —
    i.e. has at least one ACTIVE `ClientPromoter` row of
    `promoter_type=DEALER`. Used by `/dealer/home` to decide whether
    to render the quick-actions grid or the "ask a Field Manager to
    onboard you" empty state.

    Deliberately not gated on `_assert_active_dealer` — the whole
    point of this endpoint is to *tell* the PWA whether that gate
    would pass. Returning 403 here would defeat the purpose. The
    auth dependency still requires a logged-in user.
    """
    from app.modules.clients.models import ClientPromoter

    count = (await db.execute(
        select(func.count(ClientPromoter.id)).where(
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.promoter_type == "DEALER",
            ClientPromoter.status == "ACTIVE",
        )
    )).scalar() or 0
    return {
        "onboarded": count > 0,
        "client_count": int(count),
    }


# ── Dealer / Facilitator: Promoted farmers ────────────────────────────────────

@router.get("/dealer/promoted-farmers")
async def dealer_promoted_farmers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_active_dealer(db, current_user.id)
    return await _promoted_farmers(db, current_user.id)


@router.get("/facilitator/promoted-farmers")
async def facilitator_promoted_farmers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_active_facilitator(db, current_user.id)
    return await _promoted_farmers(db, current_user.id)


@router.get("/facilitator/promoter-invitations")
async def facilitator_promoter_invitations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """R9 (2026-05-29): list this Facilitator's outstanding Promoter
    invitations — ClientPromoter rows in `PENDING` state — with
    enough Client branding to render an accept/decline card per
    invitation.

    Like /facilitator/onboarding-clients this is NOT gated on
    `_assert_active_facilitator` — an empty list is a valid render
    (no pending invitations). The Facilitator can still see the
    history of past invites later via state-extended versions of
    this endpoint if we add them."""
    from app.modules.clients.models import Client, ClientPromoter

    rows = (await db.execute(
        select(ClientPromoter, Client)
        .join(Client, Client.id == ClientPromoter.client_id)
        .where(
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.promoter_type == "FACILITATOR",
            ClientPromoter.status == "ACTIVE",
            ClientPromoter.promoter_request_status == "PENDING",
        )
        .order_by(ClientPromoter.promoter_request_sent_at.desc())
    )).all()

    return [
        {
            "client_promoter_id": cp.id,
            "client_id": c.id,
            "client_name": c.display_name or c.full_name,
            "short_name": c.short_name,
            "logo_url": c.logo_url,
            "primary_colour": c.primary_colour,
            "sent_at": cp.promoter_request_sent_at,
        }
        for cp, c in rows
    ]


@router.put("/facilitator/promoter-invitations/{client_promoter_id}/accept")
async def facilitator_accept_promoter_invitation(
    client_promoter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """R9 (2026-05-29) Facilitator side: accept a Promoter invitation.

    Transitions PENDING → ACCEPTED, sets `is_promoter=True`, stamps
    `promoter_request_responded_at`.

    §11.2 race guard: at the moment of accept, refuse if the same
    Facilitator is already ACCEPTED elsewhere (e.g., a parallel
    accept won the race). Auth: the row must belong to the caller.

    Other PENDING invitations for this Facilitator are NOT
    auto-declined — the Facilitator may want to keep them around
    in case they later step down from this one. Per §11.2 only
    one ACCEPTED at a time, so those PENDING become future
    options, not active obligations."""
    from app.modules.clients.models import ClientPromoter

    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.id == client_promoter_id,
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.promoter_type == "FACILITATOR",
            ClientPromoter.status == "ACTIVE",
        )
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Promoter invitation not found")
    if cp.promoter_request_status != "PENDING":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "invitation_not_pending",
                "message": (
                    f"This invitation is in state "
                    f"'{cp.promoter_request_status}', not 'PENDING'. "
                    "It may have been revoked, declined earlier, or "
                    "already accepted."
                ),
            },
        )

    accepted_elsewhere = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.promoter_type == "FACILITATOR",
            ClientPromoter.status == "ACTIVE",
            ClientPromoter.is_promoter == True,  # noqa: E712
            ClientPromoter.id != cp.id,
        )
    )).scalar_one_or_none()
    if accepted_elsewhere:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "already_promoter_elsewhere",
                "message": (
                    "You're already a Promoter at another company. "
                    "Step down from that role first to accept this one."
                ),
            },
        )

    from datetime import datetime, timezone
    cp.is_promoter = True
    cp.promoter_request_status = "ACCEPTED"
    cp.promoter_request_responded_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(cp)
    return {
        "id": cp.id,
        "client_id": cp.client_id,
        "is_promoter": cp.is_promoter,
        "promoter_request_status": cp.promoter_request_status,
        "promoter_request_responded_at": cp.promoter_request_responded_at,
    }


@router.put("/facilitator/promoter-invitations/{client_promoter_id}/decline")
async def facilitator_decline_promoter_invitation(
    client_promoter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """R9 (2026-05-29) Facilitator side: decline a pending Promoter
    invitation. Transitions PENDING → DECLINED. `is_promoter` stays
    False. The FM can re-invite later (which will transition
    DECLINED → PENDING)."""
    from app.modules.clients.models import ClientPromoter

    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.id == client_promoter_id,
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.promoter_type == "FACILITATOR",
            ClientPromoter.status == "ACTIVE",
        )
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Promoter invitation not found")
    if cp.promoter_request_status != "PENDING":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "invitation_not_pending",
                "message": (
                    "Only pending invitations can be declined. To leave "
                    "an accepted Promoter role, use the step-down endpoint."
                ),
            },
        )

    from datetime import datetime, timezone
    cp.promoter_request_status = "DECLINED"
    cp.promoter_request_responded_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(cp)
    return {
        "id": cp.id,
        "promoter_request_status": cp.promoter_request_status,
        "promoter_request_responded_at": cp.promoter_request_responded_at,
    }


@router.put("/facilitator/promoter-status/{client_promoter_id}/step-down")
async def facilitator_step_down_promoter(
    client_promoter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """R10 (2026-05-29) Facilitator side: step down from an accepted
    Promoter role. Transitions ACCEPTED → NONE, clears `is_promoter`.

    Symmetric to the Client-side `revoke-promoter` — either side can
    end the Promoter relationship. The Facilitator-onboarding row
    itself is untouched (still ACTIVE for normal Facilitator work)."""
    from app.modules.clients.models import ClientPromoter

    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.id == client_promoter_id,
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.promoter_type == "FACILITATOR",
            ClientPromoter.status == "ACTIVE",
        )
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Promoter row not found")
    if not cp.is_promoter:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_currently_promoter",
                "message": (
                    "You are not currently a Promoter at this company. "
                    "Nothing to step down from."
                ),
            },
        )

    from datetime import datetime, timezone
    cp.is_promoter = False
    cp.promoter_request_status = "NONE"
    cp.promoter_request_responded_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(cp)
    return {
        "id": cp.id,
        "is_promoter": cp.is_promoter,
        "promoter_request_status": cp.promoter_request_status,
        "promoter_request_responded_at": cp.promoter_request_responded_at,
    }


@router.get("/facilitator/onboarding-clients")
async def facilitator_onboarding_clients(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List of Clients that have onboarded the caller as a Facilitator.

    Deliberately *not* gated on `_assert_active_facilitator` — a
    Facilitator who has been deactivated (or never been onboarded)
    should see an empty list, not a 403. The PWA renders the empty
    state with a "Ask a Field Manager to onboard you" prompt.

    Includes `is_promoter` so the UI can mark which Client (if any)
    currently has the caller as their Promoter — at most one per the
    Facilitator-Promoter exclusivity rule (spec §11.2)."""
    from app.modules.clients.models import Client, ClientPromoter

    rows = (await db.execute(
        select(ClientPromoter, Client)
        .join(Client, Client.id == ClientPromoter.client_id)
        .where(
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.promoter_type == "FACILITATOR",
            ClientPromoter.status == "ACTIVE",
        )
        .order_by(ClientPromoter.registered_at.desc())
    )).all()

    return [
        {
            "client_promoter_id": cp.id,
            "client_id": c.id,
            "client_name": c.display_name or c.full_name,
            "short_name": c.short_name,
            "logo_url": c.logo_url,
            "primary_colour": c.primary_colour,
            "is_promoter": cp.is_promoter,
            "onboarded_at": cp.registered_at,
        }
        for cp, c in rows
    ]


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
    await _assert_active_facilitator(db, current_user.id)
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
    await _assert_active_facilitator(db, current_user.id)
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
    await _assert_active_facilitator(db, current_user.id)
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
    await _assert_active_facilitator(db, current_user.id)
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
    order_id: Optional[str] = None,
    client_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns up to 5 nearest dealers filtered by order type
    (PESTICIDE/FERTILISER/SEED).

    V1.1 Item 6 (2026-05-09) — Locked-Brand routing per spec § and
    user clarification 2026-05-09:
      - Dealer pool always restricted to onboarded dealers (any
        active ClientPromoter row of type DEALER somewhere).
      - If `order_id` supplied: derive `client_id` from the order
        and detect locked-brand items.
        Batch 39I-b (2026-05-16) — detection now reads
        Practice.is_brand_locked instead of inferring from
        element_type='brand'. The SE opts in to Brand Lock
        explicitly at authoring time.
      - **Locked**: pool further restricted to dealers onboarded by
        the order's client. Tier = LOCKED_MATCH.
      - **Unlocked but client_id known**: client-onboarded dealers
        get FIRST_DEALER_ADVANTAGE tier; other onboarded dealers
        get OPEN tier. Both returned, sorted by tier then distance.
      - **No client context**: all onboarded dealers, tier OPEN.

    The locked-brand restriction is to dealers onboarded by the
    *order's client*, NOT by the brand's manufacturer. Per user
    2026-05-09: "do not link it with any particular Manufacturer
    of a Brand; it is linked to the dealers who have been
    onboarded by that client".
    """
    from app.modules.clients.models import ClientPromoter

    await _assert_active_facilitator(db, current_user.id)
    if lat is None:
        lat = float(current_user.gps_lat) if current_user.gps_lat else 0.0
    if lng is None:
        lng = float(current_user.gps_lng) if current_user.gps_lng else 0.0

    # Resolve order context: derive target client + locked-brand
    # status from the order's items.
    has_locked = False
    target_client_id = client_id
    if order_id:
        order = (await db.execute(
            select(Order).where(Order.id == order_id)
        )).scalar_one_or_none()
        if order:
            target_client_id = order.client_id
            order_items = (await db.execute(
                select(OrderItem).where(OrderItem.order_id == order_id)
            )).scalars().all()
            practice_ids = [it.practice_id for it in order_items if it.practice_id]
            if practice_ids:
                # Batch 39I-b: Practice.is_brand_locked is the
                # authoritative flag.
                locked_practices = (await db.execute(
                    select(Practice.id).where(
                        Practice.id.in_(practice_ids),
                        Practice.is_brand_locked.is_(True),
                    )
                )).all()
                has_locked = bool(locked_practices)

    # Build the onboarded-dealer pool.
    onboarded_q = select(ClientPromoter).where(
        ClientPromoter.promoter_type == "DEALER",
        ClientPromoter.status == "ACTIVE",
    )
    if has_locked and target_client_id:
        # Locked: hard-restrict to this client's onboarded dealers.
        onboarded_q = onboarded_q.where(
            ClientPromoter.client_id == target_client_id,
        )
    onboarded_rows = (await db.execute(onboarded_q)).scalars().all()
    onboarded_user_ids = {p.user_id for p in onboarded_rows}
    client_onboarded_user_ids = (
        {p.user_id for p in onboarded_rows if p.client_id == target_client_id}
        if target_client_id else set()
    )

    if not onboarded_user_ids:
        return []

    profiles = (await db.execute(
        select(DealerProfile).where(DealerProfile.user_id.in_(onboarded_user_ids))
    )).scalars().all()

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
        if not dealer:
            continue
        if has_locked:
            tier = "LOCKED_MATCH"
        elif profile.user_id in client_onboarded_user_ids:
            tier = "FIRST_DEALER_ADVANTAGE"
        else:
            tier = "OPEN"
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
            "tier": tier,
        })

    # Sort: tier priority, then distance.
    tier_rank = {"LOCKED_MATCH": 0, "FIRST_DEALER_ADVANTAGE": 0, "OPEN": 1}
    results.sort(key=lambda x: (tier_rank.get(x["tier"], 99), x["distance_km"]))
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
    """List pending payment requests for this Facilitator.

    Decorated 2026-05-29 with the context the Facilitator needs to
    decide whether to pay without an extra round-trip: farmer name +
    phone (for tap-to-call), package name + crop name, exact amount,
    and `hours_remaining` (computed from `expires_at`) so the UI can
    show a countdown. Only PENDING rows are surfaced; PAID, DECLINED,
    CANCELLED rows are historical and drop off the active list."""
    from app.modules.subscriptions.models import (
        Subscription, SubscriptionPaymentRequest,
    )
    from app.modules.advisory.models import Package
    from app.modules.platform.models import User as PlatformUser
    from app.modules.sync.models import CoshCoreItem

    await _assert_active_facilitator(db, current_user.id)
    rows = (await db.execute(
        select(SubscriptionPaymentRequest, Subscription, Package, PlatformUser)
        .join(Subscription, Subscription.id == SubscriptionPaymentRequest.subscription_id)
        .join(Package, Package.id == Subscription.package_id)
        .join(PlatformUser, PlatformUser.id == SubscriptionPaymentRequest.farmer_user_id)
        .where(
            SubscriptionPaymentRequest.requested_from_user_id == current_user.id,
            SubscriptionPaymentRequest.status == "PENDING",
        )
        .order_by(SubscriptionPaymentRequest.created_at.desc())
    )).all()

    # Bulk-resolve crop English names so we never echo a raw cosh_id.
    crop_ids = {pkg.crop_cosh_id for _, _, pkg, _ in rows if pkg.crop_cosh_id}
    crop_name_by_id: dict[str, str] = {}
    if crop_ids:
        for r in (await db.execute(
            select(CoshCoreItem).where(CoshCoreItem.cosh_id.in_(crop_ids))
        )).scalars().all():
            tr = r.translations or {}
            crop_name_by_id[r.cosh_id] = tr.get("en") or tr.get("hi") or r.cosh_id

    now = datetime.now(timezone.utc)
    out = []
    for req, sub, pkg, farmer in rows:
        delta = req.expires_at - now
        hours_remaining = max(0, int(delta.total_seconds() // 3600))
        out.append({
            "id": req.id,
            "subscription_id": req.subscription_id,
            "farmer_user_id": req.farmer_user_id,
            "farmer_name": farmer.name,
            "farmer_phone": farmer.phone,
            "package_id": pkg.id,
            "package_name": pkg.name,
            "crop_cosh_id": pkg.crop_cosh_id,
            "crop_name": crop_name_by_id.get(pkg.crop_cosh_id) if pkg.crop_cosh_id else None,
            "amount": float(req.amount),
            "status": req.status,
            "expires_at": req.expires_at,
            "hours_remaining": hours_remaining,
            "created_at": req.created_at,
        })
    return out


@router.put("/facilitator/payment-requests/{request_id}/decline")
async def facilitator_decline_payment(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator declines a payment request at the outset. Notifies
    the farmer via FCM so they know to delegate to someone else (or
    pay themselves) without waiting out the 24-hour expiry."""
    from app.modules.subscriptions.models import SubscriptionPaymentRequest
    from app.modules.platform.models import User as PlatformUser
    from app.services.fcm_service import send_fcm

    await _assert_active_facilitator(db, current_user.id)
    req = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.id == request_id,
            SubscriptionPaymentRequest.requested_from_user_id == current_user.id,
            SubscriptionPaymentRequest.status == "PENDING",
        )
    )).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Payment request not found or no longer pending")
    req.status = "DECLINED"
    await db.commit()

    farmer = (await db.execute(
        select(PlatformUser).where(PlatformUser.id == req.farmer_user_id)
    )).scalar_one_or_none()
    if farmer and farmer.fcm_token:
        try:
            await send_fcm(
                token=farmer.fcm_token,
                title="Payment request declined",
                body=f"{current_user.name or 'Your contact'} declined to pay for your subscription. You can choose someone else or pay yourself.",
                data={
                    "type": "PAYMENT_REQUEST_DECLINED",
                    "subscription_id": req.subscription_id,
                    "payment_request_id": req.id,
                },
            )
        except Exception:
            pass   # FCM failure must not break the API response.

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


async def _practice_ids_have_locked_brand(
    db: AsyncSession, practice_ids: list[str],
) -> bool:
    """Pre-order variant of `_order_has_locked_brand_items`. Used by
    POST /farmer/orders (no Order row exists yet) and by the
    eligible-recipients endpoint that powers the /order/new picker."""
    if not practice_ids:
        return False
    result = await db.execute(
        select(func.count()).select_from(Practice).where(
            Practice.id.in_(practice_ids),
            Practice.is_brand_locked.is_(True),
        )
    )
    return bool((result.scalar() or 0) > 0)


async def _build_eligible_recipients_payload(
    db: AsyncSession,
    *,
    current_user: User,
    client_id: str,
    category: str | None,
    has_locked: bool,
) -> dict:
    """Shared core for both the order-based and new-order
    eligible-recipients endpoints. Filters dealers by licence-
    category match + (if locked) client onboarding; filters
    facilitators to active-only, empty when locked-brand."""
    cat_to_plural = {"PESTICIDE": "PESTICIDES", "FERTILIZER": "FERTILISERS"}
    required_plural = cat_to_plural.get((category or "").upper())

    onboarded_dealer_ids: set[str] = set()
    if has_locked:
        from app.modules.clients.models import ClientPromoter
        rows = (await db.execute(
            select(ClientPromoter.user_id).where(
                ClientPromoter.client_id == client_id,
                ClientPromoter.promoter_type == "DEALER",
                ClientPromoter.status == "ACTIVE",
            )
        )).all()
        onboarded_dealer_ids = {r[0] for r in rows}

    farmer_lat = float(current_user.gps_lat) if current_user.gps_lat else 0.0
    farmer_lng = float(current_user.gps_lng) if current_user.gps_lng else 0.0

    profiles = (await db.execute(select(DealerProfile))).scalars().all()
    dealers: list[dict] = []
    for profile in profiles:
        if required_plural and required_plural not in (profile.sell_categories or []):
            continue
        if has_locked and profile.user_id not in onboarded_dealer_ids:
            continue
        if not profile.shop_gps_lat or not profile.shop_gps_lng:
            continue
        dealer = (await db.execute(select(User).where(User.id == profile.user_id))).scalar_one_or_none()
        if not dealer:
            continue
        from math import radians, sin, cos, asin, sqrt
        lat1, lon1, lat2, lon2 = map(radians, [
            farmer_lat, farmer_lng,
            float(profile.shop_gps_lat), float(profile.shop_gps_lng),
        ])
        a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
        dist = 2 * 6371 * asin(sqrt(a))
        dealers.append({
            "user_id": dealer.id,
            "name": dealer.name,
            "phone": dealer.phone,
            "shop_name": profile.shop_name,
            "shop_address": profile.shop_address,
            "sell_categories": profile.sell_categories or [],
            "distance_km": round(dist, 1),
        })
    dealers.sort(key=lambda x: x["distance_km"])

    facilitators: list[dict] = []
    if not has_locked:
        from app.modules.platform.models import RoleType, UserRole
        fac_users = (await db.execute(
            select(User).join(UserRole, UserRole.user_id == User.id)
            .where(UserRole.role_type == RoleType.FACILITATOR)
        )).scalars().all()
        for fac in fac_users:
            facilitators.append({
                "user_id": fac.id, "name": fac.name, "phone": fac.phone,
            })

    return {
        "category": (category or "").upper() or None,
        "has_locked_brand": has_locked,
        "locked_brand_explainer": (
            "This order has a brand-locked item — only the company's "
            "onboarded dealers can fulfil it."
        ) if has_locked else None,
        "dealers": dealers[:10],
        "facilitators": facilitators[:10],
    }


async def _order_has_locked_brand_items(db: AsyncSession, order_id: str) -> bool:
    """Whether this order contains at least one item whose Practice
    is brand-locked. Drives the Orders V2 (2026-05-31) rule:
    locked-brand orders can only be sent to dealers onboarded by
    the client (no facilitators, no non-onboarded dealers).

    REROUTED / REMOVED items are excluded — historical husk-rows
    shouldn't keep the order constrained.
    """
    excluded = [OrderItemStatus.REROUTED, OrderItemStatus.REMOVED]
    result = await db.execute(
        select(func.count()).select_from(OrderItem)
        .join(Practice, Practice.id == OrderItem.practice_id)
        .where(
            OrderItem.order_id == order_id,
            Practice.is_brand_locked.is_(True),
            OrderItem.status.notin_(excluded),
            # Batch 8: archived items don't constrain the order's
            # routing — they're already off the active surface.
            OrderItem.archived_at.is_(None),
        )
    )
    return bool((result.scalar() or 0) > 0)


async def _is_dealer_onboarded_by_client(
    db: AsyncSession, dealer_user_id: str, client_id: str,
) -> bool:
    """Active onboarding row in client_promoters? Matches what
    `make_onboarded_dealer` writes."""
    from app.modules.clients.models import ClientPromoter
    row = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.client_id == client_id,
            ClientPromoter.user_id == dealer_user_id,
            ClientPromoter.promoter_type == "DEALER",
            ClientPromoter.status == "ACTIVE",
        )
    )).scalar_one_or_none()
    return row is not None


async def _get_farmer_order(db: AsyncSession, order_id: str, farmer_user_id: str) -> Order:
    result = await db.execute(
        select(Order).where(Order.id == order_id, Order.farmer_user_id == farmer_user_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


async def _assert_active_dealer(db: AsyncSession, user_id: str) -> None:
    """V1.1 Item 5 (2026-05-09): dealer-side endpoints require the
    user to be onboarded as an active Dealer at at least one company.
    Per the five-ecosystem architecture, a self-claimed UserRole.
    DEALER is the prerequisite to onboarding but doesn't itself
    authorise order-side actions — onboarding by a company is what
    RootsTalk treats as authentication. User confirmed 2026-05-08:
    "It is only when at least one company onboards a dealer that the
    dealer can receive orders".

    NOT scoped to a specific client — the user can act on orders
    from any company once any one company has onboarded them.
    """
    from app.modules.clients.models import ClientPromoter

    onboarded = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == user_id,
            ClientPromoter.promoter_type == "DEALER",
            ClientPromoter.status == "ACTIVE",
        ).limit(1)
    )).scalar_one_or_none()
    if onboarded is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "not_an_active_dealer",
                "message": (
                    "You aren't currently onboarded as a Dealer at any "
                    "RootsTalk company. Ask a Field Manager to onboard "
                    "you first."
                ),
            },
        )


async def _assert_active_facilitator(db: AsyncSession, user_id: str) -> None:
    """Mirror of `_assert_active_dealer` for facilitator-side
    endpoints. Same rule: at least one ACTIVE FACILITATOR
    ClientPromoter row required."""
    from app.modules.clients.models import ClientPromoter

    onboarded = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == user_id,
            ClientPromoter.promoter_type == "FACILITATOR",
            ClientPromoter.status == "ACTIVE",
        ).limit(1)
    )).scalar_one_or_none()
    if onboarded is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "not_an_active_facilitator",
                "message": (
                    "You aren't currently onboarded as a Facilitator at "
                    "any RootsTalk company. Ask a Field Manager to "
                    "onboard you first."
                ),
            },
        )


async def _get_dealer_order(db: AsyncSession, order_id: str, dealer_user_id: str) -> Order:
    """Mirrors _get_farmer_order for the dealer side. Returns 404 (no
    existence leak) when the order doesn't exist OR is assigned to a
    different dealer — closes the BL-10 audit privilege gap where the
    dealer endpoints accepted any authenticated user.

    Also runs the V1.1 Item 5 onboarding gate so every dealer endpoint
    that resolves an order through this helper inherits the auth
    check for free.
    """
    await _assert_active_dealer(db, dealer_user_id)
    result = await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == dealer_user_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def _raise_transition(res, status_code: int = 400) -> None:
    """Convert a TransitionResult.allowed=False into an HTTPException
    with the stable error_code in the detail."""
    raise HTTPException(
        status_code=status_code,
        detail={"error_code": res.error_code, "message": res.message},
    )


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
