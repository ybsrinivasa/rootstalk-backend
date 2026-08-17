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
from app.services.i18n_cosh import pick_translation, get_locale
from app.services.npk_candidates import load_fertiliser_candidates
from app.services.npk_ranking import (
    Candidate as NPKCandidate, Concentration as NPKConcentration, Dose,
    classify_fertiliser, compute_gap_after_mixed,
    enabled_straights as npk_enabled_straights,
    rank_mixed, straight_kg_for_gap,
)
from app.services.npk_trade_names import (
    group_trade_names_for_dealer,
    trade_names_for_chemical_npk,
    trade_names_for_fertigation_npk,
)
from app.modules.sync.models import CoshCoreItem as _NPKCoshCoreItem
from app.services.bl10_order_state import (
    DEALER, FACILITATOR, FARMER,
    is_item_abortable, is_order_abortable,
    validate_item_transition, validate_order_transition,
)
from app.services.bl14_approval import is_brand_visible_to_farmer
from app.services.bl15_reference import (
    format_reference, parse_sequence, reference_prefix, two_digit_year,
)
from app.services.fcm_service import send_fcm
import logging
from app.modules.advisory.models import RelationType
from app.modules.subscriptions.models import PromoterAssignment, SubscriptionPaymentRequest, AssignmentStatus

_orders_logger = logging.getLogger(__name__)

# BL-14 spec: facilitator gets the FCM "your farmer needs to approve"
# alert when the dealer submits volumes/prices for farmer approval.
# The farmer is the actor (they approve/reject); the facilitator
# gets a courtesy nudge to help follow up if the farmer delays.
SUBMIT_FOR_APPROVAL_FARMER_FCM_TITLE = "Your order needs your approval"
SUBMIT_FOR_APPROVAL_FARMER_FCM_BODY_TPL = (
    "The dealer has sent volumes and prices for order {ref}. "
    "Open RootsTalk to approve or return items."
)
SUBMIT_FOR_APPROVAL_FACILITATOR_FCM_TITLE = "Your farmer needs to approve"
SUBMIT_FOR_APPROVAL_FACILITATOR_FCM_BODY_TPL = (
    "The dealer has sent volume and pricing for order {ref}. "
    "Open RootsTalk to nudge the farmer if needed."
)

# 2026-07-16 — Additional order-flow pushes (see sweep in the same
# commit). All fire-and-forget, all skipped silently when the target
# hasn't registered an fcm_token yet.
# Templates use {ref} for the order's reference_number so every
# push identifies WHICH order it's about — a farmer or dealer with
# several open orders would otherwise not be able to tell them
# apart from the title alone.
DEALER_ACCEPT_FARMER_FCM_TITLE = "Your order has been accepted"
DEALER_ACCEPT_FARMER_FCM_BODY_TPL = (
    "The dealer has accepted order {ref} and started processing it. "
    "You'll be notified when it's ready for pickup."
)
FACILITATOR_ACCEPT_FARMER_FCM_TITLE = "Your order has been accepted"
FACILITATOR_ACCEPT_FARMER_FCM_BODY_TPL = (
    "The facilitator has accepted order {ref}. They'll coordinate "
    "with the dealer and let you know when it's ready."
)
PACKING_PICKED_UP_FARMER_FCM_TITLE = "Your order is on the way"
PACKING_PICKED_UP_FARMER_FCM_BODY_TPL = (
    "The facilitator has picked up order {ref} from the dealer. "
    "Confirm in RootsTalk when you receive it."
)
PACKING_RECEIVED_DEALER_FCM_TITLE = "Order marked received"
PACKING_RECEIVED_DEALER_FCM_BODY_TPL = (
    "The farmer has confirmed they received order {ref}."
)


def _fmt_order_body(template: str, order) -> str:
    """Fill {ref} with the order's reference_number when present;
    otherwise drop the placeholder cleanly (older orders may not
    have a reference)."""
    return template.format(ref=order.reference_number or "").replace(" .", ".").replace("  ", " ").strip()


async def _push_order_sent_recipient(db, order, farmer_name: str) -> None:
    """Notify whichever party — dealer or facilitator — the farmer
    routed this order to. Called from every code path that transitions
    an order INTO OrderStatus.SENT (three born-SENT create paths plus
    the DRAFT → SENT endpoint), so a bare `POST /farmer/orders` push
    reaches the recipient the same way `/farmer/orders/{id}/send`
    does. Fire-and-forget; skipped silently if the recipient hasn't
    registered an fcm_token."""
    recipient_id = order.dealer_user_id or order.facilitator_user_id
    if not recipient_id:
        return
    recipient = (await db.execute(
        select(User).where(User.id == recipient_id)
    )).scalar_one_or_none()
    if not recipient or not recipient.fcm_token:
        return
    ref = order.reference_number or ""
    try:
        await send_fcm(
            token=recipient.fcm_token,
            title=f"New order from {farmer_name or 'a farmer'}",
            body=(
                f"{ref}. Review the items and share volumes and prices."
                if ref else "Review the items and share volumes and prices."
            ),
            data={
                "type": "ORDER_SENT_TO_RECIPIENT",
                "order_id": order.id,
                "farmer_name": farmer_name or "",
                "click_action": (
                    f"/dealer/orders/{order.id}"
                    if order.dealer_user_id
                    else f"/facilitator/orders/{order.id}"
                ),
            },
        )
    except Exception as e:
        _orders_logger.error(
            f"FCM send raised unexpectedly for recipient {recipient.id}: {e}"
        )

router = APIRouter(tags=["Orders"])


def _released_from_fields(order, recipients: dict) -> dict:
    """Build the "Cancelled by you · from X" hint fields for a Manage
    tab card. Called for every order in the list; returns empty when
    the released_* columns are NULL (i.e. not a cancel-migrate DRAFT).

    Recipients dict is the batch-load result from load_recipients — a
    per-user_id RecipientInfo. Falls back gracefully if the user id
    was somehow not in the batch (e.g. deleted-user).
    """
    dealer_id = getattr(order, "released_dealer_user_id", None)
    facilitator_id = getattr(order, "released_facilitator_user_id", None)
    if not dealer_id and not facilitator_id:
        return {}
    rcp = (
        recipients.get(facilitator_id) if facilitator_id
        else recipients.get(dealer_id)
    )
    if rcp is None:
        return {}
    return {
        "released_from_recipient_name": rcp.name,
        "released_from_recipient_shop_name": rcp.shop_name,
        "released_from_recipient_role": rcp.role,
    }


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
    # 2026-06-30 — CHA / QA timelines (DAYS_AFTER_DETECTION /
    # DAYS_AFTER_RESPONSE) are EXCLUDED from this rule per user
    # direction: "don't associate the CHA-PG, CHA-SP, and QA timelines
    # with any DBS, DAS, or Calendar." Those pipes are pest-driven and
    # anchored to events outside the CCA calendar, so they should be
    # combinable with any CCA selection. The check now only fires when
    # the order mixes two or more of {DBS, DAS, CALENDAR}; any number
    # of CHA / QA items can ride alongside without tripping it.
    CCA_TIMING_TYPES = {"DBS", "DAS", "CALENDAR"}
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
        cca_timing_types = timing_types & CCA_TIMING_TYPES
        if len(cca_timing_types) > 1:
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
    # dealer.
    #
    # 2026-06-18 — facilitator branch removed. The earlier code raised
    # 409 when has_locked + facilitator_user_id, but per the seed-flow
    # audit (Points 3b + 3c) and the DBS path's permissive treatment
    # at line ~1586, the farmer CAN route a locked-brand order
    # through a facilitator. The brand-lock enforces on the
    # facilitator's onward route-to-dealer hop (which got its own
    # brand-lock check the same day at line ~3819). PWA was rendering
    # the 409's structured detail as "Page Not Found" because the
    # error handler typed `detail` as string.
    if request.practice_ids:
        has_locked = await _practice_ids_have_locked_brand(db, request.practice_ids)
        if has_locked:
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

    # 2026-06-07 — Root order creation: generate the human-readable
    # Order ID. Every reroute / cancel-migrate child inherits this
    # value so the whole lineage shares one ID across all three PWAs.
    reference_number = await _generate_order_reference(db)
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
        reference_number=reference_number,
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

    # This create endpoint births the order directly in SENT — the
    # PWA calls it with dealer_user_id / facilitator_user_id set, no
    # DRAFT step. Push the recipient the same way /send does.
    await _push_order_sent_recipient(db, order, current_user.name or "a farmer")
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

    Bundling rule (locked 2026-05-21, NPK re-included 2026-07-13):
      - category PESTICIDE → L1 in {PESTICIDE, SPECIAL_INPUT}
      - category FERTILIZER → L1 in {FERTILIZER} (NPK dosages
        included — the /npk-options + /npk-select dealer flow
        handles their fulfilment)
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

    from app.services.order_meta import (
        load_meta_for_subscription_ids, load_recipients,
    )

    q = select(Order).where(Order.farmer_user_id == current_user.id).order_by(Order.created_at.desc())
    if status_filter:
        q = q.where(Order.status == status_filter)
    result = await db.execute(q)
    orders = result.scalars().all()

    # Phase 1 of the farmer-side Orders restructure (2026-06-02) —
    # each card needs crop name, company, start date so the farmer can
    # tell at a glance which subscription an order belongs to. Batch-
    # loads all the join chain in one round.
    meta_by_sub = await load_meta_for_subscription_ids(
        db, [o.subscription_id for o in orders],
        lang=current_user.language_code or "en",
    )
    # 2026-06-02 — surface recipient (dealer / facilitator) name +
    # shop + phone on every card so tracking an order doesn't
    # require drilling into the detail page.
    recipients = await load_recipients(
        db,
        [o.dealer_user_id for o in orders],
        [o.facilitator_user_id for o in orders],
    )

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

        meta = meta_by_sub.get(o.subscription_id)
        # 2026-06-21 — Facilitator wins when both are set: a
        # facilitator-routed order is always "with the facilitator"
        # from the farmer's perspective, even after the facilitator
        # has forwarded to a dealer. The dealer is the facilitator's
        # choice; the farmer doesn't deal with the dealer directly
        # on that flow. Direct-to-dealer orders still resolve to
        # the dealer.
        rcp = (
            recipients.get(o.facilitator_user_id)
            if o.facilitator_user_id
            else recipients.get(o.dealer_user_id)
        )
        out.append({
            "id": o.id,
            "status": o.status,
            # 2026-06-07 — Human-readable Order ID; shared across all
            # orders in a lineage. May be null on legacy rows where
            # the backfill missed.
            "reference_number": o.reference_number,
            "date_from": o.date_from,
            "date_to": o.date_to,
            "dealer_user_id": o.dealer_user_id,
            "facilitator_user_id": o.facilitator_user_id,
            "created_at": o.created_at,
            "item_count": cd.count,
            "is_max_count": cd.is_max,
            # Package-anchor metadata (Phase 1, 2026-06-02). All
            # nullable so a stray subscription-less order doesn't 500
            # the whole list — the PWA renders what's available.
            "subscription_id": o.subscription_id,
            "category": o.category,
            **(meta.to_dict() if meta else {}),
            **(rcp.to_dict() if rcp else {}),
        })
    return out


@router.get("/farmer/subscriptions/{subscription_id}/orders")
async def list_subscription_orders(
    subscription_id: str,
    include_husks: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Phase 2 of the farmer Orders restructure (2026-06-02): all
    orders (regular + seed) for ONE subscription, so the package
    detail screen can show its own Orders section. Ordered newest
    first.

    Auth: subscription must be the current farmer's. Items are
    counted only when active (archived rows live in History
    elsewhere). Seed-order rows interleave via `kind="SEED"` so the
    PWA can render both shapes in one chronological list.

    2026-06-09 — Husk suppression (parity with /dealer/orders +
    /facilitator/orders). The Manage tab Routed pill matched any
    order with awaiting/returned/pickup === 0, which silently
    included REROUTED-only husks (lineage parents whose items all
    migrated to a new order). New `include_husks=false` default
    skips those husks; `?include_husks=true` lifts the filter for
    the per-crop History page's audit deep-dive. Each SubOrder row
    also ships `rerouted_count` so the PWA can distinguish a husk
    from a live order in History's Cancelled tab.
    """
    from app.modules.advisory.models import Relation
    from app.modules.seed_mgmt.models import SeedOrderFull, SeedOrderStatus, SeedVariety
    from app.modules.subscriptions.models import Subscription
    from app.services.order_meta import (
        load_meta_for_subscription_ids, load_recipients,
    )
    from app.services.relations import (
        PracticeRef, build_structure, compute_count_display,
    )

    # Auth check: the subscription must belong to the caller.
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    meta_by_sub = await load_meta_for_subscription_ids(
        db, [subscription_id],
        lang=current_user.language_code or "en",
    )
    meta_dict = meta_by_sub.get(subscription_id).to_dict() if meta_by_sub.get(subscription_id) else {}

    # ── Regular orders ────────────────────────────────────────────────
    regular_rows = (await db.execute(
        select(Order).where(
            Order.subscription_id == subscription_id,
            Order.farmer_user_id == current_user.id,
        ).order_by(Order.created_at.desc())
    )).scalars().all()

    # 2026-08-17 (per-batch rework) — load all PackingList rows per
    # order keyed by (order_id, approval_round). Farmer's Manage tab
    # now reads packing_batches[] on each order to render one Pickup
    # pill entry per not-yet-received batch. The prior single-PL model
    # conflated staggered approval rounds and blocked Pickup on the
    # new batch once the prior batch had been received.
    pls_by_order: dict[str, dict[int, PackingList]] = {}
    if regular_rows:
        regular_ids = [o.id for o in regular_rows]
        pl_rows = (await db.execute(
            select(PackingList).where(PackingList.order_id.in_(regular_ids))
        )).scalars().all()
        for pl in pl_rows:
            round_key = pl.approval_round or 1
            pls_by_order.setdefault(pl.order_id, {})[round_key] = pl
        # Lazy-create a PL row per (order, round) with APPROVED items.
        # Mirror of the /dealer/orders path so the farmer sees packing
        # codes as soon as items land in a fresh round.
        created_any_farmer = False
        for o in regular_rows:
            o_items = (await db.execute(
                select(OrderItem).where(
                    OrderItem.order_id == o.id,
                    OrderItem.archived_at.is_(None),
                    OrderItem.status == OrderItemStatus.APPROVED,
                )
            )).scalars().all()
            approved_rounds = {(i.approval_round or 1) for i in o_items}
            existing_rounds = set(pls_by_order.get(o.id, {}).keys())
            for round_n in approved_rounds - existing_rounds:
                new_pl = PackingList(
                    order_id=o.id,
                    approval_round=round_n,
                    pdf_url=None,
                    packing_code=await _generate_packing_code(db),
                )
                db.add(new_pl)
                await db.flush()
                pls_by_order.setdefault(o.id, {})[round_n] = new_pl
                created_any_farmer = True
        if created_any_farmer:
            await db.commit()

    # 2026-06-02 — recipient info batch-loaded once for the whole
    # list so cards can show dealer / facilitator name + shop +
    # phone without per-row lookups.
    # 2026-08-11 — Include released_* user_ids in the batch so the
    # Returned pill card can show "Cancelled by you · from X" without
    # a separate lookup per DRAFT.
    recipients = await load_recipients(
        db,
        [o.dealer_user_id for o in regular_rows]
            + [o.released_dealer_user_id for o in regular_rows],
        [o.facilitator_user_id for o in regular_rows]
            + [o.released_facilitator_user_id for o in regular_rows],
    )

    regular_out: list[dict] = []
    for o in regular_rows:
        items = (await db.execute(
            select(OrderItem).where(
                OrderItem.order_id == o.id,
                OrderItem.archived_at.is_(None),
            )
        )).scalars().all()

        by_relation: dict[str, list[OrderItem]] = {}
        standalone_items: list[OrderItem] = []
        for it in items:
            if it.relation_id and it.relation_role:
                by_relation.setdefault(it.relation_id, []).append(it)
            else:
                standalone_items.append(it)

        structures = []
        if by_relation:
            practice_ids = list({
                i.practice_id for rel_items in by_relation.values()
                for i in rel_items
            })
            practices = (await db.execute(
                select(Practice).where(Practice.id.in_(practice_ids))
            )).scalars().all()
            practice_map = {p.id: p for p in practices}
            relations = (await db.execute(
                select(Relation).where(Relation.id.in_(list(by_relation.keys())))
            )).scalars().all()
            rel_type_map = {
                r.id: (r.relation_type.value if hasattr(r.relation_type, "value")
                       else str(r.relation_type))
                for r in relations
            }
            for rel_id, rel_items in by_relation.items():
                practice_refs = []
                for it in rel_items:
                    prac = practice_map.get(it.practice_id)
                    if prac and it.relation_role:
                        practice_refs.append(PracticeRef(
                            practice_id=it.practice_id,
                            common_name_cosh_id=prac.common_name_cosh_id,
                            is_special_input=prac.is_special_input,
                            role=it.relation_role,
                        ))
                if not practice_refs:
                    continue
                rel_type = rel_type_map.get(rel_id, "OR")
                try:
                    structures.append(build_structure(practice_refs, rel_id, rel_type))
                except ValueError:
                    standalone_items.extend(rel_items)

        cd = compute_count_display(structures, len(standalone_items))
        # Per-status item breakdown so the PWA's Manage tab can
        # surface only the counts that matter — without ever
        # exposing item names. Names stay hidden from the farmer
        # for anti-manipulation reasons; the count is what they
        # need to act on (approve N, send N returned to another
        # dealer).
        AWAITING = {OrderItemStatus.SENT_FOR_APPROVAL}
        RETURNED = {
            OrderItemStatus.NOT_AVAILABLE,
            OrderItemStatus.REJECTED,
        }
        # 2026-06-03 — POSTPONED is no longer counted under "returned".
        # Surfaced separately so the nudge modal can ask "you also have
        # N postponed items with this dealer — cancel them and bundle?"
        POSTPONED = {OrderItemStatus.POSTPONED}
        sfa_items_for_o = [i for i in items if i.status in AWAITING]
        awaiting_count = len(sfa_items_for_o)
        returned_count = sum(1 for i in items if i.status in RETURNED)
        postponed_count = sum(1 for i in items if i.status in POSTPONED)
        approved_count = sum(1 for i in items if i.status == OrderItemStatus.APPROVED)
        # 2026-08-14 (Phase 2 rework): Final Confirmation splits APPROVED
        # into two sub-states. `awaiting_final_confirmation` = dealer
        # has yet to commit; `pickup_ready` = dealer has stamped
        # final_confirmed_at and the item is ready for hand-off.
        awaiting_final_confirmation_count = sum(
            1 for i in items
            if i.status == OrderItemStatus.APPROVED and i.final_confirmed_at is None
        )
        final_confirmed_count = sum(
            1 for i in items
            if i.status == OrderItemStatus.APPROVED and i.final_confirmed_at is not None
        )
        # 2026-08-13 — U-turn model: order stays with the dealer as one
        # unit until fully settled. Returned pill on the farmer + faci-
        # litator PWAs gates on active_item_count == 0 (no more in-flight
        # dealer work of any kind) so N/A items don't leak to the farmer
        # while a POSTPONE or unsubmitted PENDING/AVAILABLE is still open.
        # 2026-08-14 — Phase 2: APPROVED-awaiting-Final-Confirm also
        # counts as active (dealer's Final Confirmation is the remaining
        # in-flight work; farmer's Cancel should still release it).
        active_item_count = (
            sum(
                1 for i in items if i.status in (
                    OrderItemStatus.PENDING,
                    OrderItemStatus.AVAILABLE,
                    OrderItemStatus.POSTPONED,
                    OrderItemStatus.SENT_FOR_APPROVAL,
                )
            )
            + awaiting_final_confirmation_count
        )
        # 2026-06-09 — REROUTED items live on a husk after a reroute /
        # cancel-migrate. Count separately so the PWA's History page
        # can show "lineage husk" rows under Cancelled.
        rerouted_count = sum(1 for i in items if i.status == OrderItemStatus.REROUTED)
        live_items = [i for i in items if i.status != OrderItemStatus.REROUTED]
        # Pure husk = the order has items, but every active item is
        # REROUTED (no live work). Drop from default response; lift
        # via `include_husks=true` for History.
        if items and not live_items and not include_husks:
            continue
        # 2026-06-05 — Round queueing for the Manage tab card. The PWA
        # already filters to "earliest awaiting order" globally; this
        # surfaces "Approval 1 of 2" WITHIN one order when the dealer
        # has submitted a postpone-resolve while the original batch
        # is still being decided.
        current_round_for_o = (
            min((i.approval_round for i in sfa_items_for_o if i.approval_round is not None),
                default=None)
            if sfa_items_for_o else None
        )
        queued_rounds_for_o = sorted({
            i.approval_round for i in sfa_items_for_o if i.approval_round is not None
        })
        awaiting_in_current_round = sum(
            1 for i in sfa_items_for_o
            if current_round_for_o is None or i.approval_round == current_round_for_o
        )
        # 2026-08-17 (per-batch approvals) — expose approval_batches so
        # the farmer's For Approval pill can render one card per batch
        # (dealer submission round), not one per order. Each batch
        # carries item_count so the pill card leads with "N items to
        # approve" + the batch tag when there's more than one round.
        approval_by_round: dict[int, int] = {}
        for it in items:
            if it.status == OrderItemStatus.SENT_FOR_APPROVAL:
                approval_by_round[it.approval_round or 1] = (
                    approval_by_round.get(it.approval_round or 1, 0) + 1
                )
        approval_batches: list[dict] = [
            {"approval_round": r, "item_count": approval_by_round[r]}
            for r in sorted(approval_by_round.keys())
        ]
        # 2026-08-17 (per-batch rework) — assemble packing_batches from
        # APPROVED items grouped by approval_round. Each batch tracks
        # its own Final Confirmation + Pickup state so the farmer's
        # Pickup pill can render per-batch cards independently. Old
        # (received) batches stop appearing without leaking into new
        # batches' visibility.
        order_pls = pls_by_order.get(o.id, {})
        approved_by_round_farmer: dict[int, list[OrderItem]] = {}
        for it in items:
            if it.status == OrderItemStatus.APPROVED:
                approved_by_round_farmer.setdefault(it.approval_round or 1, []).append(it)
        packing_batches: list[dict] = []
        pickup_ready_item_count = 0
        for round_n in sorted(approved_by_round_farmer.keys()):
            batch_items = approved_by_round_farmer[round_n]
            pl_row = order_pls.get(round_n)
            awaiting_fc = sum(1 for i in batch_items if i.final_confirmed_at is None)
            final_confirmed = sum(1 for i in batch_items if i.final_confirmed_at is not None)
            batch_all_final = awaiting_fc == 0 and final_confirmed > 0
            batch_received = pl_row is not None and pl_row.farmer_received_at is not None
            batch_pickup_role: str | None = None
            if pl_row and pl_row.picked_up_by_user_id and pl_row.picked_up_at:
                if pl_row.picked_up_by_user_id == o.facilitator_user_id:
                    batch_pickup_role = "FACILITATOR"
                else:
                    batch_pickup_role = "FARMER"
            if batch_all_final and not batch_received:
                pickup_ready_item_count += len(batch_items)
            packing_batches.append({
                "approval_round": round_n,
                "packing_list_id": pl_row.id if pl_row else None,
                "packing_code": pl_row.packing_code if pl_row else None,
                "shared_at": (
                    pl_row.first_shared_at.isoformat()
                    if pl_row and pl_row.first_shared_at else None
                ),
                "picked_up_at": (
                    pl_row.picked_up_at.isoformat()
                    if pl_row and pl_row.picked_up_at else None
                ),
                "picked_up_by_role": batch_pickup_role,
                "farmer_received_at": (
                    pl_row.farmer_received_at.isoformat()
                    if pl_row and pl_row.farmer_received_at else None
                ),
                "awaiting_final_confirmation": awaiting_fc,
                "final_confirmed": final_confirmed,
                "all_final_confirmed": batch_all_final,
                "item_count": len(batch_items),
            })
        # 2026-06-21 — Facilitator wins when both are set: a
        # facilitator-routed order is always "with the facilitator"
        # from the farmer's perspective, even after the facilitator
        # has forwarded to a dealer. The dealer is the facilitator's
        # choice; the farmer doesn't deal with the dealer directly
        # on that flow. Direct-to-dealer orders still resolve to
        # the dealer.
        rcp = (
            recipients.get(o.facilitator_user_id)
            if o.facilitator_user_id
            else recipients.get(o.dealer_user_id)
        )
        regular_out.append({
            "kind": "REGULAR",
            "id": o.id,
            "status": o.status,
            # 2026-06-07 — Human-readable Order ID (shared across
            # lineage). Surfaced as a prominent chip on the Manage
            # card so the farmer recognises the same order across
            # surfaces / conversations with dealer + facilitator.
            "reference_number": o.reference_number,
            "date_from": o.date_from,
            "date_to": o.date_to,
            "dealer_user_id": o.dealer_user_id,
            "facilitator_user_id": o.facilitator_user_id,
            # 2026-08-11 — 30s heartbeat lease from the dealer PWA.
            # Farmer's Cancel button uses this to short-circuit the
            # confirm dialog when the dealer is actively viewing the
            # order — showing the "please wait" message directly
            # instead of asking the farmer to confirm a cancel that
            # the server would immediately refuse with 409.
            "dealer_viewing_until": o.dealer_viewing_until,
            "created_at": o.created_at,
            "item_count": cd.count,
            "is_max_count": cd.is_max,
            "awaiting_approval_count": awaiting_in_current_round,
            "awaiting_approval_total": awaiting_count,
            "approval_round_current": current_round_for_o,
            "approval_rounds_pending": len(queued_rounds_for_o),
            "returned_count": returned_count,
            "postponed_count": postponed_count,
            "active_item_count": active_item_count,
            # 2026-08-14 (Phase 2): awaiting_final_confirmation_count
            # is APPROVED items with final_confirmed_at IS NULL. The
            # farmer's Routed card uses this to render the "dealer's
            # Final Confirmation pending" state; the Pickup pill gates
            # on final_confirmed_count instead.
            "awaiting_final_confirmation_count": awaiting_final_confirmation_count,
            # 2026-06-09 — Lineage husk indicator. PWA History page
            # uses this to surface a "lineage husk" row under
            # Cancelled even when order.status is still PROCESSING.
            "rerouted_count": rerouted_count,
            # 2026-08-17 (per-batch rework) — pickup_ready_count now
            # counts items in Final-Confirmed AND not-yet-received
            # batches only. Batch-level details live in packing_batches.
            "pickup_ready_count": pickup_ready_item_count,
            "packing_batches": packing_batches,
            # 2026-08-17 — Per-batch approvals. One entry per SFA round
            # so the farmer's For Approval pill can render one card per
            # dealer submission (not one card per order).
            "approval_batches": approval_batches,
            # Legacy top-level fields (canonical = earliest unresolved
            # batch). Slated for removal once the farmer PWA fully
            # switches to packing_batches.
            **_legacy_packing_fields_farmer(packing_batches),
            # 2026-06-03 — Lineage so the Manage tab can group sub-
            # orders under one card per original procurement intent.
            # When null on a legacy row, client treats the order's
            # own id as the root.
            "lineage_root_id": o.lineage_root_id or o.id,
            "subscription_id": o.subscription_id,
            "category": o.category,
            # 2026-08-11 — Cancel-migrate marker so the Manage-tab
            # Returned pill can pick up DRAFT orders that came from a
            # farmer cancel (whole-batch forward-or-discard flow).
            "is_returned_to_farmer": bool(o.is_returned_to_farmer),
            # "Cancelled by you · Previously with X" hint. Only set
            # on returned-to-farmer DRAFTs; NULL on every other order.
            **_released_from_fields(o, recipients),
            # 2026-08-12 — Chip text differentiator for the Returned
            # pill card: farmer_cancel / dealer_declined / facilitator_declined.
            "return_reason": getattr(o, "return_reason", None),
            **meta_dict,
            **(rcp.to_dict() if rcp else {}),
        })

    # ── Seed orders ───────────────────────────────────────────────────
    seed_rows = (await db.execute(
        select(SeedOrderFull).where(
            SeedOrderFull.subscription_id == subscription_id,
            SeedOrderFull.farmer_user_id == current_user.id,
        ).order_by(SeedOrderFull.created_at.desc())
    )).scalars().all()

    # Seed recipients fetched after the rows are known.
    # 2026-08-11 — Include released_* user_ids so the Returned pill
    # can show "Cancelled by you · from X" for seed DRAFTs too.
    seed_recipients = await load_recipients(
        db,
        [so.dealer_user_id for so in seed_rows]
            + [so.released_dealer_user_id for so in seed_rows],
        [so.facilitator_user_id for so in seed_rows]
            + [so.released_facilitator_user_id for so in seed_rows],
    )

    seed_out: list[dict] = []
    for so in seed_rows:
        variety = (await db.execute(
            select(SeedVariety).where(SeedVariety.id == so.variety_id)
        )).scalar_one_or_none()
        # 2026-06-21 — Facilitator wins when both are set (parity
        # with regular orders — see sibling rcp resolution above).
        rcp = (
            seed_recipients.get(so.facilitator_user_id)
            if so.facilitator_user_id
            else seed_recipients.get(so.dealer_user_id)
        )
        seed_out.append({
            "kind": "SEED",
            "id": so.id,
            "reference_number": so.reference_number,
            "status": so.status,
            "variety_name": variety.name if variety else None,
            "crop_cosh_id": variety.crop_cosh_id if variety else None,
            "unit": so.unit,
            "quantity": float(so.quantity) if so.quantity else None,
            "total_price": float(so.total_price) if so.total_price else None,
            "created_at": so.created_at,
            "dealer_user_id": so.dealer_user_id,
            "facilitator_user_id": so.facilitator_user_id,
            "subscription_id": so.subscription_id,
            # 2026-08-11 — Seed parity for dealer-presence gate
            # (no dealer-side seed-detail screen today so this stays
            # NULL, but wired in advance).
            "dealer_viewing_until": getattr(so, "dealer_viewing_until", None),
            # SEED has no item-level table; expose a 1/0 awaiting flag
            # so the Manage tab can render the same Approve-all action
            # using the order-level PUT /farmer/seed-orders/{id}/approve.
            "awaiting_approval_count": 1 if so.status == SeedOrderStatus.SENT_FOR_APPROVAL.value else 0,
            # 2026-08-11 — Cancel-migrate marker (parity with regular).
            "is_returned_to_farmer": bool(getattr(so, "is_returned_to_farmer", False)),
            # 2026-08-14 (Phase 2): Final Confirmation timestamp on seed.
            # Farmer PWA's Pickup pill gates on this. ISO string or null.
            "final_confirmed_at": (
                so.final_confirmed_at.isoformat() if so.final_confirmed_at else None
            ),
            # 2026-08-14 (Phase 2): active_item_count for the U-turn
            # visibility gate. Seed has no per-item table, so 1 = seed
            # is in flight with the dealer (SENT/ACCEPTED/AVAILABLE/
            # POSTPONED/SFA/READY_FOR_PICKUP-not-yet-final-confirmed),
            # 0 = quiescent (NOT_AVAILABLE / READY_FOR_PICKUP-Final-
            # Confirmed / terminal).
            "active_item_count": (
                1 if so.status in {
                    SeedOrderStatus.SENT.value, SeedOrderStatus.ACCEPTED.value,
                    SeedOrderStatus.AVAILABLE.value, SeedOrderStatus.POSTPONED.value,
                    SeedOrderStatus.SENT_FOR_APPROVAL.value,
                } or (
                    so.status == SeedOrderStatus.READY_FOR_PICKUP.value
                    and so.final_confirmed_at is None
                ) else 0
            ),
            # 2026-08-14 (Phase 2): matches regular's awaiting_final_
            # confirmation_count. Seeds get 1 when READY_FOR_PICKUP and
            # final_confirmed_at IS NULL; else 0.
            "awaiting_final_confirmation_count": (
                1 if so.status == SeedOrderStatus.READY_FOR_PICKUP.value
                and so.final_confirmed_at is None else 0
            ),
            # 2026-08-14 (Phase 2): pickup_ready_count for seed — 1
            # when Final Confirmed by dealer and farmer hasn't picked up
            # yet, else 0.
            "pickup_ready_count": (
                1 if so.status == SeedOrderStatus.READY_FOR_PICKUP.value
                and so.final_confirmed_at is not None else 0
            ),
            **_released_from_fields(so, seed_recipients),
            "return_reason": getattr(so, "return_reason", None),
            **meta_dict,
            **(rcp.to_dict() if rcp else {}),
        })

    # Merge + chronological (newest first).
    merged = regular_out + seed_out
    merged.sort(key=lambda x: x["created_at"], reverse=True)
    return {"orders": merged, "package_meta": meta_dict}


@router.get("/farmer/orders/{order_id}")
async def get_farmer_order_detail(
    order_id: str,
    approval_round: int | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer's review page for a single order.

    2026-06-03 — Reshaped to bucket items by what the farmer needs to
    act on:
      - approval_items    : SENT_FOR_APPROVAL — brand + manufacturer +
                            qty + price visible; per-item Approve/Reject.
      - postponed_items   : POSTPONED — postponed_until visible; farmer
                            can Cancel (→ NOT_AVAILABLE, joins Returned).
      - returned_items    : NOT_AVAILABLE + REJECTED — farmer can
                            re-route to another dealer or skip.
      - approved_items    : APPROVED — already received; shown for
                            completeness.

    Approval + Approved items are consolidated by (brand_cosh_id,
    volume_unit) per the cross-timeline brand-merge rule (yesterday's
    task 4). Postponed + Returned items are NOT consolidated — each
    underlying timeline keeps its own row so the farmer can act on
    each one independently.

    Anti-manipulation carve-out (2026-06-03): brand+qty+price ARE
    surfaced on this review page for SENT_FOR_APPROVAL items so the
    farmer can make an informed approve decision. The earlier "only
    post-approval" rule applied to the Manage tab card list; this
    review page is the surface where the farmer's decision happens.
    """
    from app.modules.orders.models import BrandLookupCache
    from app.modules.advisory.models import Practice as AdvPractice

    order = await _get_farmer_order(db, order_id, current_user.id)
    # 2026-06-06 — Dealer + facilitator display names so the focused
    # pickup page can render "From Sri Lakshmi Agro Inputs" without
    # a per-page round-trip.
    from app.modules.orders.models import DealerProfile
    dealer_name = None
    if order.dealer_user_id:
        row = (await db.execute(
            select(User.name, DealerProfile.shop_name)
            .join(DealerProfile, DealerProfile.user_id == User.id, isouter=True)
            .where(User.id == order.dealer_user_id)
        )).first()
        if row:
            dealer_name = row[1] or row[0]
    facilitator_name = None
    if order.facilitator_user_id:
        row = (await db.execute(
            select(User.name).where(User.id == order.facilitator_user_id)
        )).first()
        if row:
            facilitator_name = row[0]
    # 2026-06-06 — Lazy-create a PackingList row + code once items
    # have been approved so the farmer sees the Packing ID alongside
    # the approve action.
    # 2026-08-17 — Per-batch rework: when the caller passes
    # ?approval_round=N (Pickup detail page), scope the packing_list to
    # that specific batch. Otherwise fall back to earliest-round as the
    # single-slot representative used for the review page's legacy
    # packing_* fields.
    if approval_round is not None:
        packing_list = (await db.execute(
            select(PackingList).where(
                PackingList.order_id == order.id,
                PackingList.approval_round == approval_round,
            )
        )).scalar_one_or_none()
    else:
        packing_list = (await db.execute(
            select(PackingList)
            .where(PackingList.order_id == order.id)
            .order_by(PackingList.approval_round.asc().nulls_first())
            .limit(1)
        )).scalar_one_or_none()
    items_result = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.archived_at.is_(None),
        )
    )
    items = items_result.scalars().all()

    # Batch-load brand + manufacturer names (locale-aware) for every
    # distinct brand_cosh_id on the order's items.
    brand_ids = sorted({i.brand_cosh_id for i in items if i.brand_cosh_id})
    lang = current_user.language_code or "en"
    manufacturer_by_brand: dict[str, str | None] = {}
    brand_loc: dict[str, str | None] = {}
    if brand_ids:
        rows = (await db.execute(
            select(
                BrandLookupCache.trade_name_cosh_id,
                BrandLookupCache.trade_name,
                BrandLookupCache.trade_name_translations,
                BrandLookupCache.manufacturer_name,
                BrandLookupCache.manufacturer_translations,
            ).where(BrandLookupCache.trade_name_cosh_id.in_(brand_ids))
        )).all()
        for tn_id, tn_en, tn_tr, mfr_en, mfr_tr in rows:
            # Take the first non-null manufacturer per brand. Some
            # brands appear under multiple common_names; same trade-
            # name resolves to same manufacturer in practice.
            if tn_id not in manufacturer_by_brand and mfr_en:
                manufacturer_by_brand[tn_id] = pick_translation(
                    mfr_tr or {}, lang, mfr_en,
                )
            if tn_id not in brand_loc and tn_en:
                brand_loc[tn_id] = pick_translation(
                    tn_tr or {}, lang, tn_en,
                )

    # Batch-load practice names for the Returned + Postponed cards
    # (those rows can't always lean on brand_name since the dealer
    # may not have picked one).
    practice_ids = sorted({i.practice_id for i in items if i.practice_id})
    practice_name_by_id: dict[str, str] = {}
    if practice_ids:
        rows = (await db.execute(
            select(AdvPractice.id, AdvPractice.l2_type)
            .where(AdvPractice.id.in_(practice_ids))
        )).all()
        for p_id, l2 in rows:
            if l2:
                practice_name_by_id[p_id] = l2.replace("_", " ").title()

    def base_row(i: OrderItem) -> dict:
        return {
            "id": i.id,
            "practice_id": i.practice_id,
            "practice_name": practice_name_by_id.get(i.practice_id),
            "status": i.status,
            "brand_cosh_id": i.brand_cosh_id,
            "brand_name": (
                (brand_loc.get(i.brand_cosh_id) or i.brand_name)
                if is_brand_visible_to_farmer(i.status) else None
            ),
            "manufacturer_name": (
                manufacturer_by_brand.get(i.brand_cosh_id)
                if i.brand_cosh_id and is_brand_visible_to_farmer(i.status)
                else None
            ),
            "given_volume": float(i.given_volume) if i.given_volume else None,
            "volume_unit": i.volume_unit,
            "price": float(i.price) if i.price else None,
            "postponed_until": i.postponed_until.isoformat() if i.postponed_until else None,
        }

    # 2026-06-05 — Round-based queueing within a single order. Dealer's
    # bulk submit stamps approval_round=N on every AVAILABLE item; each
    # submit gets its own round (postpone-resolve rounds are +1 on the
    # previous max).
    # 2026-08-17 — Explicit approval_round param: farmer's For Approval
    # pill lists per-batch cards; tapping a batch's card navigates here
    # with ?approval_round=N. If absent (legacy call), fall back to the
    # earliest still-pending round.
    sfa_items = [i for i in items if i.status == OrderItemStatus.SENT_FOR_APPROVAL]
    if approval_round is not None:
        current_round = approval_round
    else:
        current_round = (
            min((i.approval_round for i in sfa_items if i.approval_round is not None), default=None)
            if sfa_items else None
        )
    approval_raw = [
        base_row(i) for i in sfa_items
        if current_round is None or i.approval_round == current_round or i.approval_round is None
    ]
    # 2026-08-17 — Per-batch Pickup: when approval_round is passed, scope
    # approved_items to that specific batch so the /orders/{id}/pickup
    # page's item list matches the batch the farmer tapped. Without this
    # the page merged round 1 items (already picked up) with round 2
    # items via consolidate_purchased_items, and the batch-2 rows either
    # disappeared into a merged same-brand row or looked like already-
    # received items on the Received banner.
    if approval_round is not None:
        approved_raw = [
            base_row(i) for i in items
            if i.status == OrderItemStatus.APPROVED
            and (i.approval_round or 1) == approval_round
        ]
    else:
        approved_raw = [base_row(i) for i in items if i.status == OrderItemStatus.APPROVED]
    postponed_raw = [base_row(i) for i in items if i.status == OrderItemStatus.POSTPONED]
    returned_raw = [
        base_row(i) for i in items
        if i.status in (OrderItemStatus.NOT_AVAILABLE, OrderItemStatus.REJECTED)
    ]
    # Distinct queued rounds (those with at least one SFA item).
    queued_rounds = sorted({i.approval_round for i in sfa_items if i.approval_round is not None})

    return {
        "id": order.id, "status": order.status,
        "reference_number": order.reference_number,
        "date_from": order.date_from, "date_to": order.date_to,
        "created_at": order.created_at,
        "dealer_user_id": order.dealer_user_id,
        "dealer_name": dealer_name,
        "facilitator_user_id": order.facilitator_user_id,
        "facilitator_name": facilitator_name,
        "subscription_id": order.subscription_id,
        "category": order.category,
        # 2026-08-11 — Cancel-migrate marker so the DRAFT detail page
        # can skip its intermediate "Pick a recipient" wrapper and
        # auto-open the picker sheet on mount for returned batches.
        "is_returned_to_farmer": bool(order.is_returned_to_farmer),
        # 2026-06-03 — Bucketed items for the review page. The brand
        # consolidation helper merges same-brand rows across timelines
        # for the approval + approved buckets (so the farmer sees one
        # Agroneem 3.5 L · ₹350 line, not three rows of 1 L / 1 L /
        # 1.5 L). Postponed + Returned stay per-row so the farmer can
        # act on each timeline independently.
        "approval_items": consolidate_purchased_items(approval_raw),
        "approved_items": consolidate_purchased_items(approved_raw),
        "postponed_items": postponed_raw,
        "returned_items": returned_raw,
        # 2026-06-05 — Round queueing context for the PWA banner.
        # approval_round_current: which round the farmer is reviewing.
        # approval_rounds_pending: how many rounds (including current)
        # are still SFA, so the page can render "Approval 1 of 2".
        "approval_round_current": current_round,
        "approval_rounds_pending": len(queued_rounds),
        # 2026-06-06 — Packing surface fields for the farmer review.
        # Lazy-create the row when there's at least one approved item
        # so the Packing ID is visible from the moment the farmer
        # could possibly want to confirm receipt.
        # 2026-08-17 — When the caller passes approval_round (Pickup
        # detail page), the packing_* fields are scoped to that batch.
        **(await _farmer_packing_fields(db, order, packing_list, len(approved_raw), approval_round)),
        # Legacy flat list kept for any pre-2026-06-03 caller.
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


async def _check_cancel_eligibility(order, db: AsyncSession) -> tuple[bool, str | None, str | None]:
    """Shared gate for the cancel flow. Returns (can_cancel, code, message).

    Used by GET /cancel-eligibility (called by the farmer PWA on tap
    of the Cancel button so we can decide confirm-vs-alert BEFORE the
    farmer sees any confirmation dialog) AND by PUT /cancel itself as
    the authoritative check (handles the tiny race between the
    eligibility read and the mutation write).
    """
    from datetime import datetime, timezone
    from app.modules.orders.models import (
        OrderStatus, OrderItem, OrderItemStatus,
    )
    terminal = {OrderStatus.COMPLETED, OrderStatus.CANCELLED, OrderStatus.EXPIRED}
    if order.status in terminal:
        s = order.status.value if hasattr(order.status, "value") else order.status
        return False, "already_terminal", f"Order is already {s}; nothing to cancel."
    now = datetime.now(timezone.utc)
    if order.dealer_viewing_until and order.dealer_viewing_until > now:
        return False, "dealer_currently_viewing", (
            "Your dealer is looking at this order right now. Please try again in a minute."
        )
    pending = (await db.execute(
        select(func.count(OrderItem.id)).where(
            OrderItem.order_id == order.id,
            OrderItem.status == OrderItemStatus.SENT_FOR_APPROVAL,
            OrderItem.archived_at.is_(None),
        )
    )).scalar_one() or 0
    if pending > 0:
        return False, "items_pending_your_approval", (
            "You have items awaiting your approval on this order. Please "
            "approve or reject them first, then you can cancel the "
            "remaining items."
        )
    return True, None, None


@router.get("/farmer/orders/{order_id}/cancel-eligibility")
async def cancel_eligibility(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Tap-time eligibility check for the farmer's Cancel button.

    The farmer PWA fires this BEFORE showing the "Cancel this order?"
    confirm dialog. If can_cancel is false, the PWA shows the message
    directly (e.g. "The dealer has opened your order for processing,
    please wait.") — no confirmation prompt, no failed cancel attempt.

    This matters because dealer_viewing_until is a 30s heartbeat lease
    that can be freshly stamped between the PWA's last list fetch and
    the farmer's cancel tap. Cached data on the client is stale by
    definition; only a server call at tap-time is authoritative.

    The result is authoritative at the moment of the query — the tiny
    race window between this GET and the follow-up PUT is handled by
    the PUT calling the same helper and 409'ing if the state changed.
    """
    order = await _get_farmer_order(db, order_id, current_user.id)
    can_cancel, code, message = await _check_cancel_eligibility(order, db)
    return {"can_cancel": can_cancel, "code": code, "message": message}


@router.put("/farmer/orders/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer's Cancel — releases in-flight items and marks the source
    as returned-to-farmer (Phase 2 rework, 2026-08-14). Supersedes the
    2026-08-11 Model B DRAFT flow.

    Cancel is a RELEASE step, not a fate decision:
      - In-flight items (PENDING / AVAILABLE / POSTPONED + APPROVED-
        with-final_confirmed_at-null i.e. dealer hasn't Final Confirmed
        yet) flip to NOT_AVAILABLE. They stay on the source order as
        unsold items awaiting the farmer's Send-or-Discard decision.
      - APPROVED-Final-Confirmed items stay untouched — they are the
        dealer's physical commitment and continue their Pickup lifecycle.
      - Source order does NOT transition to CANCELLED (that's what
        /discard does). It gets `is_returned_to_farmer=true` +
        `return_reason='farmer_cancel'` + released_dealer_user_id /
        released_facilitator_user_id preserved for the chip context.
      - No DRAFT row is created (Model B fully unwound).

    Blocks: (a) dealer's live heartbeat lease; (b) SFA items awaiting
    farmer's approval — the farmer clears those first via approve/
    reject, then Cancel becomes valid.

    Cascade: all lineage siblings receive the same release + flag flip.
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
                "message": "Your dealer is looking at this order right now. Please try again in a minute.",
            },
        )

    pending_approval_count = (await db.execute(
        select(func.count(OrderItem.id)).where(
            OrderItem.order_id == order.id,
            OrderItem.status == OrderItemStatus.SENT_FOR_APPROVAL,
            OrderItem.archived_at.is_(None),
        )
    )).scalar_one() or 0
    if pending_approval_count > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "items_pending_your_approval",
                "message": (
                    "You have items awaiting your approval on this order. "
                    "Please approve or reject them first, then you can cancel "
                    "the remaining items."
                ),
            },
        )

    prev_order_status = order.status.value if hasattr(order.status, "value") else order.status

    # Cascade across the lineage — every non-terminal sibling gets the
    # same release + flag flip. Reference_number groups the family.
    root_id = order.lineage_root_id or order.id
    sibling_rows = (await db.execute(
        select(Order).where(
            ((Order.lineage_root_id == root_id) | (Order.id == root_id))
            & (Order.id != order.id)
        )
    )).scalars().all()
    siblings_to_release = [
        s for s in sibling_rows if s.status not in terminal
    ]
    if order.lineage_root_id is None:
        order.lineage_root_id = order.id

    def _is_in_flight(it: OrderItem) -> bool:
        # In-flight = still expected from the dealer. Includes APPROVED
        # items the dealer hasn't Final Confirmed yet (they're a farmer-
        # side commitment but not a dealer-side physical commitment).
        if it.status in (
            OrderItemStatus.PENDING,
            OrderItemStatus.AVAILABLE,
            OrderItemStatus.POSTPONED,
        ):
            return True
        if it.status == OrderItemStatus.APPROVED and it.final_confirmed_at is None:
            return True
        return False

    async def _collect_in_flight(target: Order) -> list[OrderItem]:
        items_q = await db.execute(
            select(OrderItem).where(
                OrderItem.order_id == target.id,
                OrderItem.archived_at.is_(None),
            )
        )
        return [it for it in items_q.scalars().all() if _is_in_flight(it)]

    async def _release(target: Order, items: list[OrderItem]) -> None:
        for it in items:
            prev = it.status.value if hasattr(it.status, "value") else it.status
            it.status = OrderItemStatus.NOT_AVAILABLE
            it.postponed_until = None
            await _record_event(
                db, lineage_id=it.lineage_id,
                event_type="RELEASED_BY_FARMER_CANCEL",
                actor_user_id=current_user.id, actor_role="FARMER",
                order_id=target.id, order_item_id=it.id,
                prev_status=prev,
                new_status=OrderItemStatus.NOT_AVAILABLE.value,
            )

    def _flip_return_flag(target: Order) -> None:
        if target.lineage_root_id is None:
            target.lineage_root_id = root_id
        target.is_returned_to_farmer = True
        target.return_reason = 'farmer_cancel'
        if target.dealer_user_id and not target.released_dealer_user_id:
            target.released_dealer_user_id = target.dealer_user_id
        if target.facilitator_user_id and not target.released_facilitator_user_id:
            target.released_facilitator_user_id = target.facilitator_user_id
        # 2026-08-15 (Phase 2 facilitator-flow fix F4): farmer's Cancel
        # pulls the order back from whoever's holding it. Clear FK
        # pointers so facilitator's + dealer's queue filters drop the
        # order; released_* fields preserved for chip context. Also
        # clear is_returned_to_facilitator (that flag is now moot —
        # is_returned_to_farmer supersedes it).
        target.dealer_user_id = None
        target.facilitator_user_id = None
        target.is_returned_to_facilitator = False

    released_from_source = await _collect_in_flight(order)
    await _release(order, released_from_source)
    _flip_return_flag(order)

    cascaded_counts: list[tuple[str, int]] = []
    for sibling in siblings_to_release:
        released = await _collect_in_flight(sibling)
        await _release(sibling, released)
        _flip_return_flag(sibling)
        cascaded_counts.append((sibling.id, len(released)))
        await _record_event(
            db, lineage_id=sibling.id,
            event_type="RETURNED_TO_FARMER_BY_CANCEL",
            actor_user_id=current_user.id, actor_role="FARMER",
            order_id=sibling.id,
            prev_status=(
                sibling.status.value if hasattr(sibling.status, "value")
                else sibling.status
            ),
            new_status=(
                sibling.status.value if hasattr(sibling.status, "value")
                else sibling.status
            ),
            metadata={
                "trigger": "lineage_cascade",
                "source_order_id": order.id,
                "released_item_count": len(released),
            },
        )

    total_released = (
        len(released_from_source)
        + sum(count for _, count in cascaded_counts)
    )
    await _record_event(
        db, lineage_id=order.id,
        event_type="RETURNED_TO_FARMER_BY_CANCEL",
        actor_user_id=current_user.id, actor_role="FARMER",
        order_id=order.id,
        prev_status=prev_order_status,
        new_status=prev_order_status,
        metadata={
            "released_item_count": len(released_from_source),
            "cascaded_sibling_count": len(cascaded_counts),
            "cascaded_sibling_ids": [sid for sid, _ in cascaded_counts],
            "total_released_item_count": total_released,
        },
    )

    await db.commit()
    return {
        "status": order.status,
        "is_returned_to_farmer": True,
        "released_item_count": len(released_from_source),
        "cascaded_sibling_count": len(cascaded_counts),
        "total_released_item_count": total_released,
    }


@router.put("/farmer/orders/{order_id}/discard")
async def discard_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer's Discard — the fate decision that finalises a returned-
    to-farmer order (Phase 2 rework, 2026-08-14). Reached from the
    Routed card's Discard button, either directly (at natural quiescence
    with unsold items) or after Cancel (which set is_returned_to_farmer).

    Semantics:
      - Requires is_returned_to_farmer=true on the source (the order
        must be back with the farmer for a fate decision).
      - Unsold items (NOT_AVAILABLE / REJECTED) stay in place; the
        Discard doesn't need to re-flip them — they're already terminal
        per-item. The event log stamps the farmer's intent.
      - is_returned_to_farmer is cleared (the fate is now decided).
      - If NO APPROVED items exist on the source (nothing pending in
        Pickup lifecycle), the source order transitions to CANCELLED
        so the Routed card auto-removes cleanly.
      - If APPROVED items exist (physical hand-off in motion via
        Pickup pill), the source stays live — the Routed card renders
        in "Delivering" state with no buttons, Pickup card continues
        independently. Farmer's Discard has closed the reroute/discard
        question; the pickup lifecycle proceeds on its own clock.

    Rerouted items on other lineage siblings are not touched here —
    each sibling with is_returned_to_farmer=true gets its own Discard
    action from its own Routed card if needed.
    """
    order = await _get_farmer_order(db, order_id, current_user.id)
    if not order.is_returned_to_farmer:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "not_returned_to_farmer",
                "message": "This order is not currently back with you for a fate decision.",
            },
        )
    if order.status == OrderStatus.CANCELLED:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "already_cancelled",
                "message": "Order is already cancelled.",
            },
        )

    unsold_items = (await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.archived_at.is_(None),
            OrderItem.status.in_((
                OrderItemStatus.NOT_AVAILABLE,
                OrderItemStatus.REJECTED,
            )),
        )
    )).scalars().all()

    has_approved = (await db.execute(
        select(func.count(OrderItem.id)).where(
            OrderItem.order_id == order.id,
            OrderItem.status == OrderItemStatus.APPROVED,
            OrderItem.archived_at.is_(None),
        )
    )).scalar_one() or 0

    prev_order_status = (
        order.status.value if hasattr(order.status, "value") else order.status
    )
    # 2026-08-17 — Flip unsold items to SKIPPED so `returned_count`
    # (which counts NA + REJECTED) drops to 0. Without this, the farmer's
    # Routed card kept rendering Send/Discard on the next refresh — the
    # frontend's send-discard mode inference from active===0 && returned>0
    # doesn't see is_returned_to_farmer, and clicking Discard again 400s
    # because we just cleared the flag. SKIPPED = "farmer opted out";
    # per-item event log preserves the transition.
    for it in unsold_items:
        prev_item = it.status.value if hasattr(it.status, "value") else it.status
        it.status = OrderItemStatus.SKIPPED
        await _record_event(
            db, lineage_id=it.lineage_id,
            event_type="DISCARDED_BY_FARMER",
            actor_user_id=current_user.id, actor_role="FARMER",
            order_id=order.id, order_item_id=it.id,
            prev_status=prev_item,
            new_status=OrderItemStatus.SKIPPED.value,
        )
    order.is_returned_to_farmer = False
    if not has_approved:
        order.status = OrderStatus.CANCELLED

    await _record_event(
        db, lineage_id=order.id,
        event_type="DISCARDED_BY_FARMER",
        actor_user_id=current_user.id, actor_role="FARMER",
        order_id=order.id,
        prev_status=prev_order_status,
        new_status=(
            order.status.value if hasattr(order.status, "value")
            else order.status
        ),
        metadata={
            "unsold_item_count": len(unsold_items),
            "approved_items_pending_pickup": has_approved,
            "source_cancelled": not has_approved,
        },
    )

    await db.commit()
    return {
        "id": order.id,
        "status": order.status,
        "unsold_item_count": len(unsold_items),
        "approved_items_pending_pickup": has_approved,
    }


class DiscardReturnedItemsBody(BaseModel):
    # Mirror of /reroute-returned's include_postponed: when the source
    # order also has POSTPONED items still with the dealer, this flag
    # decides whether the discard sweeps them in too (True) or leaves
    # them where they are (False).
    include_postponed: bool = False


@router.put("/farmer/orders/{order_id}/discard-returned-items")
async def discard_returned_items(
    order_id: str,
    body: DiscardReturnedItemsBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer discards dealer-returned items on the source order.

    Parallel of /reroute-returned for the "set aside" verb: NA/REJECTED
    items (and optionally POSTPONED, mirroring the reroute nudge) flip
    to REROUTED so advisory dedup excludes them and the underlying
    practice comes back on the next advisory pull with an Order CTA.
    The source order itself is untouched status-wise beyond the natural
    recompute (may transition to COMPLETED if the only remaining live
    items are all APPROVED).

    Contrast with the cancel-migrate DRAFT discard endpoint above —
    that one operates on a whole fresh DRAFT the farmer holds; this
    one operates on the specific returned items still glued to the
    original dealer's order.
    """
    order = await _get_farmer_order(db, order_id, current_user.id)
    # Match the reroute-returned surface's guards: don't touch terminal
    # orders (they're history) and don't step into the facilitator's
    # queue on facilitator-owned orders.
    terminal = {OrderStatus.CANCELLED, OrderStatus.EXPIRED}
    if order.status in terminal:
        raise HTTPException(
            status_code=400,
            detail=f"Order is already {order.status.value if hasattr(order.status, 'value') else order.status}; nothing to discard.",
        )
    if order.facilitator_user_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "facilitator_owns_order",
                "message": "The facilitator is holding this order — the discard action belongs on their queue, not yours.",
            },
        )

    target_statuses = [OrderItemStatus.NOT_AVAILABLE, OrderItemStatus.REJECTED]
    if body.include_postponed:
        target_statuses.append(OrderItemStatus.POSTPONED)

    items = (await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.archived_at.is_(None),
            OrderItem.status.in_(target_statuses),
        )
    )).scalars().all()

    if not items:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "nothing_to_discard",
                "message": "No returned items on this order need discarding.",
            },
        )

    discarded_na = 0
    discarded_postponed = 0
    for it in items:
        prev_item_status = (
            it.status.value if hasattr(it.status, "value") else it.status
        )
        was_postponed = it.status == OrderItemStatus.POSTPONED
        await _record_event(
            db, lineage_id=it.lineage_id,
            event_type="DISCARDED_BY_FARMER",
            actor_user_id=current_user.id, actor_role="FARMER",
            order_id=order.id, order_item_id=it.id,
            prev_status=prev_item_status,
            new_status=OrderItemStatus.REROUTED.value,
            metadata={
                "reason": (
                    "returned_items_discard_include_postponed"
                    if was_postponed else "returned_items_discard"
                ),
            },
        )
        it.status = OrderItemStatus.REROUTED
        if was_postponed:
            discarded_postponed += 1
        else:
            discarded_na += 1

    # Recompute the order status — with returned items gone, the
    # remaining live items may all be APPROVED, in which case the
    # order should transition to COMPLETED.
    await _update_order_status(db, order.id)
    await db.commit()
    return {
        "id": order.id,
        "status": order.status,
        "discarded_returned_count": discarded_na,
        "discarded_postponed_count": discarded_postponed,
    }


@router.delete("/farmer/orders/{order_id}")
async def delete_cancelled_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer deletes a CANCELLED husk or a DRAFT order.

    Two valid call shapes:
    - CANCELLED: standard husk delete (existing behavior). Items
      sit on the husk as REROUTED historical pointers and get
      removed here.
    - DRAFT: 2026-06-09 — the farmer discards a never-sent draft
      (typically created by dealer-decline). The DRAFT carries
      PENDING items; delete them along with the order row.

    Any other status is refused. `order_item_events` rows survive
    via the FK SET NULL migration so the audit trail (lineage_id +
    reference_number) is preserved.
    """
    order = await _get_farmer_order(db, order_id, current_user.id)
    if order.status not in (OrderStatus.CANCELLED, OrderStatus.DRAFT):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "delete_not_allowed",
                "message": "Only CANCELLED husks or DRAFT orders can be deleted.",
            },
        )

    # Wipe the REROUTED husk-items so the Order row can drop without
    # tripping the order_items.order_id FK. The events table keeps
    # the lineage trail via SET NULL.
    from sqlalchemy import delete as sa_delete, update as sa_update
    await db.execute(sa_delete(OrderItem).where(OrderItem.order_id == order.id))

    # 2026-06-07 — Other orders in the same lineage may carry
    # lineage_root_id pointing at this husk's id (the cancel +
    # reroute paths now set this consistently). Null those
    # references before dropping the row; the audit chain still
    # survives via reference_number + the events table's
    # lineage_id. Self-reference also handled.
    await db.execute(
        sa_update(Order)
        .where(Order.lineage_root_id == order.id)
        .values(lineage_root_id=None)
    )

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


@router.get("/farmer/pre-sowing-available")
async def farmer_pre_sowing_available(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """True iff at least one of the farmer's subscriptions has unbooked
    DBS Pre-sowing items available right now. Drives the visibility of
    the Pre-sowing button on /orders so the farmer isn't routed to a
    page that wouldn't have anything actionable.

    Aggregates the same shape `/farmer/subscriptions/{id}/dbs-bulk-
    preview` returns — ANNUAL package + start-date window open + at
    least one remaining DBS practice for PESTICIDE or FERTILIZER.
    Short-circuits on the first match.
    """
    from datetime import date as _date
    from app.services.order_bundle import (
        resolve_dbs_practices_for_category, already_ordered_practice_ids,
    )

    subs = (await db.execute(
        select(Subscription).where(
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalars().all()
    if not subs:
        return {"available": False, "reason": "no_subscriptions"}

    today = _date.today()
    for sub in subs:
        package = (await db.execute(
            select(Package).where(Package.id == sub.package_id)
        )).scalar_one_or_none()
        if package is None:
            continue
        pkg_type = (
            package.package_type.value
            if hasattr(package.package_type, "value")
            else str(package.package_type)
        )
        if pkg_type != "ANNUAL":
            continue
        window_open = sub.crop_start_date is None or (
            (sub.crop_start_date.date()
             if hasattr(sub.crop_start_date, "date")
             else sub.crop_start_date) > today
        )
        if not window_open:
            continue
        already = await already_ordered_practice_ids(db, sub.id)
        for category in ("PESTICIDE", "FERTILIZER"):
            all_dbs = await resolve_dbs_practices_for_category(
                db, subscription=sub, category=category,
            )
            remaining = [pid for pid in all_dbs if pid not in already]
            if remaining:
                return {"available": True}
    return {"available": False, "reason": "nothing_remaining"}


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

    # 2026-06-07 — DBS-bulk is also a root creation path; generate
    # the Order ID. Lineage children downstream inherit.
    reference_number = await _generate_order_reference(db)
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
        reference_number=reference_number,
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
    # DBS-bulk endpoint also births the order in SENT (like
    # create_order); same recipient push applies.
    await _push_order_sent_recipient(db, order, current_user.name or "a farmer")
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
    # 2026-08-17 — Phase 2 unwind: returned-to-farmer orders reuse the
    # same Order row (no more Model B DRAFT); they can be non-DRAFT
    # (COMPLETED / PROCESSING / SENT_FOR_APPROVAL) with is_returned_to_
    # farmer set. Accept those too. Anchor: user hit "Only DRAFT orders
    # can be sent" trying to Send-to-another-dealer on a returned order.
    is_returned = bool(getattr(order, "is_returned_to_farmer", False))
    if order.status != OrderStatus.DRAFT and not is_returned:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "not_sendable",
                "message": "This order isn't currently available for re-routing. Cancel it first to reclaim the items.",
            },
        )

    if bool(body.dealer_user_id) == bool(body.facilitator_user_id):
        raise HTTPException(
            status_code=422,
            detail="Pick exactly one — dealer or facilitator.",
        )

    # ── Locked-brand gate (Orders V2 Batch 5) ────────────────────
    # If even one item is brand-locked, the recipient dealer must
    # be onboarded by the order's client.
    #
    # 2026-06-19 — facilitator branch removed. Same correction as
    # `POST /farmer/orders` (commit 4f7e196): the farmer CAN route
    # a locked-brand order through a facilitator; the brand-lock
    # enforces on the facilitator's onward route-to-dealer hop
    # (which carries its own brand-lock check). The dealer branch
    # of the gate stays.
    has_locked = await _order_has_locked_brand_items(db, order.id)
    if has_locked and body.dealer_user_id:
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
    # 2026-08-11 — Clear the cancel-migrate marker once the DRAFT is
    # sent so it no longer surfaces on the Returned pill. The flag is
    # only meaningful while the DRAFT is waiting on the farmer's
    # forward-or-discard decision. Also drop the released-from hint
    # and the return-reason — informational only, and the new
    # recipient replaces the "with X" context anyway.
    order.is_returned_to_farmer = False
    order.released_dealer_user_id = None
    order.released_facilitator_user_id = None
    order.return_reason = None
    # Refresh the 14-day expiry from the moment of send — the
    # original draft's clock isn't fair to a recipient who only
    # just got the order.
    order.expires_at = datetime.now(timezone.utc) + timedelta(days=14)

    # 2026-08-17 — Phase 2 unwind (returned-to-farmer path): on the
    # DRAFT flow the migrated items were already PENDING on the fresh
    # DRAFT row. On the reused-order flow the items are still NA /
    # REJECTED / SKIPPED (or POSTPONED) from whichever action landed
    # them back with the farmer. Flip everything not-in-flight back to
    # PENDING and clear the dealer-side fields so the new recipient
    # sees a fresh order. APPROVED-with-Final-Confirmed items are left
    # untouched — those keep their pickup lifecycle independent of the
    # re-route.
    if is_returned:
        resettable_statuses = {
            OrderItemStatus.NOT_AVAILABLE, OrderItemStatus.REJECTED,
            OrderItemStatus.SKIPPED, OrderItemStatus.POSTPONED,
            OrderItemStatus.NOT_NEEDED,
        }
        reset_rows = (await db.execute(
            select(OrderItem).where(
                OrderItem.order_id == order.id,
                OrderItem.archived_at.is_(None),
                OrderItem.status.in_(resettable_statuses),
            )
        )).scalars().all()
        for it in reset_rows:
            prev = it.status.value if hasattr(it.status, "value") else it.status
            it.status = OrderItemStatus.PENDING
            it.brand_cosh_id = None
            it.brand_name = None
            it.given_volume = None
            it.volume_unit = None
            it.price = None
            it.postponed_until = None
            it.scan_verified = False
            it.approval_round = None
            it.final_confirmed_at = None
            await _record_event(
                db, lineage_id=it.lineage_id,
                event_type="REROUTED_ON_SEND",
                actor_user_id=current_user.id, actor_role="FARMER",
                order_id=order.id, order_item_id=it.id,
                prev_status=prev,
                new_status=OrderItemStatus.PENDING.value,
            )

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
    await _push_order_sent_recipient(db, order, current_user.name or "a farmer")
    return {
        "status": order.status,
        "dealer_user_id": order.dealer_user_id,
        "facilitator_user_id": order.facilitator_user_id,
    }


@router.get("/farmer/orders/{order_id}/eligible-recipients")
async def list_eligible_recipients(
    order_id: str,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
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
    from app.services.training import resolve_package_client_id
    from app.modules.clients.models import Client
    order = await _get_farmer_order(db, order_id, current_user.id)
    has_locked = await _order_has_locked_brand_items(db, order.id)
    # Training subs' order.client_id is the training-child id, which
    # holds no ClientPromoter rows — resolve to parent so the picker
    # surfaces the parent's onboarded promoters.
    effective_client_id = await resolve_package_client_id(db, order.client_id)
    # Training Dealer (if any) lives on the training-child row.
    training_dealer_user_id: str | None = None
    if effective_client_id != order.client_id:
        child = (await db.execute(
            select(Client.training_dealer_user_id).where(Client.id == order.client_id)
        )).scalar_one_or_none()
        training_dealer_user_id = child
    return await _build_eligible_recipients_payload(
        db,
        current_user=current_user,
        client_id=effective_client_id,
        category=order.category,
        has_locked=has_locked,
        origin_lat=lat,
        origin_lng=lng,
        training_dealer_user_id=training_dealer_user_id,
    )


@router.get("/farmer/subscriptions/{subscription_id}/eligible-recipients-for-new-order")
async def list_eligible_recipients_for_new_order(
    subscription_id: str,
    category: str,
    practice_ids: str = "",
    lat: Optional[float] = None,
    lng: Optional[float] = None,
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

    from app.services.training import resolve_package_client_id
    from app.modules.clients.models import Client
    pids = [p for p in (practice_ids or "").split(",") if p]
    has_locked = await _practice_ids_have_locked_brand(db, pids)
    effective_client_id = await resolve_package_client_id(db, sub.client_id)
    training_dealer_user_id: str | None = None
    if effective_client_id != sub.client_id:
        training_dealer_user_id = (await db.execute(
            select(Client.training_dealer_user_id).where(Client.id == sub.client_id)
        )).scalar_one_or_none()
    return await _build_eligible_recipients_payload(
        db,
        current_user=current_user,
        client_id=effective_client_id,
        category=category,
        has_locked=has_locked,
        origin_lat=lat,
        origin_lng=lng,
        training_dealer_user_id=training_dealer_user_id,
    )


@router.get("/farmer/subscriptions/{subscription_id}/lookup-recipient")
async def lookup_recipient_for_new_order(
    subscription_id: str,
    phone: str,
    category: str,
    practice_ids: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    lang: str = Depends(get_locale),
):
    """Phone-entry lookup for the pesticide / fertiliser order picker.

    Mirrors the seed-order
    `/farmer/seed-orders/lookup-recipient` shape so the PWA can
    reuse the same `LookupCard` rendering. The brand-lock branch
    fires only when at least one of `practice_ids` is on a
    `Practice.is_brand_locked=True` row — pesticide/fertiliser
    items opt in (unlike seeds where every variety is locked).

    Eligibility rules (locked 2026-06-18 audit):
      FACILITATOR — always allowed, even when has_locked=True
        (the farmer can route locked-brand through a facilitator;
        the facilitator's onward route-to-dealer enforces the
        same dealer-onboarded check at the next hop).
      DEALER — allowed when has_locked=False; when has_locked=True,
        must be onboarded as DEALER by the subscription's client.
      Both held — DEALER takes priority when onboarded-or-not-locked;
        else falls through to FACILITATOR (permissive passthrough).
      Neither — `not_dealer_or_facilitator`, not eligible.
      Self / phone unknown — guarded same as the seed flow.

    Always returns 200 with a structured payload; `reason` carries
    the verdict.
    """
    from app.services.i18n_cosh import resolve_names_by_cosh_id
    from app.modules.auth.service import get_user_by_phone
    from app.modules.clients.models import Client, ClientPromoter

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

    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) < 10:
        return {"found": False, "reason": "phone_not_registered", "phone": phone}
    normalised = "+91" + digits[-10:]
    target = await get_user_by_phone(db, normalised)
    if target is None:
        return {"found": False, "reason": "phone_not_registered", "phone": normalised}

    if target.id == current_user.id:
        return {
            "found": True,
            "user_id": target.id,
            "phone": target.phone,
            "name": target.name,
            "can_receive": False,
            "reason": "self",
        }

    cp_rows = (await db.execute(
        select(ClientPromoter.promoter_type, ClientPromoter.client_id)
        .where(
            ClientPromoter.user_id == target.id,
            ClientPromoter.promoter_type.in_(("FACILITATOR", "DEALER")),
            ClientPromoter.status == "ACTIVE",
        )
    )).all()
    roles_held = {ptype for (ptype, _cid) in cp_rows}
    is_active = bool(cp_rows)

    company = (await db.execute(
        select(Client).where(Client.id == sub.client_id)
    )).scalar_one_or_none()
    client_name = (
        (company.display_name or company.short_name) if company else None
    )

    loc_ids = {
        cid for cid in (target.state_cosh_id, target.district_cosh_id) if cid
    }
    loc_names = await resolve_names_by_cosh_id(db, loc_ids, lang) if loc_ids else {}

    base = {
        "found": True,
        "user_id": target.id,
        "phone": target.phone,
        "name": target.name,
        "photo_url": target.photo_url,
        "state_name": loc_names.get(target.state_cosh_id) if target.state_cosh_id else None,
        "district_name": loc_names.get(target.district_cosh_id) if target.district_cosh_id else None,
        "is_active": is_active,
        "client_name": client_name,
        "has_locked_brand": has_locked,
    }

    # Role precedence. Same shape as the seed-order lookup, except
    # the DEALER onboarded-check fires only when has_locked is True
    # (regular orders without a locked brand are open to any active
    # dealer in the right licence category).
    dealer_allowed = False
    if "DEALER" in roles_held:
        if not has_locked:
            dealer_allowed = True
        elif await _is_dealer_onboarded_by_client(
            db, target.id, sub.client_id,
        ):
            dealer_allowed = True

    if dealer_allowed:
        return {**base, "role": "DEALER", "can_receive": True, "reason": "ok"}
    if "FACILITATOR" in roles_held:
        return {**base, "role": "FACILITATOR", "can_receive": True, "reason": "ok"}
    if "DEALER" in roles_held:
        # Dealer-only + has_locked + not onboarded by this client.
        return {
            **base,
            "role": "DEALER",
            "can_receive": False,
            "reason": "dealer_not_onboarded",
        }
    return {
        **base,
        "role": None,
        "can_receive": False,
        "reason": "not_dealer_or_facilitator",
    }


@router.get("/farmer/orders/{order_id}/lookup-recipient")
async def lookup_recipient_for_forward(
    order_id: str,
    phone: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    lang: str = Depends(get_locale),
):
    """Phone-entry lookup for the forward-returned-items flow.

    Same shape as `/farmer/subscriptions/{id}/lookup-recipient` so the
    PWA reuses `RecipientLookupCard`. Differs in two ways:

    1. Brand-lock is computed from the ORDER's items
       (`_order_has_locked_brand_items`) — the items being forwarded
       carry their own practice_ids; the caller doesn't need to pass
       them.
    2. Reuses the new-order role precedence (DEALER wins when
       not has_locked or onboarded; FACILITATOR is permissive
       passthrough); both roles allowed because the farmer can forward
       to either.
    """
    from app.modules.auth.service import get_user_by_phone
    from app.modules.clients.models import Client, ClientPromoter
    from app.services.i18n_cosh import resolve_names_by_cosh_id

    order = await _get_farmer_order(db, order_id, current_user.id)
    has_locked = await _order_has_locked_brand_items(db, order.id)

    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) < 10:
        return {"found": False, "reason": "phone_not_registered", "phone": phone}
    normalised = "+91" + digits[-10:]
    target = await get_user_by_phone(db, normalised)
    if target is None:
        return {"found": False, "reason": "phone_not_registered", "phone": normalised}

    if target.id == current_user.id:
        return {
            "found": True, "user_id": target.id, "phone": target.phone,
            "name": target.name, "can_receive": False, "reason": "self",
        }

    cp_rows = (await db.execute(
        select(ClientPromoter.promoter_type)
        .where(
            ClientPromoter.user_id == target.id,
            ClientPromoter.promoter_type.in_(("FACILITATOR", "DEALER")),
            ClientPromoter.status == "ACTIVE",
        )
    )).scalars().all()
    roles_held = set(cp_rows)
    is_active = bool(cp_rows)

    company = (await db.execute(
        select(Client).where(Client.id == order.client_id)
    )).scalar_one_or_none()
    client_name = (company.display_name or company.short_name) if company else None

    loc_ids = {cid for cid in (target.state_cosh_id, target.district_cosh_id) if cid}
    loc_names = await resolve_names_by_cosh_id(db, loc_ids, lang) if loc_ids else {}

    base = {
        "found": True,
        "user_id": target.id,
        "phone": target.phone,
        "name": target.name,
        "photo_url": target.photo_url,
        "state_name": loc_names.get(target.state_cosh_id) if target.state_cosh_id else None,
        "district_name": loc_names.get(target.district_cosh_id) if target.district_cosh_id else None,
        "is_active": is_active,
        "client_name": client_name,
        "has_locked_brand": has_locked,
    }

    dealer_allowed = False
    if "DEALER" in roles_held:
        if not has_locked:
            dealer_allowed = True
        elif await _is_dealer_onboarded_by_client(db, target.id, order.client_id):
            dealer_allowed = True

    if dealer_allowed:
        return {**base, "role": "DEALER", "can_receive": True, "reason": "ok"}
    if "FACILITATOR" in roles_held:
        return {**base, "role": "FACILITATOR", "can_receive": True, "reason": "ok"}
    if "DEALER" in roles_held:
        return {**base, "role": "DEALER", "can_receive": False, "reason": "dealer_not_onboarded"}
    return {**base, "role": None, "can_receive": False, "reason": "not_dealer_or_facilitator"}


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
    await _update_order_status(db, order_id)
    await db.commit()
    return {"item_id": item_id, "status": item.status}


@router.put("/farmer/orders/{order_id}/items/{item_id}/cancel-postponed")
async def cancel_postponed_item(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer cancels a postponed item ("I don't want to wait"). Flips
    POSTPONED → NOT_AVAILABLE so the item joins the Returned bucket on
    the review page, alongside dealer-side not-available items. Farmer
    can then re-route to another dealer or skip for this cycle."""
    await _get_farmer_order(db, order_id, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    res = validate_item_transition(item.status, OrderItemStatus.NOT_AVAILABLE.value, FARMER)
    if not res.allowed:
        _raise_transition(res)
    prev = item.status.value if hasattr(item.status, "value") else item.status
    item.status = OrderItemStatus.NOT_AVAILABLE
    await _record_event(
        db, lineage_id=item.lineage_id,
        event_type="POSTPONED_CANCELLED_BY_FARMER",
        actor_user_id=current_user.id, actor_role="FARMER",
        order_id=order_id, order_item_id=item.id,
        prev_status=prev, new_status=OrderItemStatus.NOT_AVAILABLE.value,
    )
    await _update_order_status(db, order_id)
    await db.commit()
    return {"item_id": item_id, "status": item.status}


@router.get("/farmer/purchased-items")
async def list_purchased_items(
    subscription_id: Optional[str] = None,
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

    # 2026-06-06 — Received tab now means "items in hand" — only
    # surfaces items the farmer has confirmed receipt of. Approved-
    # but-not-yet-confirmed items live in a "Ready to pick up" strip
    # on the same tab (driven by pickup_ready_count on the
    # subscriptions/orders endpoint).
    from app.modules.orders.models import BrandLookupCache
    q = (
        select(
            OrderItem, Order, Timeline, AdvPractice, Subscription,
            PackingList.farmer_received_at.label("received_at"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Timeline, Timeline.id == OrderItem.timeline_id)
        .join(AdvPractice, AdvPractice.id == OrderItem.practice_id)
        .join(Subscription, Subscription.id == Order.subscription_id)
        .join(PackingList, PackingList.order_id == Order.id)
        .where(
            Order.farmer_user_id == current_user.id,
            OrderItem.status == OrderItemStatus.APPROVED,
            PackingList.farmer_received_at.isnot(None),
        )
        .order_by(Order.date_from.desc())
    )
    if subscription_id:
        q = q.where(Order.subscription_id == subscription_id)
    rows = (await db.execute(q)).all()

    # 2026-06-06 — Recipient resolution. An order is sent to EITHER a
    # dealer OR a facilitator (mutually exclusive — see send_order
    # ~line 1632). Resolve both in a single batched lookup so the
    # PWA's Received card can render shop/name + phone regardless of
    # which role handled the order.
    recipient_ids: set[str] = set()
    for r in rows:
        order = r[1]
        rid = order.dealer_user_id or order.facilitator_user_id
        if rid:
            recipient_ids.add(rid)
    user_by_id: dict[str, tuple[str | None, str | None]] = {}
    shop_by_dealer_id: dict[str, str | None] = {}
    if recipient_ids:
        urows = (await db.execute(
            select(User.id, User.name, User.phone)
            .where(User.id.in_(recipient_ids))
        )).all()
        for uid, uname, uphone in urows:
            user_by_id[uid] = (uname, uphone)
        srows = (await db.execute(
            select(DealerProfile.user_id, DealerProfile.shop_name)
            .where(DealerProfile.user_id.in_(recipient_ids))
        )).all()
        for did, sname in srows:
            shop_by_dealer_id[did] = sname

    # 2026-07-05 — QR-availability check. Only surface the "Scan to
    # Verify" CTA in the PWA when the item's (client, brand) pair
    # has at least one ACTIVE ProductQRCode registered. Otherwise
    # the farmer taps Scan for a company that hasn't rolled out QR
    # yet, mismatches, and gets confused. Batched here so the whole
    # response comes in one round-trip.
    from app.modules.qr.models import ProductQRCode as _ProductQRCode
    qr_ready_pairs: set[tuple[str, str]] = set()
    pairs_to_check = {(r[4].client_id, r[0].brand_cosh_id) for r in rows if r[0].brand_cosh_id}
    if pairs_to_check:
        brand_ids_c = {b for _, b in pairs_to_check}
        client_ids_c = {c for c, _ in pairs_to_check}
        qr_check_rows = (await db.execute(
            select(_ProductQRCode.client_id, _ProductQRCode.brand_cosh_id).where(
                _ProductQRCode.status == "ACTIVE",
                _ProductQRCode.client_id.in_(client_ids_c),
                _ProductQRCode.brand_cosh_id.in_(brand_ids_c),
            ).distinct()
        )).all()
        for cid, bid in qr_check_rows:
            qr_ready_pairs.add((cid, bid))

    # Manufacturer + brand lookup batched across all rows, locale-aware.
    brand_ids = {r[0].brand_cosh_id for r in rows if r[0].brand_cosh_id}
    lang = current_user.language_code or "en"
    manufacturer_by_brand: dict[str, str | None] = {}
    brand_loc: dict[str, str | None] = {}
    if brand_ids:
        mfr_rows = (await db.execute(
            select(
                BrandLookupCache.trade_name_cosh_id,
                BrandLookupCache.trade_name,
                BrandLookupCache.trade_name_translations,
                BrandLookupCache.manufacturer_name,
                BrandLookupCache.manufacturer_translations,
            ).where(BrandLookupCache.trade_name_cosh_id.in_(brand_ids))
        )).all()
        for tn_id, tn_en, tn_tr, mfr_en, mfr_tr in mfr_rows:
            if tn_id not in manufacturer_by_brand and mfr_en:
                manufacturer_by_brand[tn_id] = pick_translation(
                    mfr_tr or {}, lang, mfr_en,
                )
            if tn_id not in brand_loc and tn_en:
                brand_loc[tn_id] = pick_translation(
                    tn_tr or {}, lang, tn_en,
                )

    out: list[dict] = []
    for item, order, tl, practice, sub, received_at in rows:
        # Recipient: dealer wins when set; facilitator otherwise.
        # shop_name only applies to dealers (DealerProfile).
        recipient_role: str | None = None
        recipient_name: str | None = None
        recipient_phone: str | None = None
        recipient_shop_name: str | None = None
        if order.dealer_user_id:
            recipient_role = "DEALER"
            uname, uphone = user_by_id.get(order.dealer_user_id, (None, None))
            recipient_name = uname
            recipient_phone = uphone
            recipient_shop_name = shop_by_dealer_id.get(order.dealer_user_id)
        elif order.facilitator_user_id:
            recipient_role = "FACILITATOR"
            uname, uphone = user_by_id.get(order.facilitator_user_id, (None, None))
            recipient_name = uname
            recipient_phone = uphone
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
            "brand_cosh_id": item.brand_cosh_id,
            "brand_name": brand_loc.get(item.brand_cosh_id) or item.brand_name,
            "manufacturer_name": manufacturer_by_brand.get(item.brand_cosh_id) if item.brand_cosh_id else None,
            "l1_type": practice.l1_type,
            "l2_type": practice.l2_type,
            "given_volume": float(item.given_volume) if item.given_volume is not None else None,
            "volume_unit": item.volume_unit,
            "price": float(item.price) if item.price is not None else None,
            "scan_verified": bool(item.scan_verified),
            "qr_available": (
                bool(item.brand_cosh_id)
                and (sub.client_id, item.brand_cosh_id) in qr_ready_pairs
            ),
            "order_id": item.order_id,
            "created_at": item.created_at,
            "timeline_name": tl.name,
            "timeline_from_type": tl.from_type.value if hasattr(tl.from_type, 'value') else str(tl.from_type),
            "timeline_from_value": int(tl.from_value),
            "timeline_to_value": int(tl.to_value),
            "application_date_from": date_from_iso,
            "application_date_to": date_to_iso,
            "frequency_days": int(practice.frequency_days) if practice.frequency_days else None,
            # 2026-06-06 — Recipient context per item so every Received
            # card matches the seed-order card shape. Role-aware:
            # dealer wins when set, facilitator fills in otherwise
            # (Order.dealer_user_id and facilitator_user_id are
            # mutually exclusive — see send_order). shop_name only
            # applies to dealers.
            "recipient_role": recipient_role,
            "recipient_name": recipient_name,
            "recipient_phone": recipient_phone,
            "recipient_shop_name": recipient_shop_name,
            "received_at": received_at.isoformat() if received_at else None,
        })
    # 2026-06-03 — Brand consolidation across timelines. A practice
    # recommended in multiple non-overlapping timelines (e.g. a foliar
    # spray repeated every 12 days) leads the dealer to commit the
    # same brand on each row. The farmer reads it as N separate items
    # of the same brand — confusing. Combine same-brand rows by
    # (brand_cosh_id || brand_name, volume_unit) into a single row
    # with summed qty + summed ₹ and the merged application window.
    # The advisory side stays per-timeline; only Received + packing
    # see the merged view. See user direction 2026-06-03 task (4).
    return consolidate_purchased_items(out)


def consolidate_purchased_items(rows: list[dict]) -> list[dict]:
    """Group purchased items by (brand_cosh_id or brand_name, volume_unit).

    Empty brand_name rows (PENDING items the farmer shouldn't see)
    pass through untouched. Rows with same brand + unit are merged —
    given_volume and price are summed; application_date_from is the
    earliest, application_date_to is the latest; underlying item /
    practice / timeline ids are kept in `merged_item_ids` for the
    PWA + audit purposes. scan_verified is true only if every merged
    row was scan-verified."""
    from collections import OrderedDict
    groups: OrderedDict[tuple, dict] = OrderedDict()
    passthrough: list[dict] = []
    for r in rows:
        brand_key = r.get("brand_cosh_id") or r.get("brand_name")
        if not brand_key:
            passthrough.append(r)
            continue
        key = (brand_key, r.get("volume_unit"))
        existing = groups.get(key)
        if existing is None:
            groups[key] = {
                **r,
                "merged_item_ids": [r["id"]],
                "merged_practice_ids": [r.get("practice_id")],
                "merged_timeline_count": 1,
            }
            continue
        existing["given_volume"] = (existing.get("given_volume") or 0) + (r.get("given_volume") or 0)
        existing["price"] = (existing.get("price") or 0) + (r.get("price") or 0)
        # Earliest from, latest to.
        if r.get("application_date_from") and (
            existing.get("application_date_from") is None
            or r["application_date_from"] < existing["application_date_from"]
        ):
            existing["application_date_from"] = r["application_date_from"]
        if r.get("application_date_to") and (
            existing.get("application_date_to") is None
            or r["application_date_to"] > existing["application_date_to"]
        ):
            existing["application_date_to"] = r["application_date_to"]
        existing["scan_verified"] = bool(existing.get("scan_verified")) and bool(r.get("scan_verified"))
        existing["merged_item_ids"].append(r["id"])
        existing["merged_practice_ids"].append(r.get("practice_id"))
        existing["merged_timeline_count"] += 1
    return list(groups.values()) + passthrough


# ── Dealer: Process orders ─────────────────────────────────────────────────────

@router.get("/dealer/orders")
async def list_dealer_orders(
    include_husks: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer's orders feed.

    2026-06-05 — Enriched with per-status item counts and
    packing-list state so the PWA can split the feed across pills
    (Pending / Postponed / With Farmer / Packing) without a per-
    order round-trip.

    2026-06-09 — Husk suppression (Batch 3 of Dealer mirroring).
    Default response excludes terminal orders (CANCELLED / EXPIRED)
    AND orders where every active item is REROUTED (audit-only
    husks left behind by a reroute). `?include_husks=true` lifts
    both filters — used by /dealer/history's Cancelled tab to
    surface the dealer's terminal sub-orders.

    `item_status_counts` is always computed off LIVE items
    (REROUTED excluded) so card numbers reflect actionable work.
    """
    from app.modules.clients.models import Client
    from app.modules.orders.models import BrandLookupCache
    from app.modules.subscriptions.models import Subscription

    await _assert_active_dealer(db, current_user.id)
    base_where = [Order.dealer_user_id == current_user.id]
    if not include_husks:
        base_where.append(Order.status.notin_([
            OrderStatus.CANCELLED, OrderStatus.EXPIRED,
        ]))
        # 2026-08-15 (Phase 2 facilitator-flow F9): drop orders that
        # have been reclaimed by farmer or facilitator. Dealer_user_id
        # may still be set on the row (for released_dealer_user_id
        # chip context on the farmer/facilitator side) but the dealer
        # has been dropped from active responsibility.
        base_where.append(Order.is_returned_to_farmer.is_(False))
        base_where.append(Order.is_returned_to_facilitator.is_(False))
    # 2026-06-28 — Soft-delete defense: join through Subscription so
    # the auto-listener filters orders on soft-deleted subscriptions
    # out of the dealer's feed.
    rows = (await db.execute(
        select(Order, User, Client)
        .join(User, User.id == Order.farmer_user_id)
        .join(Client, Client.id == Order.client_id)
        .join(Subscription, Subscription.id == Order.subscription_id)
        .where(*base_where)
        .order_by(Order.created_at.desc())
    )).all()

    order_ids = [o.id for o, _u, _c in rows]
    items_by_order: dict[str, list[OrderItem]] = {}
    if order_ids:
        item_rows = (await db.execute(
            select(OrderItem).where(
                OrderItem.order_id.in_(order_ids),
                OrderItem.archived_at.is_(None),
            )
        )).scalars().all()
        for it in item_rows:
            items_by_order.setdefault(it.order_id, []).append(it)

    # 2026-06-06 — Facilitator details for the Packing card so the
    # delivery person can call BOTH parties.
    facilitator_ids = sorted({
        o.facilitator_user_id for o, _u, _c in rows if o.facilitator_user_id
    })
    facilitator_by_id: dict[str, dict] = {}
    if facilitator_ids:
        f_rows = (await db.execute(
            select(User).where(User.id.in_(facilitator_ids))
        )).scalars().all()
        for f in f_rows:
            facilitator_by_id[f.id] = {
                "name": f.name,
                "phone": f.phone,
                # 2026-06-19 — Photo for the dealer's identify-confirm
                # avatar (parity with farmer_photo_url).
                "photo_url": f.photo_url,
            }

    # 2026-08-17 (per-batch Pickup rework) — multiple PackingList rows
    # per order, one per approval_round. Legacy rows have round=1 from
    # the backfill migration; new rounds spawn fresh rows.
    pls_by_order: dict[str, dict[int, PackingList]] = {}
    if order_ids:
        pl_rows = (await db.execute(
            select(PackingList).where(PackingList.order_id.in_(order_ids))
        )).scalars().all()
        for pl in pl_rows:
            round_key = pl.approval_round or 1
            pls_by_order.setdefault(pl.order_id, {})[round_key] = pl

    # Lazy-create a PL row per (order, round) that has APPROVED items
    # but no row yet. Ensures the dealer's Final Confirmation + Packing
    # cards render as soon as items land in a new round.
    created_any = False
    for o, _u, _c in rows:
        items = items_by_order.get(o.id, [])
        approved_rounds = {
            (i.approval_round or 1) for i in items
            if i.status == OrderItemStatus.APPROVED
        }
        existing_rounds = set(pls_by_order.get(o.id, {}).keys())
        for round_n in approved_rounds - existing_rounds:
            pl = PackingList(
                order_id=o.id,
                approval_round=round_n,
                pdf_url=None,
                packing_code=await _generate_packing_code(db),
            )
            db.add(pl)
            await db.flush()
            pls_by_order.setdefault(o.id, {})[round_n] = pl
            created_any = True
    if created_any:
        await db.commit()

    # Manufacturer lookup for all approved brand_cosh_ids.
    approved_brand_ids = {
        i.brand_cosh_id
        for items in items_by_order.values()
        for i in items
        if i.status == OrderItemStatus.APPROVED and i.brand_cosh_id
    }
    lang = current_user.language_code or "en"
    manufacturer_by_brand: dict[str, str | None] = {}
    brand_loc: dict[str, str | None] = {}
    if approved_brand_ids:
        mfr_rows = (await db.execute(
            select(
                BrandLookupCache.trade_name_cosh_id,
                BrandLookupCache.trade_name,
                BrandLookupCache.trade_name_translations,
                BrandLookupCache.manufacturer_name,
                BrandLookupCache.manufacturer_translations,
            ).where(BrandLookupCache.trade_name_cosh_id.in_(approved_brand_ids))
        )).all()
        for tn_id, tn_en, tn_tr, mfr_en, mfr_tr in mfr_rows:
            if tn_id not in manufacturer_by_brand and mfr_en:
                manufacturer_by_brand[tn_id] = pick_translation(
                    mfr_tr or {}, lang, mfr_en,
                )
            if tn_id not in brand_loc and tn_en:
                brand_loc[tn_id] = pick_translation(
                    tn_tr or {}, lang, tn_en,
                )

    out = []
    for o, u, c in rows:
        items = items_by_order.get(o.id, [])
        # 2026-06-09 — Live items only (REROUTED excluded). Counts
        # reflect actionable work; pure husks (every item REROUTED)
        # are filtered out unless include_husks=True.
        live_items = [i for i in items if i.status != OrderItemStatus.REROUTED]
        if items and not live_items and not include_husks:
            continue
        counts = {
            "pending": sum(1 for i in live_items if i.status == OrderItemStatus.PENDING),
            "available": sum(1 for i in live_items if i.status == OrderItemStatus.AVAILABLE),
            "postponed": sum(1 for i in live_items if i.status == OrderItemStatus.POSTPONED),
            "not_available": sum(1 for i in live_items if i.status == OrderItemStatus.NOT_AVAILABLE),
            "sent_for_approval": sum(1 for i in live_items if i.status == OrderItemStatus.SENT_FOR_APPROVAL),
            "approved": sum(1 for i in live_items if i.status == OrderItemStatus.APPROVED),
            "rejected": sum(1 for i in live_items if i.status == OrderItemStatus.REJECTED),
            # 2026-08-14 (Phase 2): APPROVED items split by dealer's
            # Final Confirmation. Facilitator + dealer PWAs read these
            # to distinguish "awaiting your Final Confirmation" vs
            # "ready for pickup".
            "awaiting_final_confirmation": sum(
                1 for i in live_items
                if i.status == OrderItemStatus.APPROVED and i.final_confirmed_at is None
            ),
            "final_confirmed": sum(
                1 for i in live_items
                if i.status == OrderItemStatus.APPROVED and i.final_confirmed_at is not None
            ),
        }
        # 2026-08-17 (per-batch rework) — build one entry per
        # (order, approval_round) with APPROVED items. Each entry
        # carries its own Final-Confirmation state + Pickup state so
        # the dealer's Final-Confirmation pill, Packing pill and
        # farmer's Pickup pill can render per-batch cards without any
        # cross-batch conflation.
        order_pls = pls_by_order.get(o.id, {})
        facilitator = (
            facilitator_by_id.get(o.facilitator_user_id)
            if o.facilitator_user_id else None
        )
        packing_batches: list[dict] = []
        approved_by_round: dict[int, list[OrderItem]] = {}
        for i in items:
            if i.status != OrderItemStatus.APPROVED:
                continue
            approved_by_round.setdefault(i.approval_round or 1, []).append(i)
        for round_n in sorted(approved_by_round.keys()):
            batch_items = approved_by_round[round_n]
            pl_row = order_pls.get(round_n)
            batch_pickup_role: str | None = None
            batch_pickup_name: str | None = None
            if pl_row and pl_row.picked_up_by_user_id:
                if pl_row.picked_up_by_user_id == o.farmer_user_id:
                    batch_pickup_role = "FARMER"
                    batch_pickup_name = u.name
                elif pl_row.picked_up_by_user_id == o.facilitator_user_id and facilitator:
                    batch_pickup_role = "FACILITATOR"
                    batch_pickup_name = facilitator.get("name")
            awaiting_fc = sum(1 for i in batch_items if i.final_confirmed_at is None)
            final_confirmed = sum(1 for i in batch_items if i.final_confirmed_at is not None)
            packing_batches.append({
                "approval_round": round_n,
                "packing_list_id": pl_row.id if pl_row else None,
                "packing_code": pl_row.packing_code if pl_row else None,
                "shared_at": (
                    pl_row.first_shared_at.isoformat()
                    if pl_row and pl_row.first_shared_at else None
                ),
                "picked_up_at": (
                    pl_row.picked_up_at.isoformat()
                    if pl_row and pl_row.picked_up_at else None
                ),
                "picked_up_by_role": batch_pickup_role,
                "picked_up_by_name": batch_pickup_name,
                "farmer_received_at": (
                    pl_row.farmer_received_at.isoformat()
                    if pl_row and pl_row.farmer_received_at else None
                ),
                "dealer_removed_at": (
                    pl_row.dealer_removed_at.isoformat()
                    if pl_row and pl_row.dealer_removed_at else None
                ),
                "awaiting_final_confirmation": awaiting_fc,
                "final_confirmed": final_confirmed,
                "all_final_confirmed": awaiting_fc == 0 and final_confirmed > 0,
                "items": [
                    {
                        "id": i.id,
                        "brand_name": brand_loc.get(i.brand_cosh_id) or i.brand_name,
                        "manufacturer_name": (
                            manufacturer_by_brand.get(i.brand_cosh_id)
                            if i.brand_cosh_id else None
                        ),
                        "given_volume": float(i.given_volume) if i.given_volume else None,
                        "volume_unit": i.volume_unit,
                        "price": float(i.price) if i.price else None,
                        "final_confirmed_at": (
                            i.final_confirmed_at.isoformat()
                            if i.final_confirmed_at else None
                        ),
                    }
                    for i in batch_items
                ],
            })
        out.append({
            "id": o.id, "status": o.status,
            # 2026-06-07 — Human-readable Order ID.
            "reference_number": o.reference_number,
            "farmer_user_id": o.farmer_user_id,
            "farmer_name": u.name,
            "farmer_phone": u.phone,
            "farmer_photo_url": u.photo_url,
            "farmer_gps_lat": float(u.gps_lat) if u.gps_lat is not None else None,
            "farmer_gps_lng": float(u.gps_lng) if u.gps_lng is not None else None,
            "facilitator_user_id": o.facilitator_user_id,
            "facilitator_name": facilitator.get("name") if facilitator else None,
            "facilitator_phone": facilitator.get("phone") if facilitator else None,
            "facilitator_photo_url": facilitator.get("photo_url") if facilitator else None,
            "client_id": o.client_id,
            "client_name": c.display_name or c.short_name,
            "client_is_training": bool(getattr(c, "is_training", False)),
            "category": o.category,
            "date_from": o.date_from, "date_to": o.date_to,
            "created_at": o.created_at,
            "item_status_counts": counts,
            # 2026-08-17 — Per-batch Pickup lifecycle. Primary source
            # for the new Final Confirmation + Packing + Pickup pills.
            "packing_batches": packing_batches,
            # Legacy top-level fields (kept for pages that haven't yet
            # migrated to packing_batches) — populated from the earliest
            # unresolved batch, or nulls if none. Home tile / history /
            # facilitator screens still read these. Slated for removal
            # once every consumer moves to packing_batches.
            **_legacy_packing_fields_dealer(packing_batches),
        })
    return out


def _legacy_packing_fields_farmer(batches: list[dict]) -> dict:
    """Farmer-side legacy fields: packing_code + packing_picked_up_by_
    role sourced from the earliest unresolved batch. Once every farmer
    consumer switches to packing_batches[], drop this helper.
    """
    if not batches:
        return {
            "packing_code": None,
            "packing_picked_up_by_role": None,
        }
    unresolved = [
        b for b in batches
        if b["farmer_received_at"] is None
    ]
    canonical = unresolved[0] if unresolved else batches[0]
    return {
        "packing_code": canonical["packing_code"],
        "packing_picked_up_by_role": canonical["picked_up_by_role"],
    }


def _legacy_packing_fields_dealer(batches: list[dict]) -> dict:
    """Emit the pre-per-batch top-level packing_* fields from the
    earliest unresolved batch. Consumers that haven't yet migrated to
    packing_batches see the same shape as before.
    """
    if not batches:
        return {
            "packing_items": [],
            "packing_code": None,
            "packing_list_shared_at": None,
            "packing_list_removed_at": None,
            "packing_picked_up_at": None,
            "packing_picked_up_by_role": None,
            "packing_picked_up_by_name": None,
            "packing_farmer_received_at": None,
        }
    unresolved = [
        b for b in batches
        if b["farmer_received_at"] is None and b["dealer_removed_at"] is None
    ]
    canonical = unresolved[0] if unresolved else batches[0]
    return {
        "packing_items": canonical["items"],
        "packing_code": canonical["packing_code"],
        "packing_list_shared_at": canonical["shared_at"],
        "packing_list_removed_at": canonical["dealer_removed_at"],
        "packing_picked_up_at": canonical["picked_up_at"],
        "packing_picked_up_by_role": canonical["picked_up_by_role"],
        "packing_picked_up_by_name": canonical["picked_up_by_name"],
        "packing_farmer_received_at": canonical["farmer_received_at"],
    }


@router.put("/dealer/orders/{order_id}/packing-list/remove")
async def remove_packing_list_from_dealer_view(
    order_id: str,
    approval_round: int | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer voluntarily removes a packing batch from their Packing
    pill. Doesn't delete history — just flips `dealer_removed_at` on
    the specific batch's PL row.

    2026-08-17 — approval_round scopes to a specific batch.
    """
    from datetime import datetime, timezone

    await _assert_active_dealer(db, current_user.id)
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    pl = await _ensure_packing_list(db, order_id, approval_round)
    pl.dealer_removed_at = datetime.now(timezone.utc)
    await db.commit()
    return {
        "order_id": order_id,
        "approval_round": pl.approval_round,
        "dealer_removed_at": pl.dealer_removed_at.isoformat(),
    }


@router.get("/dealer/postponed-items")
async def list_dealer_postponed_items(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cross-order list of POSTPONED items the dealer still owes a
    decision on. Dedicated surface so the dealer doesn't have to
    open each order one-by-one to find their own deferred work.

    Returns one row per item with the minimum the dealer needs to
    act: practice display name, farmer + crop context, order date
    range, postponed_until + how many days remain. Tapping a row in
    the PWA navigates to /dealer/orders/{order_id}?focus_item={item_id}
    which renders the order detail with everything else hidden.
    """
    from app.modules.advisory.models import Practice as AdvPractice
    from app.modules.subscriptions.models import Subscription
    from app.modules.clients.models import Client
    from datetime import datetime, timezone

    await _assert_active_dealer(db, current_user.id)

    rows = (await db.execute(
        select(OrderItem, Order, AdvPractice, User, Subscription, Client)
        .join(Order, Order.id == OrderItem.order_id)
        .join(AdvPractice, AdvPractice.id == OrderItem.practice_id, isouter=True)
        .join(User, User.id == Order.farmer_user_id)
        .join(Subscription, Subscription.id == Order.subscription_id)
        .join(Client, Client.id == Order.client_id)
        .where(
            Order.dealer_user_id == current_user.id,
            OrderItem.status == OrderItemStatus.POSTPONED,
            OrderItem.archived_at.is_(None),
            Order.status.notin_([OrderStatus.CANCELLED, OrderStatus.EXPIRED]),
        )
        .order_by(Order.created_at.asc(), OrderItem.postponed_until.asc().nullslast())
    )).all()

    now_utc = datetime.now(timezone.utc)
    out = []
    for item, order, practice, farmer, sub, client in rows:
        # 2026-06-05 — filter out postpones whose window has expired.
        # The auto-sweep flips them to NOT_AVAILABLE; in the brief
        # window before that fires we shouldn't ask the dealer to
        # decide on something the farmer already owns. User direction:
        # "remove that item if the duration for which it was originally
        # postponed for is over — it would have assumed the status of
        # Returned and returned to the farmer without the dealer having
        # to know about it."
        if item.postponed_until and item.postponed_until <= now_utc:
            continue
        l2 = practice.l2_type if practice else None
        display_name = (
            l2.replace("_", " ").title() if l2 else "Practice"
        )
        days_remaining = None
        if item.postponed_until:
            delta = item.postponed_until - now_utc
            days_remaining = max(0, delta.days)
        out.append({
            "item_id": item.id,
            "order_id": order.id,
            "subscription_id": order.subscription_id,
            "display_name": display_name,
            "farmer_name": farmer.name,
            "farmer_phone": farmer.phone,
            "farmer_photo_url": farmer.photo_url,
            "client_name": client.display_name or client.short_name,
            "category": order.category,
            "date_from": order.date_from.isoformat() if order.date_from else None,
            "date_to": order.date_to.isoformat() if order.date_to else None,
            "order_received_at": order.created_at.isoformat() if order.created_at else None,
            "postponed_until": item.postponed_until.isoformat() if item.postponed_until else None,
            "days_remaining": days_remaining,
            "order_status": order.status.value if hasattr(order.status, "value") else order.status,
        })
    return out


@router.put("/dealer/orders/{order_id}/items/{item_id}/available")
async def mark_item_available(
    order_id: str, item_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-07: Dealer selects brand and enters volume/price before marking available.

    Brand discipline (BL-07 audit, 2026-05-05; core_type fix 2026-06-02):
      `brand_cosh_id` is REQUIRED and MUST refer to an active row in
      `cosh_core_items` with `core_type = COSH_TRADE_NAMES_CORE`
      (= "trade_names"). The original audit hard-coded the literal
      "brand" which never existed in the Cosh schema — Cosh 2.0
      represents brand-equivalent rows as `trade_names`, matching
      what `brand_cache` and `npk_trade_names` already use. With the
      wrong literal, every dealer Save tap returned BRAND_NOT_IN_SYSTEM.
      Free-text or unknown identifiers are rejected with stable error
      codes (BRAND_REQUIRED / BRAND_NOT_IN_SYSTEM) so downstream
      analytics — brand comparisons, sale tracking, manufacturer
      reports, spelling consistency — stay reliable. The dealer's
      typed `brand_name` is ignored; the canonical English name from
      cosh translations is stored on the row instead. If a real brand
      truly isn't in the system, the dealer should use POST
      /dealer/missing-brand-reports to flag it for the CM.

    Part-aware sibling handling (Build C):
      Same Part, different Option  -> mark sibling NOT_AVAILABLE (returned to farmer)
      Same Part, same Option       -> leave alone (compound AND group; dealer fills these)
      Different Part               -> leave alone (dealer processes that Part separately)

    Falls back to flat OR-group closure if the relation_role is missing or malformed.
    """
    from app.services.relations import decode_role
    from app.modules.sync.models import CoshCoreItem
    from app.services.cosh_constants import COSH_TRADE_NAMES_CORE

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
            CoshCoreItem.core_type == COSH_TRADE_NAMES_CORE,
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
    # 2026-06-03 — Distinguish "key absent" from "key present with null":
    #   - "price" not in payload → leave item.price untouched.
    #   - "price" present as null → clear item.price (dealer removed a
    #     previously-entered price; this used to silently no-op).
    #   - "price" present as a number → overwrite.
    if "price" in data:
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
                # 2026-06-29 — Same Part, different Option → mark
                # NOT_NEEDED (was NOT_AVAILABLE before). These are the
                # OR alternatives the dealer didn't pick — semantically
                # "not needed because the chosen leg covers this," NOT
                # "returned because dealer couldn't supply." Using
                # NOT_NEEDED keeps them out of the farmer's Returned
                # bucket + reroute prompts. The dealer-side render
                # already treats both as "locked" (dimmed, read-only)
                # so the UX there is unchanged.
                # 2026-08-13 — U-turn: also collapse POSTPONED
                # siblings. Once the OR resolves to a chosen leg, a
                # sibling postpone on the other leg is meaningless
                # (nobody needs it now). Clear postponed_until too
                # so the auto-expiry sweep doesn't chase it.
                if (
                    s_coords.part == my_coords.part
                    and s_coords.option != my_coords.option
                    and sibling.status in (
                        OrderItemStatus.PENDING,
                        OrderItemStatus.POSTPONED,
                    )
                ):
                    sibling.status = OrderItemStatus.NOT_NEEDED
                    sibling.postponed_until = None
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
                        OrderItem.status.in_((
                            OrderItemStatus.PENDING,
                            OrderItemStatus.POSTPONED,
                        )),
                    )
                )
                for sibling in fb_result.scalars().all():
                    sibling.status = OrderItemStatus.NOT_NEEDED
                    sibling.postponed_until = None
    elif item.relation_id and item.relation_type == "OR":
        # No relation_role at all (legacy data) — preserve original flat OR closure
        fb_result = await db.execute(
            select(OrderItem).where(
                OrderItem.order_id == order_id,
                OrderItem.relation_id == item.relation_id,
                OrderItem.id != item.id,
                OrderItem.status.in_((
                    OrderItemStatus.PENDING,
                    OrderItemStatus.POSTPONED,
                )),
            )
        )
        for sibling in fb_result.scalars().all():
            sibling.status = OrderItemStatus.NOT_NEEDED
            sibling.postponed_until = None

    # Batch 28 — drop the draft entry now that the item is committed.
    # Whole-dict reassignment so SQLAlchemy detects the JSON change.
    order_row = (await db.execute(
        select(Order).where(Order.id == order_id)
    )).scalar_one()
    if order_row.dealer_draft and item_id in order_row.dealer_draft:
        new_draft = dict(order_row.dealer_draft)
        new_draft.pop(item_id, None)
        order_row.dealer_draft = new_draft

    # 2026-08-17 — Removed the postpone-resolve auto-submit path. Under
    # the previous behaviour each postpone-resolve auto-flipped to
    # SENT_FOR_APPROVAL with a fresh approval_round, so a dealer resolving
    # two postponed items in a row generated two separate approval rounds
    # for the farmer — cluttering the approval feed and preventing the
    # dealer from reviewing before sending. Now the item stays AVAILABLE
    # after Save; the dealer taps Submit for approval when ready, and
    # the standard batch path (submit_for_approval) stamps ONE
    # approval_round covering every AVAILABLE item on the order. The
    # Pending pill catches the order via the AVAILABLE > 0 predicate.

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
    await _update_order_status(db, order_id)
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


@router.post("/dealer/orders/{order_id}/relations/{relation_id}/parts/{part_index}/reset")
async def reset_relation_part(
    order_id: str, relation_id: str, part_index: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reset every item in a (relation, part) back to PENDING.

    Used by the per-item OR rendering on the dealer page: once the
    dealer has marked an item AVAILABLE in an OR group (which cascades
    siblings to NOT_AVAILABLE via `mark_item_available`), they can
    tap "Change selection" to re-open the whole group for decision.

    BL-10 already allows (AVAILABLE | POSTPONED | NOT_AVAILABLE) →
    PENDING for DEALER on each item, so this is a thin wrapper that
    walks the items in the part. Brand / volume / price on previously
    AVAILABLE rows are cleared so the dealer starts from a clean
    slate.

    Only valid while the order is still in PROCESSING — once the
    order has moved to SENT_FOR_APPROVAL the decisions are with the
    farmer.
    """
    from app.services.relations import decode_role

    await _assert_active_dealer(db, current_user.id)
    order = await _get_dealer_order(db, order_id, current_user.id)
    if order.status != OrderStatus.PROCESSING:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ORDER_NOT_PROCESSING",
                "message": (
                    "This order has already moved past the dealer's "
                    "decision phase. The reset is only available while "
                    "the order is being processed."
                ),
            },
        )

    items = (await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order_id,
            OrderItem.relation_id == relation_id,
        )
    )).scalars().all()
    if not items:
        raise HTTPException(status_code=404, detail="Relation not in this order")

    reset_count = 0
    for item in items:
        if not item.relation_role:
            continue
        try:
            coords = decode_role(item.relation_role)
        except ValueError:
            continue
        if coords.part != part_index:
            continue
        prev_status = item.status.value if hasattr(item.status, "value") else item.status
        # 2026-06-29 — Also reset NOT_NEEDED siblings so the OR-Part
        # change-selection flow unwinds the cascade-set NOT_NEEDED
        # rows back to PENDING.
        if prev_status not in ("AVAILABLE", "POSTPONED", "NOT_AVAILABLE", "NOT_NEEDED"):
            continue
        res = validate_item_transition(item.status, OrderItemStatus.PENDING.value, DEALER)
        if not res.allowed:
            continue
        if prev_status == "AVAILABLE":
            item.brand_cosh_id = None
            item.brand_name = None
            item.given_volume = None
            item.volume_unit = None
            item.price = None
        item.status = OrderItemStatus.PENDING
        await _record_event(
            db, lineage_id=item.lineage_id,
            event_type="RESET_TO_PENDING",
            actor_user_id=current_user.id, actor_role="DEALER",
            order_id=order_id, order_item_id=item.id,
            prev_status=prev_status, new_status=OrderItemStatus.PENDING.value,
            metadata={"relation_id": relation_id, "part_index": part_index},
        )
        reset_count += 1

    await _update_order_status(db, order_id)
    await db.commit()
    return {"relation_id": relation_id, "part_index": part_index, "reset": reset_count}


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
    await _update_order_status(db, order_id)
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
        # BL-17: DBS to=0 closes day BEFORE sowing — clamp upper bound.
        return start - timedelta(days=max(int(timeline.to_value or 0), 1))
    return None


async def _postpone_window_for_item(
    db: AsyncSession, order: Order, item: OrderItem,
) -> dict:
    """Compute max postpone days for an item per the 2026-06-07 rule:
    max = (timeline_end - today). Dealer can postpone on any day
    that is NOT the last day; postpone target may land on the last
    day, where Sell is still allowed.

    Examples (timeline ends on day 15):
      Today day 13: remaining_days=2  max_days=2  can postpone (1-2)
      Today day 14 (penultimate): remaining_days=1  max_days=1
        can postpone by exactly 1 day → target = day 15 (last day,
        Sell still valid)
      Today day 15 (last day): remaining_days=0  max_days=0  disabled

    Pre-2026-06-07 the formula used `remaining_days - 1` to reserve
    a clear day for farmer reroute if the postpone elapsed to NA;
    user direction relaxed that trade-off in favour of dealer
    flexibility on the penultimate day.
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
    max_days = max(0, remaining_days)
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
    await _update_order_status(db, order_id)
    await db.commit()
    return {"item_id": item_id, "status": item.status}


# 2026-08-14 — Final Confirmation endpoints (Phase 2 rework). The
# dealer's explicit commitment step between farmer's APPROVED
# decision and the item reaching the Pickup pill. Represents the
# moment payment / credit terms are settled and the dealer is
# ready to pack. See project_rootstalk_order_lifecycle_rework
# _2026_08_13.md for full context.
@router.put("/dealer/orders/{order_id}/items/{item_id}/final-confirm")
async def final_confirm_item(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_active_dealer(db, current_user.id)
    await _get_dealer_order(db, order_id, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    if item.status != OrderItemStatus.APPROVED:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "not_approved",
                "message": "Only farmer-approved items can be Final Confirmed.",
            },
        )
    if item.final_confirmed_at is not None:
        return {"item_id": item_id, "final_confirmed_at": item.final_confirmed_at}
    item.final_confirmed_at = datetime.now(timezone.utc)
    await _record_event(
        db, lineage_id=item.lineage_id,
        event_type="FINAL_CONFIRMED",
        actor_user_id=current_user.id, actor_role="DEALER",
        order_id=order_id, order_item_id=item.id,
        prev_status=OrderItemStatus.APPROVED.value,
        new_status=OrderItemStatus.APPROVED.value,
        metadata={"final_confirmed_at": item.final_confirmed_at.isoformat()},
    )
    await _update_order_status(db, order_id)
    await db.commit()
    return {"item_id": item_id, "final_confirmed_at": item.final_confirmed_at}


@router.put("/dealer/orders/{order_id}/final-confirm-all")
async def final_confirm_all(
    order_id: str,
    approval_round: int | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Batch stamp final_confirmed_at on every APPROVED item that's
    still awaiting Final Confirmation. Dealer's one-tap "confirm all"
    action from the Final Confirmation pill card.

    2026-08-17 — approval_round scopes to a specific batch (per-batch
    Pickup rework). Legacy calls (no param) confirm across ALL rounds.
    """
    await _assert_active_dealer(db, current_user.id)
    await _get_dealer_order(db, order_id, current_user.id)
    where_clauses = [
        OrderItem.order_id == order_id,
        OrderItem.status == OrderItemStatus.APPROVED,
        OrderItem.final_confirmed_at.is_(None),
        OrderItem.archived_at.is_(None),
    ]
    if approval_round is not None:
        where_clauses.append(
            func.coalesce(OrderItem.approval_round, 1) == approval_round
        )
    items = (await db.execute(
        select(OrderItem).where(*where_clauses)
    )).scalars().all()
    now = datetime.now(timezone.utc)
    for item in items:
        item.final_confirmed_at = now
        await _record_event(
            db, lineage_id=item.lineage_id,
            event_type="FINAL_CONFIRMED",
            actor_user_id=current_user.id, actor_role="DEALER",
            order_id=order_id, order_item_id=item.id,
            prev_status=OrderItemStatus.APPROVED.value,
            new_status=OrderItemStatus.APPROVED.value,
            metadata={"final_confirmed_at": now.isoformat(), "batch": True},
        )
    await _update_order_status(db, order_id)
    await db.commit()
    return {"count": len(items), "final_confirmed_at": now}


@router.put("/dealer/orders/{order_id}/items/{item_id}/cancel-final-confirm")
async def cancel_final_confirm_item(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer's back-out on an APPROVED-but-not-yet-Final-Confirmed
    item. Common reason: farmer's payment / credit didn't materialise
    after all. Item goes to NOT_AVAILABLE and joins the wrapper —
    surfaces on the farmer's unsold-items batch when the order goes
    quiescent."""
    await _assert_active_dealer(db, current_user.id)
    await _get_dealer_order(db, order_id, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    if item.status != OrderItemStatus.APPROVED:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "not_approved",
                "message": "Cancel-final-confirm is only available on APPROVED items.",
            },
        )
    if item.final_confirmed_at is not None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "already_final_confirmed",
                "message": "This item is already Final Confirmed and can no longer be cancelled by you — it is with the farmer for pickup.",
            },
        )
    res = validate_item_transition(item.status, OrderItemStatus.NOT_AVAILABLE.value, DEALER)
    if not res.allowed:
        _raise_transition(res)
    prev = item.status.value if hasattr(item.status, "value") else item.status
    item.status = OrderItemStatus.NOT_AVAILABLE
    await _record_event(
        db, lineage_id=item.lineage_id,
        event_type="CANCEL_FINAL_CONFIRM",
        actor_user_id=current_user.id, actor_role="DEALER",
        order_id=order_id, order_item_id=item.id,
        prev_status=prev, new_status=OrderItemStatus.NOT_AVAILABLE.value,
    )
    await _update_order_status(db, order_id)
    await db.commit()
    return {"item_id": item_id, "status": item.status}


@router.put("/dealer/orders/{order_id}/items/{item_id}/reset")
async def reset_item(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Per-item Change-selection endpoint (2026-06-26).

    Flips a single item back to PENDING and clears the brand /
    volume / price the dealer entered. Used by the "Change
    selection" button on AVAILABLE standalone or AND-member cards
    where there's no relation cascade to revert — just this one
    card's decision.

    For OR-related items (pure-OR or COMPLEX_OR), the frontend
    routes Change selection through the part-level reset endpoint
    instead, so the auto-cascaded siblings come back too.

    BL-10 already permits AVAILABLE → PENDING for DEALER; we only
    accept that source state here (a PENDING item has nothing to
    reset; POSTPONED items reset via the existing /postpone undo
    flow on the order-abort path; NOT_AVAILABLE items use the
    Change-decision flow). Guarded on order.status == PROCESSING.
    """
    await _assert_active_dealer(db, current_user.id)
    order = await _get_dealer_order(db, order_id, current_user.id)
    if order.status != OrderStatus.PROCESSING:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ORDER_NOT_PROCESSING",
                "message": (
                    "This order has already moved past the dealer's "
                    "decision phase."
                ),
            },
        )
    item = await _get_order_item(db, item_id, order_id)
    prev_status = item.status.value if hasattr(item.status, "value") else item.status
    if prev_status != "AVAILABLE":
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ITEM_NOT_AVAILABLE",
                "message": (
                    "Only AVAILABLE items can be reset via this endpoint."
                ),
            },
        )
    res = validate_item_transition(item.status, OrderItemStatus.PENDING.value, DEALER)
    if not res.allowed:
        _raise_transition(res)

    # 2026-07-13 — NPK AND-sibling detection. When the dealer picked an
    # NPK Mixed + Straight combo via /npk-select, the resulting
    # OrderItems all share `practice_id` + `relation_id` + `relation_type='AND'`.
    # "Change selection" on any one of them must reset the WHOLE group:
    # archive the other siblings and clear the AND relation off THIS
    # item so the practice re-enters the pipeline as a single PENDING
    # NPK item the dealer can re-pick from scratch. Without this the
    # farmer would see the reset Mixed + the leftover AVAILABLE
    # Straight siblings as separate items — the duplication reported
    # 2026-07-13.
    #
    # Non-NPK AND (Practice-authored) uses Practice.relation_id (FK to
    # `relations`), NOT OrderItem-level fields alone, so the discriminator
    # is: `relation_type == 'AND'` AND at least one OTHER non-archived
    # item on the same order shares practice_id. Only NPK auto-AND
    # matches that shape.
    npk_siblings: list[OrderItem] = []
    if (item.relation_type or "").upper() == "AND" and item.relation_id:
        npk_siblings = (await db.execute(
            select(OrderItem).where(
                OrderItem.order_id == order_id,
                OrderItem.practice_id == item.practice_id,
                OrderItem.id != item.id,
                OrderItem.archived_at.is_(None),
                OrderItem.relation_id == item.relation_id,
            )
        )).scalars().all()

    from datetime import datetime as _dt_reset, timezone as _tz_reset
    now_utc = _dt_reset.now(_tz_reset.utc)
    for sib in npk_siblings:
        sib.archived_at = now_utc

    item.brand_cosh_id = None
    item.brand_name = None
    item.given_volume = None
    item.volume_unit = None
    item.price = None
    item.status = OrderItemStatus.PENDING
    # When this was an NPK anchor, drop the AND stamp so the item
    # reads as a fresh standalone PENDING and the dealer's next tap
    # opens the NPK form clean.
    if npk_siblings:
        item.relation_id = None
        item.relation_type = None
        item.relation_role = None

    await _record_event(
        db, lineage_id=item.lineage_id,
        event_type="RESET_TO_PENDING",
        actor_user_id=current_user.id, actor_role="DEALER",
        order_id=order_id, order_item_id=item.id,
        prev_status=prev_status, new_status=OrderItemStatus.PENDING.value,
        metadata={
            "npk_siblings_archived": [s.id for s in npk_siblings],
        } if npk_siblings else None,
    )
    await _update_order_status(db, order_id)
    await db.commit()
    return {
        "item_id": item_id,
        "status": item.status,
        "npk_siblings_archived": [s.id for s in npk_siblings],
    }


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
    # 2026-06-03 — Every active item must have a decision before the
    # dealer can submit. Any PENDING item blocks the submit (the
    # dealer hasn't decided yet). The submit succeeds if at least one
    # item is AVAILABLE OR NOT_AVAILABLE — both are signals to the
    # farmer (available = approve flow, not_available = returned).
    # All-POSTPONED means nothing for the farmer to act on; the
    # dealer just stays in PROCESSING.
    all_active = (await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order_id,
            OrderItem.archived_at.is_(None),
            OrderItem.status.in_([
                OrderItemStatus.PENDING,
                OrderItemStatus.AVAILABLE,
                OrderItemStatus.POSTPONED,
                OrderItemStatus.NOT_AVAILABLE,
            ]),
        )
    )).scalars().all()
    pending_items = [i for i in all_active if i.status == OrderItemStatus.PENDING]
    if pending_items:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "UNDECIDED_ITEMS",
                "message": (
                    f"{len(pending_items)} item(s) still need a decision "
                    "(Available / Later / Not available)."
                ),
                "pending_count": len(pending_items),
            },
        )
    available_items = [i for i in all_active if i.status == OrderItemStatus.AVAILABLE]
    not_available_items = [i for i in all_active if i.status == OrderItemStatus.NOT_AVAILABLE]
    if not available_items and not not_available_items:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "NOTHING_TO_NOTIFY",
                "message": (
                    "All items are postponed — nothing to send to the farmer yet. "
                    "Wait for the postponed items, or change some decisions."
                ),
            },
        )

    # 2026-06-05 — Round number for the queue. First bulk submit on
    # an order gets round 1; subsequent postpone-resolves increment
    # from the max existing round on the order.
    existing_rounds = (await db.execute(
        select(OrderItem.approval_round).where(
            OrderItem.order_id == order_id,
            OrderItem.approval_round.isnot(None),
        )
    )).scalars().all()
    next_round = (max(existing_rounds) if existing_rounds else 0) + 1

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
        item.approval_round = next_round

    order.status = OrderStatus.SENT_FOR_APPROVAL
    await db.commit()

    # BL-14: push the farmer first — they're the actor whose
    # approval is required. If a facilitator is assigned, push them
    # too as a courtesy nudge. Both branches are skipped silently
    # if the target hasn't registered an fcm_token yet. 2026-07-16:
    # farmer branch added; previously only the facilitator was
    # notified, which meant direct dealer↔farmer orders (no
    # facilitator on the order) sent no push at all and the farmer
    # never knew there was something to approve.
    farmer = (await db.execute(
        select(User).where(User.id == order.farmer_user_id)
    )).scalar_one_or_none()
    if farmer and farmer.fcm_token:
        try:
            await send_fcm(
                token=farmer.fcm_token,
                title=SUBMIT_FOR_APPROVAL_FARMER_FCM_TITLE,
                body=_fmt_order_body(SUBMIT_FOR_APPROVAL_FARMER_FCM_BODY_TPL, order),
                data={
                    "type": "ORDER_AWAITING_FARMER_APPROVAL",
                    "order_id": order.id,
                    "click_action": f"/crop-detail/{order.subscription_id}/orders",
                },
            )
        except Exception as e:
            _orders_logger.error(
                f"FCM send raised unexpectedly for farmer {farmer.id}: {e}"
            )
    if order.facilitator_user_id:
        facilitator = (await db.execute(
            select(User).where(User.id == order.facilitator_user_id)
        )).scalar_one_or_none()
        if facilitator and facilitator.fcm_token:
            try:
                await send_fcm(
                    token=facilitator.fcm_token,
                    title=SUBMIT_FOR_APPROVAL_FACILITATOR_FCM_TITLE,
                    body=_fmt_order_body(SUBMIT_FOR_APPROVAL_FACILITATOR_FCM_BODY_TPL, order),
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
    """Dealer-side "Reset items" — clears the dealer's in-flight
    selections so they can start over, WITHOUT reversing the order's
    acceptance.

    Spec correction 2026-06-01: the endpoint used to flip
    `order.status = SENT` and effectively un-accept the order. The
    dealer's acceptance shouldn't be reversed — only their item picks
    are. The screen behaves like Refresh + Go Back: items reset to
    PENDING, draft map wiped, but the order stays PROCESSING (or
    whatever non-terminal status it was in).

    Item-level reset (unchanged from BL-10):
    - is_item_abortable guard preserves APPROVED / REJECTED /
      REMOVED / SKIPPED items (the farmer already acted on those).
    - All fulfilment fields cleared on resettable items so a re-pick
      starts from a clean slate.
    """
    await _assert_active_dealer(db, current_user.id)
    order = await _get_dealer_order(db, order_id, current_user.id)
    if not is_order_abortable(order.status):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ORDER_NOT_ABORTABLE",
                "message": (
                    f"Order in status '{order.status}' cannot be reset. "
                    "Reset is only valid for PROCESSING / SENT_FOR_APPROVAL / "
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

    # Wipe the per-item draft map too — re-opening the screen should
    # show a clean form.
    order.dealer_draft = {}

    # Order status intentionally unchanged: the dealer's acceptance
    # stands. Only their item-level work is rolled back.
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
    # 2026-08-17 — Per-batch rework: pick earliest round's row.
    existing = (await db.execute(
        select(PackingList)
        .where(PackingList.order_id == order_id)
        .order_by(PackingList.approval_round.asc().nulls_first())
        .limit(1)
    )).scalar_one_or_none()
    if not existing:
        pl = PackingList(
            order_id=order_id,
            approval_round=await _default_pl_round(db, order_id),
            pdf_url=f"/packing/{order_id}.pdf",
        )
        db.add(pl)
        await db.commit()
        await db.refresh(pl)
        return {"packing_list_id": pl.id, "pdf_url": pl.pdf_url}
    return {"packing_list_id": existing.id, "pdf_url": existing.pdf_url}


async def _farmer_packing_fields(
    db: AsyncSession,
    order: Order,
    pl: PackingList | None,
    approved_count: int,
    approval_round: int | None = None,
) -> dict:
    """Packing-surface fields for the farmer's review payload.
    Lazy-creates the row + code when approved_count > 0 so the
    farmer sees the Packing ID alongside the receipt-confirmation
    action. Mirrors the dealer-side pattern.

    2026-08-17 — approval_round scopes the lazy-create to a specific
    batch so the Pickup detail page (which fetches with the round
    param) gets that batch's PL row created if it doesn't yet exist.
    """
    if pl is None and approved_count > 0:
        pl = await _ensure_packing_list(db, order.id, approval_round)
        await db.commit()
    if pl is None:
        return {
            "packing_code": None,
            "packing_shared_at": None,
            "packing_picked_up_at": None,
            "packing_picked_up_by_role": None,
            "packing_farmer_received_at": None,
        }
    pickup_role: str | None = None
    if pl.picked_up_by_user_id:
        if pl.picked_up_by_user_id == order.farmer_user_id:
            pickup_role = "FARMER"
        elif pl.picked_up_by_user_id == order.facilitator_user_id:
            pickup_role = "FACILITATOR"
    return {
        "packing_code": pl.packing_code,
        "packing_shared_at": pl.first_shared_at.isoformat() if pl.first_shared_at else None,
        "packing_picked_up_at": pl.picked_up_at.isoformat() if pl.picked_up_at else None,
        "packing_picked_up_by_role": pickup_role,
        "packing_farmer_received_at": (
            pl.farmer_received_at.isoformat() if pl.farmer_received_at else None
        ),
    }


_PACKING_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


_ORDER_REFERENCE_PREFIX = "RT"


async def _generate_order_reference(db: AsyncSession) -> str:
    """Generate the next RT-YY-NNNNNN order reference for the current
    UTC year. Sequential allocation: SELECT the lexicographically-
    highest existing reference matching the (RT, year) prefix, parse
    its 6-digit suffix, return prefix + (suffix + 1). Mirrors the
    BL-15 subscription-reference pattern.

    2026-06-20 — Bug fix: pre-fix this helper only scanned
    `Order.reference_number`, completely missing
    `SeedOrderFull.reference_number`. Regular orders and seed orders
    share the SAME RT-YY-NNNNNN namespace (per the 2026-06-19 seed
    order ID parity work), so a regular-order POST could land on a
    sequence number a seed had already taken. User saw the cross-
    table collision as one dealer card grouping 5 unrelated lineages
    under "RT-26-000097" with mixed sub-order statuses + a wrong
    "Seed/Pesticide" badge depending on which table the head row
    came from. Now scans both tables and takes the max.

    Concurrency note: under concurrent order creation in the same
    year, two transactions may compute the same next number and one
    will fail at commit. Caller retries are out of scope for V1 — the
    rate is low enough that a 500 + farmer retry is acceptable. V2
    will tighten via SELECT FOR UPDATE on a counter row.
    """
    from app.modules.seed_mgmt.models import SeedOrderFull
    year = two_digit_year()
    prefix = reference_prefix(_ORDER_REFERENCE_PREFIX, year)
    last_order = (await db.execute(
        select(Order.reference_number)
        .where(Order.reference_number.like(f"{prefix}%"))
        .order_by(Order.reference_number.desc())
        .limit(1)
    )).scalar_one_or_none()
    last_seed = (await db.execute(
        select(SeedOrderFull.reference_number)
        .where(SeedOrderFull.reference_number.like(f"{prefix}%"))
        .order_by(SeedOrderFull.reference_number.desc())
        .limit(1)
    )).scalar_one_or_none()
    candidates = [r for r in (last_order, last_seed) if r]
    if candidates:
        prev_seq = max(parse_sequence(r) for r in candidates)
        next_seq = prev_seq + 1 if prev_seq >= 0 else 1
    else:
        next_seq = 1
    return format_reference(_ORDER_REFERENCE_PREFIX, year, next_seq)


async def _generate_packing_code(db: AsyncSession) -> str:
    """Generate a 6-char paper-friendly packing code with collision
    retry. The alphabet excludes 0/O/1/I/L so the code reads cleanly
    when written on paper or read aloud over the phone."""
    import secrets
    for _ in range(8):
        code = ''.join(secrets.choice(_PACKING_CODE_ALPHABET) for _ in range(6))
        exists = (await db.execute(
            select(PackingList.id).where(PackingList.packing_code == code)
        )).scalar_one_or_none()
        if exists is None:
            return code
    raise HTTPException(
        status_code=500,
        detail="Could not generate a unique packing code; retry",
    )


async def _ensure_packing_list(
    db: AsyncSession, order_id: str, approval_round: int | None = None,
) -> PackingList:
    """Lazy-get-or-create a PackingList for a specific batch of an
    order (per-round Pickup lifecycle, 2026-08-17).

    approval_round=None resolves to the earliest round with APPROVED
    items on the order, defaulting to 1 if none. This keeps legacy
    single-round callers working without changes.
    """
    if approval_round is None:
        approval_round = await _default_pl_round(db, order_id)
    pl = (await db.execute(
        select(PackingList).where(
            PackingList.order_id == order_id,
            PackingList.approval_round == approval_round,
        )
    )).scalar_one_or_none()
    if pl is None:
        pl = PackingList(
            order_id=order_id,
            approval_round=approval_round,
            pdf_url=None,
            packing_code=await _generate_packing_code(db),
        )
        db.add(pl)
        await db.flush()
    elif pl.packing_code is None:
        pl.packing_code = await _generate_packing_code(db)
    return pl


async def _default_pl_round(db: AsyncSession, order_id: str) -> int:
    """Fallback resolver — picks the earliest round that still has an
    unresolved APPROVED item on the order. If no APPROVED items exist
    (edge case: pre-approval callers), returns 1.
    """
    rows = (await db.execute(
        select(OrderItem.approval_round).where(
            OrderItem.order_id == order_id,
            OrderItem.status == OrderItemStatus.APPROVED,
            OrderItem.archived_at.is_(None),
        )
    )).scalars().all()
    rounds = [r if r is not None else 1 for r in rows]
    return min(rounds) if rounds else 1


@router.put("/dealer/orders/{order_id}/packing-list/mark-shared")
async def mark_packing_list_shared(
    order_id: str,
    approval_round: int | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer's "I've shared this list" signal — sets first_shared_at
    on first call (no-op on re-shares; the PWA shows a confirm warning
    before re-trigger). Lazy-creates the PackingList row if needed so
    a fresh order can be shared without a prior `generate` call.
    Surfaces the canonical packing_code so the dealer's UI can render
    it on the first share.

    2026-08-17 — approval_round query param scopes the action to a
    specific batch (Phase 2 per-batch Pickup). Legacy calls without the
    param resolve to the earliest APPROVED-round.
    """
    await _assert_active_dealer(db, current_user.id)
    # Make sure the dealer owns this order.
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    pl = await _ensure_packing_list(db, order_id, approval_round)
    if not pl.first_shared_at:
        pl.first_shared_at = datetime.now(timezone.utc)
    await db.commit()
    return {
        "detail": "Marked as shared",
        "first_shared_at": pl.first_shared_at,
        "packing_code": pl.packing_code,
        "approval_round": pl.approval_round,
    }


# ── Packing pickup / received tracking (2026-06-06) ───────────────────────────

@router.put("/facilitator/orders/{order_id}/packing-list/mark-picked-up")
async def facilitator_mark_packing_picked_up(
    order_id: str,
    approval_round: int | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator confirms they collected items from the dealer's
    shop. Sets picked_up_at + picked_up_by_user_id. Does NOT set
    farmer_received_at — that's the farmer's separate confirmation
    after the facilitator hand-over.

    2026-08-17 — approval_round scopes to a specific batch (per-batch
    Pickup rework).
    """
    from datetime import datetime, timezone

    order = (await db.execute(
        select(Order).where(
            Order.id == order_id,
            Order.facilitator_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found or not yours")
    pl = await _ensure_packing_list(db, order_id, approval_round)
    was_first_pickup = pl.picked_up_at is None
    if was_first_pickup:
        pl.picked_up_at = datetime.now(timezone.utc)
        pl.picked_up_by_user_id = current_user.id
    await db.commit()
    # Only push on the first pickup mark — repeat calls are idempotent
    # and shouldn't spam the farmer with duplicate notifications.
    if was_first_pickup:
        farmer = (await db.execute(
            select(User).where(User.id == order.farmer_user_id)
        )).scalar_one_or_none()
        if farmer and farmer.fcm_token:
            try:
                await send_fcm(
                    token=farmer.fcm_token,
                    title=PACKING_PICKED_UP_FARMER_FCM_TITLE,
                    body=_fmt_order_body(PACKING_PICKED_UP_FARMER_FCM_BODY_TPL, order),
                    data={
                        "type": "ORDER_PICKED_UP_BY_FACILITATOR",
                        "order_id": order.id,
                        "click_action": f"/crop-detail/{order.subscription_id}/orders",
                    },
                )
            except Exception as e:
                _orders_logger.error(
                    f"FCM send raised unexpectedly for farmer {farmer.id}: {e}"
                )
    return {
        "order_id": order_id,
        "picked_up_at": pl.picked_up_at.isoformat() if pl.picked_up_at else None,
        "picked_up_by_user_id": pl.picked_up_by_user_id,
        "farmer_received_at": pl.farmer_received_at.isoformat() if pl.farmer_received_at else None,
    }


@router.put("/farmer/orders/{order_id}/packing-list/mark-received")
async def farmer_mark_packing_received(
    order_id: str,
    approval_round: int | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer confirms they have the items in hand. If no pickup has
    been recorded yet, the farmer's tap counts as the pickup too
    (auto-pickup for direct dealer-to-farmer handovers — no
    facilitator in the loop). If a facilitator already marked
    pickup, this just stamps the final farmer_received_at.

    2026-08-17 — approval_round scopes to a specific batch. Farmer's
    Pickup pill lists one card per unreceived Final-Confirmed batch;
    tap → PUT with that round. Legacy calls (no param) mark the
    earliest APPROVED-round.
    """
    from datetime import datetime, timezone

    order = (await db.execute(
        select(Order).where(
            Order.id == order_id,
            Order.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found or not yours")
    pl = await _ensure_packing_list(db, order_id, approval_round)
    now_utc = datetime.now(timezone.utc)
    was_first_receive = pl.farmer_received_at is None
    if pl.picked_up_at is None:
        pl.picked_up_at = now_utc
        pl.picked_up_by_user_id = current_user.id
    if was_first_receive:
        pl.farmer_received_at = now_utc
    await db.commit()
    # Close the loop by pushing the dealer (and facilitator if there
    # is one). Idempotent on farmer_received_at so re-taps don't spam.
    if was_first_receive:
        recipient_user_ids = [uid for uid in (order.dealer_user_id, order.facilitator_user_id) if uid]
        if recipient_user_ids:
            recipients = (await db.execute(
                select(User).where(User.id.in_(recipient_user_ids))
            )).scalars().all()
            for u in recipients:
                if not u.fcm_token:
                    continue
                try:
                    await send_fcm(
                        token=u.fcm_token,
                        title=PACKING_RECEIVED_DEALER_FCM_TITLE,
                        body=_fmt_order_body(PACKING_RECEIVED_DEALER_FCM_BODY_TPL, order),
                        data={
                            "type": "ORDER_RECEIVED_BY_FARMER",
                            "order_id": order.id,
                        },
                    )
                except Exception as e:
                    _orders_logger.error(
                        f"FCM send raised unexpectedly for user {u.id}: {e}"
                    )
    return {
        "order_id": order_id,
        "picked_up_at": pl.picked_up_at.isoformat() if pl.picked_up_at else None,
        "picked_up_by_user_id": pl.picked_up_by_user_id,
        "farmer_received_at": pl.farmer_received_at.isoformat() if pl.farmer_received_at else None,
    }


# ── Facilitator: Route and handle orders ──────────────────────────────────────

@router.get("/facilitator/orders")
async def list_facilitator_orders(
    status_filter: Optional[str] = None,
    include_husks: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Orders routed to this facilitator for handling.

    2026-06-06 — Enriched with per-status item counts so the
    facilitator's Manage card can surface the same "N returned items"
    strip the farmer's Manage tab shows for direct-to-dealer orders.
    Spec: returned items belong to the facilitator (not the farmer)
    while the facilitator owns the order. Farmer side will hide its
    own returned strip when `o.facilitator_user_id` is set.

    2026-06-07 — Husk suppression. After the facilitator forwards
    returned items to a new dealer (reroute) or hands them back to
    the farmer (return-to-farmer), the source order's items become
    REROUTED — audit-only pointers, no live work. Orders whose
    EVERY active item is REROUTED are filtered out of the default
    response so the facilitator's queue isn't cluttered with
    historical husks. `?include_husks=true` opts in (audit
    deep-dive). Mixed orders (some REROUTED + some live) still
    surface because live work remains.

    All counts (`item_count`, `item_status_counts`) compute off
    live items only — REROUTED rows are excluded from both so the
    card numbers reflect the actionable work, not the history.
    """
    await _assert_active_facilitator(db, current_user.id)
    # 2026-06-28 — Soft-delete defense: join through Subscription so
    # the listener filters orders on soft-deleted subscriptions out
    # of the facilitator's queue.
    from app.modules.subscriptions.models import Subscription
    q = (
        select(Order)
        .join(Subscription, Subscription.id == Order.subscription_id)
        .where(Order.facilitator_user_id == current_user.id)
        # 2026-08-15 (Phase 2 facilitator-flow F7): drop orders that
        # have been reclaimed by the farmer (farmer Cancel or
        # facilitator's own Return-to-farmer). facilitator_user_id
        # stays NULL on those anyway after F1/F3 clear it — but the
        # explicit is_returned_to_farmer check is defence-in-depth
        # for any code path that flips the flag without clearing FKs.
        .where(Order.is_returned_to_farmer.is_(False))
        .order_by(Order.created_at.desc())
    )
    if status_filter:
        q = q.where(Order.status == status_filter)
    result = await db.execute(q)
    orders = result.scalars().all()

    farmer_ids = {o.farmer_user_id for o in orders}
    dealer_ids = {o.dealer_user_id for o in orders if o.dealer_user_id}
    user_by_id: dict[str, User] = {}
    if farmer_ids or dealer_ids:
        ids = list(farmer_ids | dealer_ids)
        urows = (await db.execute(
            select(User).where(User.id.in_(ids))
        )).scalars().all()
        user_by_id = {u.id: u for u in urows}
    # 2026-06-07 — Dealer profile bundle: shop name + address + GPS
    # so the facilitator card can render the full dealer contact
    # block (call + maps link) per user spec for the Routed/Returned/
    # With Farmer card bodies.
    dealer_profile_by_id: dict[str, dict] = {}
    if dealer_ids:
        prows = (await db.execute(
            select(
                DealerProfile.user_id,
                DealerProfile.shop_name,
                DealerProfile.shop_address,
                DealerProfile.shop_gps_lat,
                DealerProfile.shop_gps_lng,
            ).where(DealerProfile.user_id.in_(dealer_ids))
        )).all()
        for did, sname, saddr, slat, slng in prows:
            dealer_profile_by_id[did] = {
                "shop_name": sname,
                "shop_address": saddr,
                "shop_gps_lat": float(slat) if slat is not None else None,
                "shop_gps_lng": float(slng) if slng is not None else None,
            }

    # 2026-06-07 — Crop name per order via subscription → package →
    # CoshCoreItem.translations. Batched lookups (one query per FK).
    sub_ids = {o.subscription_id for o in orders if o.subscription_id}
    sub_by_id: dict[str, Subscription] = {}
    if sub_ids:
        srows = (await db.execute(
            select(Subscription).where(Subscription.id.in_(sub_ids))
        )).scalars().all()
        sub_by_id = {s.id: s for s in srows}
    pkg_ids = {s.package_id for s in sub_by_id.values() if s.package_id}
    pkg_by_id: dict[str, Package] = {}
    if pkg_ids:
        prows = (await db.execute(
            select(Package).where(Package.id.in_(pkg_ids))
        )).scalars().all()
        pkg_by_id = {p.id: p for p in prows}
    crop_cosh_ids = {p.crop_cosh_id for p in pkg_by_id.values() if p.crop_cosh_id}
    crop_name_by_cosh_id: dict[str, str] = {}
    if crop_cosh_ids:
        from app.modules.sync.models import CoshCoreItem
        crows = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(crop_cosh_ids))
        )).all()
        lang = current_user.language_code or "en"
        for cid, tr in crows:
            if isinstance(tr, dict):
                name = pick_translation(tr, lang, "")
                if name:
                    crop_name_by_cosh_id[cid] = name

    # 2026-06-21 — Company name per client_id so the facilitator card
    # can identify the order by farmer + crop + company (parity with
    # dealer card; matches the BL-tier user-facing identifier rule —
    # see `feedback_se_internal_labels_hidden.md`).
    client_ids = {o.client_id for o in orders if o.client_id}
    client_name_by_id: dict[str, str] = {}
    client_is_training_by_id: dict[str, bool] = {}
    if client_ids:
        from app.modules.clients.models import Client
        clrows = (await db.execute(
            select(Client.id, Client.display_name, Client.short_name, Client.is_training)
            .where(Client.id.in_(client_ids))
        )).all()
        for cid, dname, sname, is_training in clrows:
            client_name_by_id[cid] = dname or sname or ""
            client_is_training_by_id[cid] = bool(is_training)

    # 2026-08-17 (per-batch rework) — multiple PL rows per order keyed
    # on approval_round. Facilitator's Pickup pill picks up the earliest
    # not-yet-received shared batch; card fields below pull from that
    # canonical batch. Full per-batch list is available on the order
    # detail response for facilitators who want granular actions.
    order_ids = [o.id for o in orders]
    pls_by_order: dict[str, list[PackingList]] = {}
    if order_ids:
        plrows = (await db.execute(
            select(PackingList).where(PackingList.order_id.in_(order_ids))
        )).scalars().all()
        for pl in plrows:
            pls_by_order.setdefault(pl.order_id, []).append(pl)

    def _canonical_pl(oid: str) -> PackingList | None:
        rows = pls_by_order.get(oid, [])
        if not rows:
            return None
        # Prefer earliest unresolved (shared but not received) batch;
        # fall back to the earliest round overall.
        unresolved = [
            pl for pl in rows
            if pl.first_shared_at is not None and pl.farmer_received_at is None
        ]
        pool = unresolved if unresolved else rows
        return sorted(pool, key=lambda p: (p.approval_round or 1))[0]

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
        # 2026-06-07 — Live items = non-archived AND non-REROUTED.
        # Husk = order with zero live items (every item migrated away
        # to a new lineage child). Filter unless include_husks=true.
        live_items = [
            i for i in items if i.status != OrderItemStatus.REROUTED
        ]
        if not live_items and not include_husks:
            continue
        farmer = user_by_id.get(o.farmer_user_id)
        dealer = user_by_id.get(o.dealer_user_id) if o.dealer_user_id else None
        dealer_prof = dealer_profile_by_id.get(o.dealer_user_id) if o.dealer_user_id else None
        sub = sub_by_id.get(o.subscription_id) if o.subscription_id else None
        pkg = pkg_by_id.get(sub.package_id) if (sub and sub.package_id) else None
        crop_name = (
            crop_name_by_cosh_id.get(pkg.crop_cosh_id)
            if (pkg and pkg.crop_cosh_id) else None
        )
        counts = {
            "pending": sum(1 for i in live_items if i.status == OrderItemStatus.PENDING),
            "available": sum(1 for i in live_items if i.status == OrderItemStatus.AVAILABLE),
            "postponed": sum(1 for i in live_items if i.status == OrderItemStatus.POSTPONED),
            "not_available": sum(1 for i in live_items if i.status == OrderItemStatus.NOT_AVAILABLE),
            "sent_for_approval": sum(1 for i in live_items if i.status == OrderItemStatus.SENT_FOR_APPROVAL),
            "approved": sum(1 for i in live_items if i.status == OrderItemStatus.APPROVED),
            "rejected": sum(1 for i in live_items if i.status == OrderItemStatus.REJECTED),
            # 2026-08-14 (Phase 2): APPROVED items split by dealer's
            # Final Confirmation. Facilitator + dealer PWAs read these
            # to distinguish "awaiting your Final Confirmation" vs
            # "ready for pickup".
            "awaiting_final_confirmation": sum(
                1 for i in live_items
                if i.status == OrderItemStatus.APPROVED and i.final_confirmed_at is None
            ),
            "final_confirmed": sum(
                1 for i in live_items
                if i.status == OrderItemStatus.APPROVED and i.final_confirmed_at is not None
            ),
        }
        pl = _canonical_pl(o.id)
        # 2026-06-22 — Inline list of items the facilitator picks up
        # (only APPROVED items count). User wants brand + qty + price
        # for cross-check at the dealer's shop. Mirrors the
        # `packing_items` shape /dealer/orders ships for its Packing
        # pill, minus the locale-aware brand cache lookup (using the
        # raw OrderItem.brand_name is fine here — the facilitator
        # cross-checks the printed label, not a translation).
        packing_items: list[dict] = []
        if counts["approved"] > 0:
            for i in live_items:
                if i.status != OrderItemStatus.APPROVED:
                    continue
                packing_items.append({
                    "id": i.id,
                    "brand_name": i.brand_name,
                    "given_volume": float(i.given_volume) if i.given_volume else None,
                    "volume_unit": i.volume_unit,
                    "price": float(i.price) if i.price else None,
                })
        # 2026-08-17 (per-batch rework) — assemble packing_batches
        # analogous to the dealer + farmer serializers. Facilitator
        # PWA iterates these to render one Pickup card per batch (own
        # packing code, own pick-up state, own hand-off progress).
        order_pls_fac = pls_by_order.get(o.id, [])
        pls_by_round_fac = {(pl_.approval_round or 1): pl_ for pl_ in order_pls_fac}
        approved_by_round_fac: dict[int, list[OrderItem]] = {}
        for i in live_items:
            if i.status == OrderItemStatus.APPROVED:
                approved_by_round_fac.setdefault(i.approval_round or 1, []).append(i)
        packing_batches: list[dict] = []
        for round_n in sorted(approved_by_round_fac.keys()):
            batch_items = approved_by_round_fac[round_n]
            pl_row = pls_by_round_fac.get(round_n)
            awaiting_fc = sum(1 for i in batch_items if i.final_confirmed_at is None)
            final_confirmed = sum(1 for i in batch_items if i.final_confirmed_at is not None)
            batch_pickup_role = None
            if pl_row and pl_row.picked_up_by_user_id and pl_row.picked_up_at:
                if pl_row.picked_up_by_user_id == o.facilitator_user_id:
                    batch_pickup_role = "FACILITATOR"
                elif pl_row.picked_up_by_user_id == o.farmer_user_id:
                    batch_pickup_role = "FARMER"
            packing_batches.append({
                "approval_round": round_n,
                "packing_list_id": pl_row.id if pl_row else None,
                "packing_code": pl_row.packing_code if pl_row else None,
                "shared_at": (
                    pl_row.first_shared_at.isoformat()
                    if pl_row and pl_row.first_shared_at else None
                ),
                "picked_up_at": (
                    pl_row.picked_up_at.isoformat()
                    if pl_row and pl_row.picked_up_at else None
                ),
                "picked_up_by_role": batch_pickup_role,
                "farmer_received_at": (
                    pl_row.farmer_received_at.isoformat()
                    if pl_row and pl_row.farmer_received_at else None
                ),
                "awaiting_final_confirmation": awaiting_fc,
                "final_confirmed": final_confirmed,
                "all_final_confirmed": awaiting_fc == 0 and final_confirmed > 0,
                "items": [
                    {
                        "id": i.id,
                        "brand_name": i.brand_name,
                        "given_volume": float(i.given_volume) if i.given_volume else None,
                        "volume_unit": i.volume_unit,
                        "price": float(i.price) if i.price else None,
                    }
                    for i in batch_items
                ],
            })
        out.append({
            "id": o.id, "status": o.status,
            # 2026-06-07 — Order ID.
            "reference_number": o.reference_number,
            # 2026-06-19 — `category` drives the PWA's
            # confirm-forward-to-dealer sheet's inputType label.
            "category": o.category,
            "farmer_user_id": o.farmer_user_id, "client_id": o.client_id,
            "dealer_user_id": o.dealer_user_id,
            "date_from": o.date_from, "date_to": o.date_to,
            "created_at": o.created_at,
            "item_count": len(live_items),
            "pending_count": counts["pending"],
            # Per-status counts so the PWA can render strips for
            # returned / awaiting-approval without a per-id round-trip.
            "item_status_counts": counts,
            # Farmer + dealer contact so the facilitator card can
            # render the chain without a /admin/users lookup.
            "farmer_name": farmer.name if farmer else None,
            "farmer_phone": farmer.phone if farmer else None,
            "farmer_photo_url": farmer.photo_url if farmer else None,
            "dealer_name": dealer.name if dealer else None,
            "dealer_phone": dealer.phone if dealer else None,
            "dealer_shop_name": dealer_prof.get("shop_name") if dealer_prof else None,
            # 2026-06-07 — full dealer location for Routed/Returned/
            # With-Farmer card bodies (call + address + maps link).
            "dealer_shop_address": dealer_prof.get("shop_address") if dealer_prof else None,
            "dealer_shop_gps_lat": dealer_prof.get("shop_gps_lat") if dealer_prof else None,
            "dealer_shop_gps_lng": dealer_prof.get("shop_gps_lng") if dealer_prof else None,
            # 2026-06-07 — Crop name + subscription_id for the card
            # header (per facilitator card spec).
            "crop_name": crop_name,
            "subscription_id": o.subscription_id,
            # 2026-06-21 — Company name so the facilitator card can
            # show farmer + crop + company (parity with dealer card).
            "client_name": client_name_by_id.get(o.client_id),
            "client_is_training": client_is_training_by_id.get(o.client_id, False),
            # 2026-06-06 — Packing fields drive the Pickup pill
            # (approved items the facilitator hasn't picked up yet)
            # and the Completed pill (farmer-confirmed receipt).
            # 2026-06-21 — Added packing_list_shared_at so /facilitator/orders
            # can render a Pickup pill (only shared lists are pickup-ready).
            "packing_code": pl.packing_code if pl else None,
            "packing_list_shared_at": (
                pl.first_shared_at.isoformat() if pl and pl.first_shared_at else None
            ),
            "packing_picked_up_at": (
                pl.picked_up_at.isoformat() if pl and pl.picked_up_at else None
            ),
            "packing_farmer_received_at": (
                pl.farmer_received_at.isoformat() if pl and pl.farmer_received_at else None
            ),
            "packing_items": packing_items,
            # 2026-08-17 — Per-batch pickup lifecycle. Facilitator PWA
            # iterates this to render one Pickup card per batch (each
            # with its own code + pick-up state).
            "packing_batches": packing_batches,
            # 2026-08-12 — Facilitator-side returned marker so subBelongsTo
            # can route dealer-declined orders to Returned pill (not
            # lumped with fresh "needs accept" on Pending). Cleared on
            # /route-to-dealer send.
            "is_returned_to_facilitator": bool(getattr(o, "is_returned_to_facilitator", False)),
            "released_dealer_user_id": getattr(o, "released_dealer_user_id", None),
        })
    return out


@router.put("/facilitator/orders/{order_id}/route-to-dealer")
async def route_order_to_dealer(
    order_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator assigns a dealer to handle a specific order.

    2026-06-09 — Status set to SENT (not PROCESSING). The dealer
    sees the order in their Pending pill with the Accept / Decline
    buttons, identical to a direct farmer→dealer order. Earlier
    code flipped straight to PROCESSING, robbing the dealer of the
    chance to decline (per user direction 2026-06-09 Issue 1).
    """
    await _assert_active_facilitator(db, current_user.id)
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.facilitator_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found or not assigned to you")
    if order.status not in [OrderStatus.SENT, OrderStatus.ACCEPTED]:
        raise HTTPException(status_code=400, detail="Order cannot be routed in current status")
    dealer_user_id = data.get("dealer_user_id")
    if not dealer_user_id:
        raise HTTPException(status_code=422, detail="dealer_user_id required")
    # 2026-06-06 — Spec: facilitators can only forward to dealers,
    # never to another facilitator. Guard recipient is an active
    # dealer (the picker UI is dealer-only, but the endpoint took
    # any user id before this check landed).
    await _assert_active_dealer(db, dealer_user_id)
    # 2026-06-18 — Brand-lock guard on the facilitator's onward hop.
    # Mirror of the farmer→dealer write-time check at line 1592 and
    # the same rule the seed-flow `/facilitator/seed-orders/{id}/
    # route-to-dealer` enforces. Audit had this flagged as a sibling
    # gap when Point 3c landed for seeds.
    if await _order_has_locked_brand_items(db, order.id):
        if not await _is_dealer_onboarded_by_client(
            db, dealer_user_id, order.client_id,
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "locked_brand_requires_onboarded_dealer",
                    "message": "This order has a brand-locked item. It can only be forwarded to a dealer onboarded by the company.",
                },
            )
    prev_status = order.status.value if hasattr(order.status, "value") else order.status
    order.dealer_user_id = dealer_user_id
    # 2026-06-09 — SENT so the dealer can Accept / Decline. The
    # facilitator handed off the routing decision; the dealer still
    # owns the commit-to-process decision.
    order.status = OrderStatus.SENT
    # 2026-08-12 — Clear the returned-to-facilitator marker on forward:
    # the previous dealer's decline is history, this is a fresh routing.
    order.is_returned_to_facilitator = False
    order.released_dealer_user_id = None
    await _record_event(
        db, lineage_id=order.id,
        event_type="ROUTED_TO_DEALER",
        actor_user_id=current_user.id, actor_role="FACILITATOR",
        order_id=order.id,
        prev_status=prev_status, new_status=OrderStatus.SENT.value,
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
    # 2026-06-06 — Packing fields for the facilitator's pickup tap.
    approved_count = sum(1 for i in items if i.status == OrderItemStatus.APPROVED)
    # 2026-08-17 — Per-batch rework: load ALL PL rows so we can emit
    # packing_batches[] alongside the legacy single-slot packing_*
    # (canonical = earliest round, kept for pages that haven't yet
    # migrated).
    all_pls = (await db.execute(
        select(PackingList).where(PackingList.order_id == order.id)
    )).scalars().all()
    pls_by_round_detail = {(pl_.approval_round or 1): pl_ for pl_ in all_pls}
    pl = (
        sorted(all_pls, key=lambda p: (p.approval_round or 1))[0]
        if all_pls else None
    )
    packing_fields = await _farmer_packing_fields(db, order, pl, approved_count)

    # Per-batch payload matching the /facilitator/orders list shape.
    approved_by_round_det: dict[int, list[OrderItem]] = {}
    for i in items:
        if i.status == OrderItemStatus.APPROVED:
            approved_by_round_det.setdefault(i.approval_round or 1, []).append(i)
    packing_batches_detail: list[dict] = []
    for round_n in sorted(approved_by_round_det.keys()):
        batch_items = approved_by_round_det[round_n]
        pl_row = pls_by_round_detail.get(round_n)
        awaiting_fc = sum(1 for i in batch_items if i.final_confirmed_at is None)
        final_confirmed = sum(1 for i in batch_items if i.final_confirmed_at is not None)
        batch_pickup_role = None
        if pl_row and pl_row.picked_up_by_user_id and pl_row.picked_up_at:
            if pl_row.picked_up_by_user_id == order.facilitator_user_id:
                batch_pickup_role = "FACILITATOR"
            elif pl_row.picked_up_by_user_id == order.farmer_user_id:
                batch_pickup_role = "FARMER"
        packing_batches_detail.append({
            "approval_round": round_n,
            "packing_list_id": pl_row.id if pl_row else None,
            "packing_code": pl_row.packing_code if pl_row else None,
            "shared_at": (
                pl_row.first_shared_at.isoformat()
                if pl_row and pl_row.first_shared_at else None
            ),
            "picked_up_at": (
                pl_row.picked_up_at.isoformat()
                if pl_row and pl_row.picked_up_at else None
            ),
            "picked_up_by_role": batch_pickup_role,
            "farmer_received_at": (
                pl_row.farmer_received_at.isoformat()
                if pl_row and pl_row.farmer_received_at else None
            ),
            "awaiting_final_confirmation": awaiting_fc,
            "final_confirmed": final_confirmed,
            "all_final_confirmed": awaiting_fc == 0 and final_confirmed > 0,
            "items": [
                {
                    "id": i.id,
                    "brand_name": i.brand_name,
                    "given_volume": float(i.given_volume) if i.given_volume else None,
                    "volume_unit": i.volume_unit,
                    "price": float(i.price) if i.price else None,
                }
                for i in batch_items
            ],
        })

    # 2026-06-30 — Affected-plants count per approved item. Mirrors the
    # dealer endpoint's resolution so the facilitator sees the same
    # "Treatment for N of M palms" context on the items they're about
    # to pick up.
    sub_fac = (await db.execute(
        select(Subscription).where(Subscription.id == order.subscription_id)
    )).scalar_one_or_none()
    fac_number_of_plants = (
        int(sub_fac.number_of_plants)
        if sub_fac and sub_fac.number_of_plants else None
    )
    affected_count_by_item: dict[str, Optional[int]] = {}
    if fac_number_of_plants:
        from app.modules.subscriptions.models import TriggeredCHAEntry
        item_tl_ids = [i.timeline_id for i in items if i.timeline_id]
        timelines_fac: dict[str, Timeline] = {}
        if item_tl_ids:
            tlrows = (await db.execute(
                select(Timeline).where(Timeline.id.in_(item_tl_ids))
            )).scalars().all()
            timelines_fac = {t.id: t for t in tlrows}
        tl_rec_ids: set[str] = set()
        for _t in timelines_fac.values():
            r = (
                _t.sp_recommendation_id
                or _t.pg_recommendation_id
                or _t.standard_response_id
            )
            if r:
                tl_rec_ids.add(r)
        latest_count_by_rec: dict[str, Optional[int]] = {}
        if tl_rec_ids:
            crows = (await db.execute(
                select(TriggeredCHAEntry).where(
                    TriggeredCHAEntry.subscription_id == sub_fac.id,
                    TriggeredCHAEntry.recommendation_id.in_(tl_rec_ids),
                    TriggeredCHAEntry.status == "ACTIVE",
                ).order_by(TriggeredCHAEntry.triggered_at.desc())
            )).scalars().all()
            for cr in crows:
                if cr.recommendation_id not in latest_count_by_rec:
                    latest_count_by_rec[cr.recommendation_id] = cr.affected_plants_count
        for it in items:
            tl = timelines_fac.get(it.timeline_id) if it.timeline_id else None
            if tl is None:
                continue
            rec = (
                tl.sp_recommendation_id
                or tl.pg_recommendation_id
                or tl.standard_response_id
            )
            if rec:
                affected_count_by_item[it.id] = latest_count_by_rec.get(rec)
            else:
                affected_count_by_item[it.id] = fac_number_of_plants

    return {
        "id": order.id, "status": order.status,
        "reference_number": order.reference_number,
        # 2026-06-19 — `category` drives the confirm-forward-to-dealer
        # sheet on the PWA ("Do you wish to send the {inputType} Order
        # to {dealer}?"). PESTICIDE / FERTILIZER on regular orders.
        "category": order.category,
        "farmer_user_id": order.farmer_user_id, "client_id": order.client_id,
        "dealer_user_id": order.dealer_user_id,
        "date_from": order.date_from, "date_to": order.date_to,
        "created_at": order.created_at,
        "number_of_plants": fac_number_of_plants,
        # 2026-06-07 — Anti-manipulation rule: facilitator sees per-
        # item brand / qty / cost ONLY for items in APPROVED status
        # (post farmer-approval, needed for the pickup at the
        # dealer). All other statuses (PENDING / AVAILABLE / POSTPONED
        # / NOT_AVAILABLE / SENT_FOR_APPROVAL / REJECTED) ship as
        # count-only — practice_id + status, brand/qty/cost null.
        "items": [
            {
                "id": i.id,
                "practice_id": i.practice_id,
                "status": i.status,
                "brand_cosh_id": (
                    i.brand_cosh_id if i.status == OrderItemStatus.APPROVED else None
                ),
                "brand_name": (
                    i.brand_name if i.status == OrderItemStatus.APPROVED else None
                ),
                "given_volume": (
                    float(i.given_volume)
                    if i.status == OrderItemStatus.APPROVED and i.given_volume
                    else None
                ),
                "volume_unit": (
                    i.volume_unit if i.status == OrderItemStatus.APPROVED else None
                ),
                "price": (
                    float(i.price)
                    if i.status == OrderItemStatus.APPROVED and i.price
                    else None
                ),
                # 2026-06-30 — Affected-plants count for plant-wise
                # context. Surfaces only on APPROVED items to stay
                # consistent with the anti-manipulation rule.
                "affected_plants_count": (
                    affected_count_by_item.get(i.id)
                    if i.status == OrderItemStatus.APPROVED else None
                ),
            }
            for i in items
        ],
        **packing_fields,
        # 2026-08-17 — Per-batch pickup lifecycle for the facilitator's
        # order-detail page. Iterate to render one pickup card per batch.
        "packing_batches": packing_batches_detail,
    }


@router.get("/facilitator/pickup")
async def list_facilitator_pickup(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """For Pickup — the facilitator's separate basket of approved
    items waiting to be picked up from the dealer + handed off to
    the farmer.

    Spec (2026-06-07): persists past timeline expiry — the ONE
    exception to the rule that items disappear from active surfaces
    when their timeline window closes. Reasoning: once items are
    APPROVED, the dealer is holding them; the facilitator still has
    to physically collect them regardless of advisory window state.

    Card surface per Order ID (the PWA groups client-side):
    - Packing ID (lead identifier — paper-friendly)
    - Order ID (reference)
    - Farmer name + phone (handoff)
    - Dealer shop + address + GPS + phone (pickup)
    - Items list: brand + qty + cost (post-approval anti-manipulation
      rule allows full visibility)
    - Total + status note (Awaiting pickup / Picked up & awaiting
      farmer receipt)

    Drops out of this list when the farmer marks received
    (packing_lists.farmer_received_at is set) — order completes
    automatically via _update_order_status.
    """
    await _assert_active_facilitator(db, current_user.id)
    # Approved items the facilitator owns. DO NOT filter on
    # archived_at — pickup persists past timeline expiry per spec.
    rows = (await db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.facilitator_user_id == current_user.id,
            OrderItem.status == OrderItemStatus.APPROVED,
        )
    )).all()

    # 2026-08-17 (per-batch pickup rework) — one entry per (order,
    # approval_round) instead of per order. Facilitator's pickup basket
    # now surfaces each dealer submission round separately: batch 1 can
    # be picked up + received, batch 2 stays independently visible for
    # its own pickup. Emitted rows carry approval_round so the PWA can
    # scope `mark-picked-up?approval_round=N` calls.
    items_by_order_round: dict[tuple[str, int], list[OrderItem]] = {}
    order_by_id: dict[str, Order] = {}
    for item, order in rows:
        key = (order.id, item.approval_round or 1)
        items_by_order_round.setdefault(key, []).append(item)
        order_by_id[order.id] = order

    if not items_by_order_round:
        return []

    order_ids = list(order_by_id.keys())
    pls = (await db.execute(
        select(PackingList).where(PackingList.order_id.in_(order_ids))
    )).scalars().all()
    pl_by_key: dict[tuple[str, int], PackingList] = {
        (pl.order_id, pl.approval_round or 1): pl for pl in pls
    }

    # A batch qualifies as a pickup candidate when:
    #   - every item in the batch is Final Confirmed (dealer committed)
    #   - PL row exists AND farmer_received_at is null (not delivered)
    # PL rows without a matching batch (edge case: legacy null-round)
    # get skipped — the /facilitator/orders GET is what lazy-creates
    # them on the dealer/farmer flow; the pickup endpoint is a
    # consumer only.
    live_keys: list[tuple[str, int]] = []
    for key, items in items_by_order_round.items():
        pl = pl_by_key.get(key)
        if pl is None or pl.farmer_received_at is not None:
            continue
        if any(i.final_confirmed_at is None for i in items):
            continue
        live_keys.append(key)
    if not live_keys:
        return []
    live_order_ids = list({k[0] for k in live_keys})

    # Resolve farmer / dealer / dealer profile / crop name. Batched.
    orders = [order_by_id[oid] for oid in live_order_ids]
    farmer_ids = {o.farmer_user_id for o in orders}
    dealer_ids = {o.dealer_user_id for o in orders if o.dealer_user_id}
    user_by_id: dict[str, User] = {}
    if farmer_ids or dealer_ids:
        urows = (await db.execute(
            select(User).where(User.id.in_(list(farmer_ids | dealer_ids)))
        )).scalars().all()
        user_by_id = {u.id: u for u in urows}
    dealer_profile_by_id: dict[str, dict] = {}
    if dealer_ids:
        prows = (await db.execute(
            select(
                DealerProfile.user_id, DealerProfile.shop_name,
                DealerProfile.shop_address,
                DealerProfile.shop_gps_lat, DealerProfile.shop_gps_lng,
            ).where(DealerProfile.user_id.in_(dealer_ids))
        )).all()
        for did, sname, saddr, slat, slng in prows:
            dealer_profile_by_id[did] = {
                "shop_name": sname,
                "shop_address": saddr,
                "shop_gps_lat": float(slat) if slat is not None else None,
                "shop_gps_lng": float(slng) if slng is not None else None,
            }
    sub_ids = {o.subscription_id for o in orders if o.subscription_id}
    sub_by_id: dict[str, Subscription] = {}
    if sub_ids:
        srows = (await db.execute(
            select(Subscription).where(Subscription.id.in_(sub_ids))
        )).scalars().all()
        sub_by_id = {s.id: s for s in srows}
    pkg_ids = {s.package_id for s in sub_by_id.values() if s.package_id}
    pkg_by_id: dict[str, Package] = {}
    if pkg_ids:
        prows = (await db.execute(
            select(Package).where(Package.id.in_(pkg_ids))
        )).scalars().all()
        pkg_by_id = {p.id: p for p in prows}
    crop_cosh_ids = {p.crop_cosh_id for p in pkg_by_id.values() if p.crop_cosh_id}
    crop_name_by_cosh_id: dict[str, str] = {}
    if crop_cosh_ids:
        from app.modules.sync.models import CoshCoreItem
        crows = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(crop_cosh_ids))
        )).all()
        lang = current_user.language_code or "en"
        for cid, tr in crows:
            if isinstance(tr, dict):
                name = pick_translation(tr, lang, "")
                if name:
                    crop_name_by_cosh_id[cid] = name

    out = []
    for oid, round_n in sorted(live_keys, key=lambda k: (k[0], k[1])):
        o = order_by_id[oid]
        items = items_by_order_round[(oid, round_n)]
        pl = pl_by_key.get((oid, round_n))
        farmer = user_by_id.get(o.farmer_user_id)
        dealer = user_by_id.get(o.dealer_user_id) if o.dealer_user_id else None
        dprof = dealer_profile_by_id.get(o.dealer_user_id) if o.dealer_user_id else None
        sub = sub_by_id.get(o.subscription_id) if o.subscription_id else None
        pkg = pkg_by_id.get(sub.package_id) if (sub and sub.package_id) else None
        crop_name = (
            crop_name_by_cosh_id.get(pkg.crop_cosh_id)
            if (pkg and pkg.crop_cosh_id) else None
        )
        total = sum(float(i.price) for i in items if i.price)
        out.append({
            "order_id": o.id,
            "reference_number": o.reference_number,
            # 2026-08-17 (per-batch rework): each entry is one batch of
            # this order. approval_round scopes the mark-picked-up PUT.
            "approval_round": round_n,
            "packing_code": pl.packing_code if pl else None,
            "packing_shared_at": pl.first_shared_at.isoformat() if pl and pl.first_shared_at else None,
            "picked_up_at": pl.picked_up_at.isoformat() if pl and pl.picked_up_at else None,
            "farmer_received_at": None,  # always null here (we filtered them out)
            "created_at": o.created_at,
            "subscription_id": o.subscription_id,
            "crop_name": crop_name,
            "farmer_name": farmer.name if farmer else None,
            "farmer_phone": farmer.phone if farmer else None,
            "farmer_photo_url": farmer.photo_url if farmer else None,
            "dealer_name": dealer.name if dealer else None,
            "dealer_phone": dealer.phone if dealer else None,
            "dealer_shop_name": dprof.get("shop_name") if dprof else None,
            "dealer_shop_address": dprof.get("shop_address") if dprof else None,
            "dealer_shop_gps_lat": dprof.get("shop_gps_lat") if dprof else None,
            "dealer_shop_gps_lng": dprof.get("shop_gps_lng") if dprof else None,
            "items": [
                {
                    "id": i.id,
                    "brand_name": i.brand_name,
                    "given_volume": float(i.given_volume) if i.given_volume else None,
                    "volume_unit": i.volume_unit,
                    "price": float(i.price) if i.price else None,
                }
                for i in items
            ],
            "total_amount": total,
        })
    # Sort newest first (by order created_at, then by batch round).
    out.sort(key=lambda d: (d["created_at"], -d["approval_round"]), reverse=True)
    return out


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


# ── Dealer: Partial-edit draft (Batch 28) ──────────────────────────────────────
#
# The dealer screen debounces in-flight edits (brand, volume, unit,
# price) and PUTs the per-item bundle here every ~3 s. The PWA also
# mirrors the same payload into IndexedDB so a power-off, network
# drop, or screen change can't lose the work. When the item moves
# to AVAILABLE, /items/{id}/available removes the entry server-side
# and the client drops it from IndexedDB too.

_ALLOWED_DRAFT_KEYS = {
    "brand_cosh_id", "brand_name",
    "given_volume", "volume_unit", "price",
}


@router.put("/dealer/orders/{order_id}/draft/{item_id}")
async def upsert_dealer_draft(
    order_id: str, item_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Per-item draft upsert. Whole entry is replaced on every call;
    the client computes the merged shape from its in-memory state
    so the server doesn't have to."""
    await _assert_active_dealer(db, current_user.id)
    order = await _get_dealer_order(db, order_id, current_user.id)
    # Confirm item belongs to this order — guards against the dealer
    # PUTing to an item from a sibling order they don't own.
    await _get_order_item(db, item_id, order_id)
    entry = {k: data[k] for k in _ALLOWED_DRAFT_KEYS if k in data}
    new_draft = dict(order.dealer_draft or {})
    if entry:
        new_draft[item_id] = entry
    else:
        new_draft.pop(item_id, None)
    order.dealer_draft = new_draft
    await db.commit()
    return {"draft": order.dealer_draft, "item_id": item_id}


@router.delete("/dealer/orders/{order_id}/draft/{item_id}")
async def clear_dealer_draft(
    order_id: str, item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_active_dealer(db, current_user.id)
    order = await _get_dealer_order(db, order_id, current_user.id)
    new_draft = dict(order.dealer_draft or {})
    new_draft.pop(item_id, None)
    order.dealer_draft = new_draft
    await db.commit()
    return {"draft": order.dealer_draft}


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

    # 2026-08-17 — Hide items past the dealer's decision phase. APPROVED
    # items that were Final Confirmed (by any prior dealer, or by this
    # dealer earlier) are in the pickup lifecycle now — farmer's turn.
    # Showing them here confuses a re-routed dealer who inherited an
    # order after a previous dealer already delivered some items
    # (user anchor 2026-08-17). Original dealer still sees them on the
    # Packing pill card; this filter narrows the DETAIL page to items
    # the dealer can still act on. Also strip terminal-non-approved
    # residues (SKIPPED / NA / REJECTED / NOT_NEEDED / REROUTED /
    # REMOVED) — the re-route path resets them but a defensive filter
    # avoids showing them if a race left one behind.
    items = [
        i for i in items
        if not (
            i.status == OrderItemStatus.APPROVED
            and i.final_confirmed_at is not None
        )
        and i.status not in (
            OrderItemStatus.SKIPPED, OrderItemStatus.NOT_AVAILABLE,
            OrderItemStatus.REJECTED, OrderItemStatus.NOT_NEEDED,
            OrderItemStatus.REROUTED, OrderItemStatus.REMOVED,
        )
    ]

    # Helper for the flat item shape. NOTE: relies on
    # `element_block_for_item` being defined further down — items
    # are iterated only after the element batch-resolution pass.
    def item_brief(i: OrderItem) -> dict:
        # Fix 2026-06-01: dealer card was rendering a practice UUID
        # as the title. Resolve the SE's COMMON_NAME for non-NPK
        # practices; NPK practices have no common name (system-discovered)
        # so we label by L2 type so the dealer reads "Chemical NPK
        # Dosage" / "Fertigation NPK" instead of a UUID.
        spec = item_element_specs.get(i.id, {})
        common_name = (
            cosh_name_by_id.get(spec.get("common_name_ref"))
            if spec.get("common_name_ref") else None
        )
        practice = practice_map.get(i.practice_id) if i.practice_id else None
        l2 = practice.l2_type if practice else None
        if l2 == "FERTIGATION_NPK_DOSAGES":
            display_name = "Fertigation NPK Dosage"
        elif l2 == "CHEMICAL_FERTILIZERS_NPK_DOSAGES":
            display_name = "Chemical Fertiliser NPK Dosage"
        else:
            display_name = common_name or (
                l2.replace("_", " ").title() if l2 else "Practice"
            )
        # Fix 2026-06-01: per-item application window.
        item_df, item_dt = _per_item_dates(i)
        return {
            "id": i.id, "practice_id": i.practice_id,
            "status": i.status.value if hasattr(i.status, "value") else i.status,
            "common_name": common_name,
            "l2_type": l2,
            "display_name": display_name,
            "brand_cosh_id": i.brand_cosh_id,
            "brand_name": i.brand_name,
            "given_volume": float(i.given_volume) if i.given_volume else None,
            "estimated_volume": float(i.estimated_volume) if i.estimated_volume else None,
            "volume_unit": i.volume_unit,
            "price": float(i.price) if i.price else None,
            "relation_id": i.relation_id,
            "relation_type": i.relation_type,
            "relation_role": i.relation_role,
            # 2026-06-26 — Per-item brand-lock flag so the dealer
            # surface can show a quiet "Locked brand" indicator next
            # to the leg's status. Sourced from Practice.is_brand_locked
            # (BL-07). Per-Option `has_locked_brand` already exists
            # on the parent Option but isn't fine-grained enough when
            # an AND group mixes locked + free positions.
            "is_brand_locked": bool(practice and practice.is_brand_locked),
            # Batch 26 — SE's authored guidance the dealer reads after
            # picking a brand: recommended dosage + unit, application
            # method, volume per plant (plant-wise only).
            "element_block": element_block_for_item(i.id),
            "application_date_from": item_df,
            "application_date_to": item_dt,
            # 2026-06-30 — Affected-plants count captured at pest
            # diagnosis (PG/SP/QA paths). NULL when:
            #   • crop is area-wise (irrelevant),
            #   • timeline is CCA on area-wise crop,
            #   • QA path where farmer didn't fill the optional field.
            # For plant-wise CHA items where this is NULL, the PWA shows
            # "Please check with the farmer" alongside an empty volume
            # input; the dealer enters volume manually.
            "affected_plants_count": affected_count_by_item.get(i.id),
            # 2026-08-14 (Phase 2 rework): dealer's Final Confirmation
            # timestamp. Null when APPROVED and dealer hasn't tapped
            # Final Confirmation yet — the dealer PWA shows the button
            # on these items. Non-null when the dealer has committed
            # to hand-off → item flows to farmer's Pickup pill.
            "final_confirmed_at": (
                i.final_confirmed_at.isoformat() if i.final_confirmed_at else None
            ),
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

    # Fix 2026-06-01 (per-item date range): an order can span multiple
    # timelines, each with its own application window. Batch-load
    # Timelines + the subscription's crop_start_date so item_brief can
    # surface a per-item window instead of just the order-level one.
    all_timeline_ids = list({i.timeline_id for i in items if i.timeline_id})
    timeline_map: dict[str, Timeline] = {}
    if all_timeline_ids:
        tl_rows = (await db.execute(
            select(Timeline).where(Timeline.id.in_(all_timeline_ids))
        )).scalars().all()
        timeline_map = {t.id: t for t in tl_rows}
    sub_for_dates = (await db.execute(
        select(Subscription).where(Subscription.id == order.subscription_id)
    )).scalar_one_or_none()

    # 2026-06-30 — Per-item affected_plants_count map for plant-wise
    # CHA timelines. Resolves Practice → Timeline → recommendation_id
    # → TriggeredCHAEntry → affected_plants_count. CCA items (no
    # rec_id) fall back to the farmer's declared total; QA items
    # without a count stay None and the PWA renders a "check with the
    # farmer" hint.
    affected_count_by_item: dict[str, Optional[int]] = {}
    if sub_for_dates and (sub_for_dates.number_of_plants or False):
        from app.modules.subscriptions.models import TriggeredCHAEntry
        # Distinct rec_ids referenced by timelines in this order.
        tl_rec_ids: set[str] = set()
        for _t in timeline_map.values():
            r = (
                _t.sp_recommendation_id
                or _t.pg_recommendation_id
                or _t.standard_response_id
            )
            if r:
                tl_rec_ids.add(r)
        # Latest active TriggeredCHAEntry per rec_id (most recent wins
        # when re-diagnosis happened after a prior timeline closed).
        latest_count_by_rec: dict[str, Optional[int]] = {}
        if tl_rec_ids:
            cha_rows = (await db.execute(
                select(TriggeredCHAEntry).where(
                    TriggeredCHAEntry.subscription_id == sub_for_dates.id,
                    TriggeredCHAEntry.recommendation_id.in_(tl_rec_ids),
                    TriggeredCHAEntry.status == "ACTIVE",
                ).order_by(TriggeredCHAEntry.triggered_at.desc())
            )).scalars().all()
            for cr in cha_rows:
                if cr.recommendation_id not in latest_count_by_rec:
                    latest_count_by_rec[cr.recommendation_id] = cr.affected_plants_count
        for it in items:
            tl = timeline_map.get(it.timeline_id) if it.timeline_id else None
            if tl is None:
                continue
            rec = (
                tl.sp_recommendation_id
                or tl.pg_recommendation_id
                or tl.standard_response_id
            )
            if rec:
                # CHA item — count may be set or None (QA blank case).
                affected_count_by_item[it.id] = latest_count_by_rec.get(rec)
            else:
                # CCA item on a plant-wise crop — full orchard.
                affected_count_by_item[it.id] = int(sub_for_dates.number_of_plants)

    crop_start_date = sub_for_dates.crop_start_date if sub_for_dates else None
    crop_start_d = None
    if crop_start_date is not None:
        crop_start_d = (
            crop_start_date.date()
            if hasattr(crop_start_date, "date") else crop_start_date
        )

    # 2026-06-29 (Phase 3) — BL-03 window absorption applied at the
    # order surface. When an item's timeline absorbed another timeline
    # at advisory time (e.g. TL2's OR group absorbing TL1's standalone
    # A), the dealer / farmer / facilitator should see the merged
    # window on the item, not just the master from/to of TL2. Match
    # the farmer's advisory view exactly.
    from app.services.order_bundle import compute_absorption_extended_windows
    from datetime import date as _date_cls
    abs_today = _date_cls.today()
    abs_to_d = order.date_to.date() if order.date_to and hasattr(order.date_to, "date") else (order.date_to or abs_today)
    extended_windows: dict[str, tuple[_date_cls, _date_cls]] = {}
    if sub_for_dates is not None:
        try:
            extended_windows = await compute_absorption_extended_windows(
                db, subscription=sub_for_dates, today=abs_today, to_date=abs_to_d,
            )
        except Exception:
            # Defensive: absorption is best-effort enrichment; never
            # block the dealer order detail if it errors.
            extended_windows = {}

    def _per_item_dates(it: OrderItem) -> tuple[str | None, str | None]:
        """Resolve (date_from, date_to) ISO strings for a single item.
        Returns (None, None) when the window isn't computable (no
        crop_start_date for DAS/DBS, or CALENDAR not yet wired).
        2026-06-29: prefers absorption-extended window when this
        item's timeline absorbed another at advisory time."""
        if it.timeline_id and it.timeline_id in extended_windows:
            df, dt_ = extended_windows[it.timeline_id]
            return (df.isoformat(), dt_.isoformat())
        tl = timeline_map.get(it.timeline_id) if it.timeline_id else None
        if tl is None or crop_start_d is None:
            return (None, None)
        from app.services.snapshot_render import (
            TimelineMetadata, cca_calendar_dates,
        )
        from_type_value = (
            tl.from_type.value if hasattr(tl.from_type, "value")
            else str(tl.from_type)
        )
        if from_type_value not in ("DAS", "DBS"):
            return (None, None)
        meta = TimelineMetadata(
            from_type=from_type_value,
            from_value=int(tl.from_value),
            to_value=int(tl.to_value),
        )
        df, dt_ = cca_calendar_dates(meta, crop_start_d)
        return (df.isoformat(), dt_.isoformat())

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
            # Fix 2026-06-01: surface the SE's COMMON_NAME so the
            # dealer card shows "Mancozeb" instead of a practice UUID.
            "common_name_ref": None,
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
            elif et == "COMMON_NAME":
                spec["common_name_ref"] = _el_get(el, "cosh_ref")
        item_element_specs[it.id] = spec
        for k in ("dosage_unit_ref", "application_method_ref",
                  "vol_per_plant_unit_ref", "common_name_ref"):
            if spec[k]:
                cosh_refs_needed.add(spec[k])

    lang = current_user.language_code or "en"
    cosh_name_by_id: dict[str, str] = {}
    if cosh_refs_needed:
        cosh_rows = (await db.execute(
            select(_CoshCore).where(_CoshCore.cosh_id.in_(cosh_refs_needed))
        )).scalars().all()
        for cc in cosh_rows:
            tr = cc.translations or {}
            if isinstance(tr, dict):
                cosh_name_by_id[cc.cosh_id] = pick_translation(
                    tr, lang, cc.cosh_id,
                )

    def _resolve_name(ref):
        return cosh_name_by_id.get(ref) if ref else None

    # 2026-08-17 — Item lookup for the element block so we can read
    # Practice.frequency_days + resolve the item's timeline dates
    # (needed to compute applications_count for frequency-based practices
    # like Fertigation NPK Dosages / Fertigation Products).
    items_by_id: dict[str, OrderItem] = {i.id: i for i in items}

    def element_block_for_item(item_id: str) -> dict:
        s = item_element_specs.get(item_id, {})
        it = items_by_id.get(item_id)
        practice = practice_map.get(it.practice_id) if it and it.practice_id else None
        freq_days = (
            int(practice.frequency_days)
            if practice and practice.frequency_days else None
        )
        applications_count: int | None = None
        if freq_days and freq_days >= 1 and it:
            df_iso, dt_iso = _per_item_dates(it)
            if df_iso and dt_iso:
                from datetime import date as _d
                df = _d.fromisoformat(df_iso)
                dt = _d.fromisoformat(dt_iso)
                timeline_days = (dt - df).days + 1
                if timeline_days >= 1:
                    from math import ceil as _ceil
                    applications_count = _ceil(timeline_days / freq_days)
        return {
            "dosage_value": s.get("dosage_value"),
            "dosage_unit_cosh_id": s.get("dosage_unit_ref"),
            "dosage_unit_name": _resolve_name(s.get("dosage_unit_ref")),
            "application_method_cosh_id": s.get("application_method_ref"),
            "application_method_name": _resolve_name(s.get("application_method_ref")),
            "vol_per_plant_value": s.get("vol_per_plant_value"),
            "vol_per_plant_unit_cosh_id": s.get("vol_per_plant_unit_ref"),
            "vol_per_plant_unit_name": _resolve_name(s.get("vol_per_plant_unit_ref")),
            # 2026-08-17 — Frequency-based practice metadata for the
            # dealer's element-guidance card. Dealer sees "per
            # application" on the dosage line + an "Applications" row
            # so the total volume request makes sense (per-app dose ×
            # applications × area).
            "frequency_days": freq_days,
            "applications_count": applications_count,
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
    farmer_context = await _build_farmer_context(db, order, lang=lang)
    facilitator_context = await _build_facilitator_context(db, order)

    # 2026-06-21 — Packing-state snapshot for the dealer detail
    # page's post-share status banner. Same fields the list endpoint
    # already ships; we resolve them here too so the detail page can
    # render "Awaiting farmer pickup" / "Picked up by Facilitator …"
    # / "Received by farmer …" without a second round-trip.
    # 2026-08-17 — Per-batch rework: canonical PL = earliest round.
    pl_row = (await db.execute(
        select(PackingList)
        .where(PackingList.order_id == order.id)
        .order_by(PackingList.approval_round.asc().nulls_first())
        .limit(1)
    )).scalar_one_or_none()
    pkg_picked_up_role: str | None = None
    pkg_picked_up_name: str | None = None
    if pl_row and pl_row.picked_up_at and pl_row.picked_up_by_user_id:
        if pl_row.picked_up_by_user_id == order.facilitator_user_id:
            pkg_picked_up_role = "FACILITATOR"
            fac_user = (await db.execute(
                select(User).where(User.id == pl_row.picked_up_by_user_id)
            )).scalar_one_or_none()
            if fac_user:
                pkg_picked_up_name = fac_user.name
        else:
            pkg_picked_up_role = "FARMER"

    return {
        "id": order.id, "status": order.status,
        "reference_number": order.reference_number,
        "farmer_user_id": order.farmer_user_id, "client_id": order.client_id,
        "facilitator_user_id": order.facilitator_user_id,
        "date_from": order.date_from, "date_to": order.date_to,
        "created_at": order.created_at,
        # Batch 24 — context the dealer needs to make a call about
        # the order. Hidden from the farmer's view by living on a
        # dealer-side endpoint only.
        "farmer_context": farmer_context,
        # 2026-06-19 — When the order arrived via a facilitator, the
        # dealer needs the same identify-confirm block for them.
        # null otherwise.
        "facilitator_context": facilitator_context,
        # Flat list (unchanged shape for backward compat)
        "items": [item_brief(i) for i in items],
        # New: Part-aware relation structure
        "relations": relations_payload,
        "standalone_items": [item_brief(i) for i in standalone],
        # Batch 28 — server-authoritative copy of the dealer's
        # in-flight per-item edits. The PWA hydrates its IndexedDB
        # mirror from this on mount so a different device picks up
        # exactly where the last one left off.
        "dealer_draft": order.dealer_draft or {},
        # Batch 30C — spec §4.2. Same brand appearing across multiple
        # NPK practices on this order is summed into one consolidated
        # line so the dealer enters Given Volume once. Per-timeline
        # quantities still go to the farmer untouched.
        # 2026-07-13 — Narrowed to NPK practices per literal §4.2
        # scope (was: all brand-sharing items across any practice).
        "consolidated_brands": _consolidate_brands_across_items(items, practice_map),
        # 2026-06-21 — Packing-state for the post-share status banner.
        "packing_code": pl_row.packing_code if pl_row else None,
        "packing_list_shared_at": (
            pl_row.first_shared_at.isoformat() if pl_row and pl_row.first_shared_at else None
        ),
        "packing_picked_up_at": (
            pl_row.picked_up_at.isoformat() if pl_row and pl_row.picked_up_at else None
        ),
        "packing_picked_up_by_role": pkg_picked_up_role,
        "packing_picked_up_by_name": pkg_picked_up_name,
        "packing_farmer_received_at": (
            pl_row.farmer_received_at.isoformat() if pl_row and pl_row.farmer_received_at else None
        ),
    }


def _consolidate_brands_across_items(
    items: list[OrderItem],
    practice_map: dict[str, Practice],
) -> list[dict]:
    """Spec §4.2 — brand consolidation. NPK practices can produce many
    OrderItems for the same trade name across timelines (e.g. Urea in
    basal + top dressing). The dealer cares about the total to procure;
    the farmer still sees per-timeline lines. We sum `given_volume` per
    `brand_cosh_id` across active items and surface a small list the
    PWA can render as a Volume/Price summary card.

    Only counts:
    - Items with a brand committed and a volume set.
    - Items in dealer-actionable states (already-skipped or
      farmer-rejected items would inflate the total wrongly).
    - Items whose practice.l2_type is an NPK dosage type. Spec §4.2
      is explicit that consolidation is for NPK screens; non-NPK
      cross-timeline totals (e.g. same pesticide brand on two spray
      timelines) can arrive as a separate feature if needed.

    Groups by (brand_cosh_id, volume_unit) so a rare unit mismatch
    (kg vs g) never silently sums. In practice NPK is always kg
    (npk_select pins `volume_unit = 'kg'`) so this is belt-and-braces.
    """
    npk_l2_types = {
        "CHEMICAL_FERTILIZERS_NPK_DOSAGES",
        "FERTIGATION_NPK_DOSAGES",
    }
    actionable = {
        OrderItemStatus.AVAILABLE,
        OrderItemStatus.PENDING,
        OrderItemStatus.SENT_FOR_APPROVAL,
        OrderItemStatus.APPROVED,
    }
    totals: dict[tuple[str, str | None], dict] = {}
    for it in items:
        if it.brand_cosh_id is None or it.given_volume is None:
            continue
        if it.status not in actionable:
            continue
        practice = practice_map.get(it.practice_id) if it.practice_id else None
        if practice is None or practice.l2_type not in npk_l2_types:
            continue
        key = (it.brand_cosh_id, it.volume_unit)
        bucket = totals.setdefault(key, {
            "brand_cosh_id": it.brand_cosh_id,
            "brand_name": it.brand_name,
            "volume_unit": it.volume_unit,
            "total_volume": 0.0,
            "line_count": 0,
        })
        bucket["total_volume"] += float(it.given_volume)
        bucket["line_count"] += 1
    # Stable sort by brand name for the PWA — easier to read than by
    # cosh_id and easier for the dealer to scan a packing list against.
    out = list(totals.values())
    out.sort(key=lambda b: (b.get("brand_name") or "").lower())
    for b in out:
        b["total_volume"] = round(b["total_volume"], 2)
    return out


async def _build_farmer_context(
    db: AsyncSession, order: Order, *, lang: str = "en",
) -> dict:
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
                if isinstance(tr, dict):
                    crop_name = pick_translation(tr, lang, "") or None

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
        # 2026-06-19 — Photo for the dealer's WhatsApp-style identity
        # confirmation. Rendered as a tap-to-enlarge avatar.
        "farmer_photo_url": farmer.photo_url if farmer else None,
        "crop_name": crop_name,
        "measure": measure,
        "age_value": age_value,
        "age_unit": age_unit,
        "farm_area_acres": float(sub.farm_area_acres) if (sub and sub.farm_area_acres) else None,
        "number_of_plants": int(sub.number_of_plants) if (sub and sub.number_of_plants) else None,
    }


async def _build_facilitator_context(
    db: AsyncSession, order: Order,
) -> Optional[dict]:
    """Facilitator details for the dealer order-detail header.
    Returns None when the order didn't come via a facilitator.

    Symmetric with `_build_farmer_context` (name + phone + photo)
    so the dealer can call and visually identify the person who
    routed the order to them — same WhatsApp-style identity
    confirmation as the farmer.
    """
    if not order.facilitator_user_id:
        return None
    fac = (await db.execute(
        select(User).where(User.id == order.facilitator_user_id)
    )).scalar_one_or_none()
    if fac is None:
        return None
    return {
        "facilitator_user_id": fac.id,
        "facilitator_name": fac.name,
        "facilitator_phone": fac.phone,
        "facilitator_photo_url": fac.photo_url,
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
    """SA / privileged CM: list every brand submission. Response is
    enriched with the dealer's name + phone so the SA can call for
    clarification without a second round-trip.
    """
    rows = (await db.execute(
        select(MissingBrandReport).order_by(MissingBrandReport.created_at.desc())
    )).scalars().all()
    if not rows:
        return []
    dealer_ids = list({r.dealer_user_id for r in rows})
    users = (await db.execute(
        select(User).where(User.id.in_(dealer_ids))
    )).scalars().all()
    u_by_id = {u.id: u for u in users}
    out = []
    for r in rows:
        u = u_by_id.get(r.dealer_user_id)
        out.append({
            "id": r.id,
            "dealer_user_id": r.dealer_user_id,
            "dealer_name": u.name if u else None,
            "dealer_phone": u.phone if u else None,
            "order_item_id": r.order_item_id,
            "brand_name_reported": r.brand_name_reported,
            "manufacturer_name": r.manufacturer_name,
            "l1_type": r.l1_type,
            "l2_practice": r.l2_practice,
            "additional_info": r.additional_info,
            "photos": r.photos or [],
            "status": r.status,
            "cm_notes": r.cm_notes,
            "reviewed_at": r.reviewed_at,
            "created_at": r.created_at,
        })
    return out


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
    claimed it but the function body didn't check).

    2026-07-04 — Stamp `reviewed_at` on any transition INTO a terminal
    status (APPROVED / REJECTED). Drives the dealer's "unseen update"
    badge on the dashboard tile.
    """
    from app.modules.advisory.router import _assert_sa_or_privileged_cm
    from datetime import datetime, timezone
    await _assert_sa_or_privileged_cm(db, current_user, "BRAND_HANDLING")
    report = (await db.execute(
        select(MissingBrandReport).where(MissingBrandReport.id == report_id)
    )).scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if "status" in data:
        new_status = data["status"]
        report.status = new_status
        if new_status in ("APPROVED", "REJECTED"):
            report.reviewed_at = datetime.now(timezone.utc)
    if "cm_notes" in data:
        report.cm_notes = data["cm_notes"]
    await db.commit()
    return {"id": report_id, "status": report.status}


# ── Dealer Brand Forms — dashboard-launched standalone submissions ──

def _brand_form_to_dict(r: MissingBrandReport) -> dict:
    """Serialise a MissingBrandReport row for the dealer PWA. Excludes
    dealer PII since the dealer already knows their own info."""
    return {
        "id": r.id,
        "brand_name_reported": r.brand_name_reported,
        "manufacturer_name": r.manufacturer_name,
        "l1_type": r.l1_type,
        "l2_practice": r.l2_practice,
        "additional_info": r.additional_info,
        "photos": r.photos or [],
        "status": r.status,
        "cm_notes": r.cm_notes,
        "reviewed_at": r.reviewed_at,
        "created_at": r.created_at,
        "dealer_seen_status_at": r.dealer_seen_status_at,
    }


def _send_sa_brand_form_email(
    dealer: User, brand: str, manufacturer: str | None,
    l1_type: str | None, l2_practice: str | None,
):
    """Nudge the SA that a new Brand Form landed. Failures are logged
    but never block the create — the row is already committed by the
    time this runs. Uses the existing sync `_send_email` helper from
    clients.service (same SMTP path CA-welcome / onboarding-link
    emails travel through)."""
    import logging
    from app.config import settings
    from app.modules.clients.service import _send_email

    sa_email = (settings.sa_email or "").strip()
    if not sa_email:
        return
    subject = f"[RootsTalk] New Brand submission: {brand}"
    plain_lines = [
        "A dealer has submitted a new Brand Form for review.",
        "",
        f"Brand:        {brand}",
        f"Manufacturer: {manufacturer or '—'}",
        f"L1 category:  {l1_type or '—'}",
        f"L2 category:  {l2_practice or '—'}",
        "",
        f"Dealer:       {dealer.name or '—'}",
        f"Phone:        {dealer.phone or '—'}",
        "",
        "Open the SA portal → Brand Handling to review.",
    ]
    plain = "\n".join(plain_lines)
    html = (
        "<p>A dealer has submitted a new Brand Form for review.</p>"
        f"<p><b>Brand:</b> {brand}<br>"
        f"<b>Manufacturer:</b> {manufacturer or '—'}<br>"
        f"<b>L1 category:</b> {l1_type or '—'}<br>"
        f"<b>L2 category:</b> {l2_practice or '—'}</p>"
        f"<p><b>Dealer:</b> {dealer.name or '—'}<br>"
        f"<b>Phone:</b> {dealer.phone or '—'}</p>"
        "<p>Open the SA portal → Brand Handling to review.</p>"
    )
    try:
        _send_email(sa_email, subject, html, plain)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "SA brand-form email failed for report by dealer %s: %s",
            dealer.id, e,
        )


@router.post("/dealer/brand-forms", status_code=201)
async def create_brand_form(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer creates a standalone Brand Form from the dashboard tile.

    Required: `brand_name_reported`, `manufacturer_name`, `l1_type`,
    `l2_practice`, `photos` (2-4 S3 URLs). Optional:
    `additional_info` (dealer's free-text notes).

    Fires an email to `settings.sa_email` after commit so the SA
    picks up the new submission even when they aren't at the portal.
    """
    await _assert_active_dealer(db, current_user.id)

    brand = (data.get("brand_name_reported") or "").strip()
    manufacturer = (data.get("manufacturer_name") or "").strip()
    l1_type = (data.get("l1_type") or "").strip()
    l2_practice = (data.get("l2_practice") or "").strip()
    photos = data.get("photos") or []
    additional_info = (data.get("additional_info") or "").strip() or None

    if not brand:
        raise HTTPException(
            status_code=422,
            detail={"code": "brand_required", "message": "Brand name is required."},
        )
    if not manufacturer:
        raise HTTPException(
            status_code=422,
            detail={"code": "manufacturer_required", "message": "Manufacturer name is required."},
        )
    if not l1_type or not l2_practice:
        raise HTTPException(
            status_code=422,
            detail={"code": "category_required", "message": "Input category (L1) and sub-category (L2) are required."},
        )
    if not isinstance(photos, list) or len(photos) < 2 or len(photos) > 4:
        raise HTTPException(
            status_code=422,
            detail={"code": "photo_count", "message": "Please upload between 2 and 4 product photos."},
        )

    report = MissingBrandReport(
        dealer_user_id=current_user.id,
        order_item_id=None,
        brand_name_reported=brand,
        manufacturer_name=manufacturer,
        l1_type=l1_type,
        l2_practice=l2_practice,
        additional_info=additional_info,
        photos=list(photos),
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)

    # Fire-and-forget SA email — failures don't affect the create.
    _send_sa_brand_form_email(
        current_user, brand, manufacturer, l1_type, l2_practice,
    )
    return _brand_form_to_dict(report)


@router.get("/dealer/brand-forms")
async def list_dealer_brand_forms(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer's own Brand Form history. Excludes rows the dealer has
    hidden from their view via /hide (SA still sees those on the
    admin surface). Newest first."""
    await _assert_active_dealer(db, current_user.id)
    rows = (await db.execute(
        select(MissingBrandReport)
        .where(
            MissingBrandReport.dealer_user_id == current_user.id,
            MissingBrandReport.hidden_from_dealer_at.is_(None),
        )
        .order_by(MissingBrandReport.created_at.desc())
    )).scalars().all()
    return [_brand_form_to_dict(r) for r in rows]


@router.put("/dealer/brand-forms/{form_id}/hide")
async def hide_dealer_brand_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer soft-deletes a Brand Form from their history view. Only
    allowed after the SA has responded (status APPROVED or REJECTED)
    — a PENDING / REVIEWED submission is still live, hiding it would
    be a footgun. SA continues to see the row in the admin surface."""
    from datetime import datetime, timezone
    await _assert_active_dealer(db, current_user.id)
    report = (await db.execute(
        select(MissingBrandReport).where(
            MissingBrandReport.id == form_id,
            MissingBrandReport.dealer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Brand Form not found")
    if report.status not in ("APPROVED", "REJECTED"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_responded",
                "message": "You can hide a Brand Form only after we've responded to it.",
            },
        )
    report.hidden_from_dealer_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": form_id, "hidden": True}


@router.put("/dealer/brand-forms/{form_id}/mark-seen")
async def mark_dealer_brand_form_seen(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer viewed a form's status. Stamp `dealer_seen_status_at`
    so the unread-count badge on the dashboard tile clears for this
    row."""
    from datetime import datetime, timezone
    await _assert_active_dealer(db, current_user.id)
    report = (await db.execute(
        select(MissingBrandReport).where(
            MissingBrandReport.id == form_id,
            MissingBrandReport.dealer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Brand Form not found")
    report.dealer_seen_status_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": form_id, "seen_at": report.dealer_seen_status_at}


@router.get("/dealer/brand-forms/unread-count")
async def dealer_brand_form_unread_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Count of dealer's Brand Forms whose SA response is newer than
    the dealer's last-seen timestamp. Drives the dashboard tile
    badge."""
    await _assert_active_dealer(db, current_user.id)
    rows = (await db.execute(
        select(MissingBrandReport).where(
            MissingBrandReport.dealer_user_id == current_user.id,
            MissingBrandReport.hidden_from_dealer_at.is_(None),
            MissingBrandReport.reviewed_at.is_not(None),
        )
    )).scalars().all()
    unseen = sum(
        1 for r in rows
        if r.dealer_seen_status_at is None
        or r.reviewed_at > r.dealer_seen_status_at
    )
    return {"count": unseen}


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
        lang=current_user.language_code or "en",
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
    data: Optional[dict] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Orders V2 Batch 10 + 2026-06-03 lineage/postpone-choice rework —
    bundled re-route of Returned items into a fresh DRAFT.

    Eligible item statuses by default:
      - NOT_AVAILABLE (dealer said no)
      - REJECTED (farmer's "Remove" on the approval screen)

    POSTPONED items are NOT included by default. The farmer chooses
    at reroute time via the nudge modal:
      body = { "include_postponed": true } → POSTPONED items on the
      same order are first flipped to NOT_AVAILABLE (with a
      POSTPONED_CANCELLED_BY_FARMER event for audit), then included
      in the reroute batch.

    Lineage: the new DRAFT order carries `lineage_root_id` = the
    original order's `lineage_root_id` if set, else the original's
    id. The farmer's Manage tab groups every order with the same
    lineage_root under one card.

    Each migrated item keeps its `lineage_id` so the audit trail is
    a single thread through the dealer hops. Original rows stay on
    the source order as REROUTED so reports still see what each
    leg's dealer did.
    """
    order = await _get_farmer_order(db, order_id, current_user.id)

    # 2026-06-08 — Spec defence: when a facilitator owns the order
    # (`order.facilitator_user_id` is set), the returned items belong
    # to the facilitator's queue. The facilitator either forwards
    # them to another dealer via /facilitator/orders/{id}/reroute-
    # returned OR hands them back via /return-to-farmer. Letting the
    # farmer reroute here would create a new DRAFT with
    # facilitator_user_id=None and silently steal the order out of
    # the facilitator's loop. The PWA Manage tab + review page
    # already hide the CTA; this guard is defence-in-depth for
    # stale tabs / direct URL hits.
    if order.facilitator_user_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "facilitator_owns_order",
                "message": (
                    "This order is being handled by your facilitator. "
                    "They will forward returned items to another "
                    "dealer or hand them back to you."
                ),
            },
        )

    payload = data or {}
    include_postponed = bool(payload.get("include_postponed"))

    items_q = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.archived_at.is_(None),
        )
    )
    all_items = items_q.scalars().all()

    # Returned-by-default set.
    returned_set = {OrderItemStatus.NOT_AVAILABLE, OrderItemStatus.REJECTED}
    items_to_reroute = [it for it in all_items if it.status in returned_set]
    postponed_items = [it for it in all_items if it.status == OrderItemStatus.POSTPONED]

    if include_postponed:
        # Flip each postponed item to NOT_AVAILABLE before including
        # in the reroute. Records a POSTPONED_CANCELLED_BY_FARMER
        # event so the audit shows the farmer's choice, not a
        # dealer-side abandonment.
        for pi in postponed_items:
            prev = pi.status.value if hasattr(pi.status, "value") else pi.status
            res = validate_item_transition(
                pi.status, OrderItemStatus.NOT_AVAILABLE.value, FARMER,
            )
            if not res.allowed:
                _raise_transition(res)
            pi.status = OrderItemStatus.NOT_AVAILABLE
            await _record_event(
                db, lineage_id=pi.lineage_id,
                event_type="POSTPONED_CANCELLED_BY_FARMER",
                actor_user_id=current_user.id, actor_role="FARMER",
                order_id=order.id, order_item_id=pi.id,
                prev_status=prev, new_status=OrderItemStatus.NOT_AVAILABLE.value,
                metadata={"trigger": "bundled_reroute_include_postponed"},
            )
            items_to_reroute.append(pi)

    if not items_to_reroute:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "nothing_to_reroute",
                "message": "No items in this order need re-routing.",
            },
        )

    # Lineage: new draft inherits root from the original order. If
    # the original is itself a root (lineage_root_id is null), point
    # the new draft at the original's id.
    new_lineage_root = order.lineage_root_id or order.id

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
        lineage_root_id=new_lineage_root,
        # Farmer reroute-returned inherits the Order ID.
        reference_number=order.reference_number,
    )
    db.add(new_draft)
    await db.flush()

    # Backfill the original order's lineage_root_id if it's still
    # null (i.e. this is the first reroute child being created from
    # it). Keeps the grouping query simple.
    if order.lineage_root_id is None:
        order.lineage_root_id = order.id

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
    sfa = (await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order_id,
            OrderItem.status == OrderItemStatus.SENT_FOR_APPROVAL,
        )
    )).scalars().all()
    if not sfa:
        raise HTTPException(status_code=400, detail="No items awaiting approval")

    # 2026-06-05 — Approve only the CURRENT round so per-order queueing
    # works. A later round (resolved postpone) waits behind. Items with
    # NULL approval_round (legacy) collapse to the same bucket as the
    # min-non-null round so the data backfilled by the migration
    # behaves consistently.
    rounds_present = sorted({i.approval_round for i in sfa if i.approval_round is not None})
    current_round = rounds_present[0] if rounds_present else None
    items_to_approve = [
        i for i in sfa
        if current_round is None or i.approval_round is None or i.approval_round == current_round
    ]
    for item in items_to_approve:
        res = validate_item_transition(item.status, OrderItemStatus.APPROVED.value, FARMER)
        if not res.allowed:
            _raise_transition(res)
        item.status = OrderItemStatus.APPROVED
    await _update_order_status(db, order_id)
    await db.commit()
    return {"approved_count": len(items_to_approve)}


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
    # Push the farmer that dealer has accepted; the farmer's next
    # action is to wait for packing / pickup so no CTA in the body.
    farmer = (await db.execute(
        select(User).where(User.id == order.farmer_user_id)
    )).scalar_one_or_none()
    if farmer and farmer.fcm_token:
        try:
            await send_fcm(
                token=farmer.fcm_token,
                title=DEALER_ACCEPT_FARMER_FCM_TITLE,
                body=_fmt_order_body(DEALER_ACCEPT_FARMER_FCM_BODY_TPL, order),
                data={
                    "type": "ORDER_ACCEPTED_BY_DEALER",
                    "order_id": order.id,
                    "click_action": f"/crop-detail/{order.subscription_id}/orders",
                },
            )
        except Exception as e:
            _orders_logger.error(
                f"FCM send raised unexpectedly for farmer {farmer.id}: {e}"
            )
    return {"order_id": order_id, "status": order.status}


@router.put("/dealer/orders/{order_id}/decline")
async def dealer_decline_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer declines an order (Phase 2 rework, 2026-08-14). Model B
    DRAFT flow unwound — source order gets flagged returned-to-farmer
    (or returned-to-facilitator, if facilitator-owned), items released
    in place. No new order row created.

    Semantics:
      - All non-terminal items on the source flip to NOT_AVAILABLE
        (dealer said no to every item).
      - Facilitator-owned source: is_returned_to_facilitator=true so
        the facilitator's Routed card picks it up in "Returned" state.
      - Direct farmer-dealer source: is_returned_to_farmer=true +
        return_reason='dealer_declined', released_dealer_user_id
        preserved for the chip context. Farmer's Routed card picks it
        up in "Returned" state with Send / Discard buttons.
      - Source order stays live (status not changed to CANCELLED).
        Dealer's queue drops the order via the is_returned_to_farmer /
        is_returned_to_facilitator filter.
    """
    await _assert_active_dealer(db, current_user.id)
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.dealer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    prev_order_status = order.status.value if hasattr(order.status, "value") else order.status
    facilitator_owns = order.facilitator_user_id is not None

    items = (await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.archived_at.is_(None),
        )
    )).scalars().all()

    released = 0
    for it in items:
        if it.status in {
            OrderItemStatus.APPROVED, OrderItemStatus.REJECTED,
            OrderItemStatus.REROUTED, OrderItemStatus.SKIPPED,
            OrderItemStatus.REMOVED, OrderItemStatus.NOT_NEEDED,
            OrderItemStatus.NOT_AVAILABLE,
        }:
            continue
        prev_item = it.status.value if hasattr(it.status, "value") else it.status
        it.status = OrderItemStatus.NOT_AVAILABLE
        it.postponed_until = None
        await _record_event(
            db, lineage_id=it.lineage_id,
            event_type="RELEASED_BY_DEALER_DECLINE",
            actor_user_id=current_user.id, actor_role="DEALER",
            order_id=order.id, order_item_id=it.id,
            prev_status=prev_item,
            new_status=OrderItemStatus.NOT_AVAILABLE.value,
        )
        released += 1

    if order.lineage_root_id is None:
        order.lineage_root_id = order.id

    if facilitator_owns:
        order.is_returned_to_facilitator = True
        # 2026-08-15 (Phase 2 facilitator-flow fix F2): preserve the
        # dealer chip context, then drop dealer_user_id. Dealer is out;
        # the order is now the facilitator's problem (Send / Return to
        # farmer). Symmetric with F1 on facilitator_reject_order.
        if order.dealer_user_id and not order.released_dealer_user_id:
            order.released_dealer_user_id = order.dealer_user_id
        order.dealer_user_id = None
    else:
        order.is_returned_to_farmer = True
        order.return_reason = 'dealer_declined'
        if order.dealer_user_id and not order.released_dealer_user_id:
            order.released_dealer_user_id = order.dealer_user_id
        order.dealer_user_id = None

    await _record_event(
        db, lineage_id=order.id,
        event_type="DECLINED_BY_DEALER",
        actor_user_id=current_user.id, actor_role="DEALER",
        order_id=order.id,
        prev_status=prev_order_status,
        new_status=prev_order_status,
        metadata={
            "released_item_count": released,
            "routed_back_to": "FACILITATOR" if facilitator_owns else "FARMER",
        },
    )
    await db.commit()
    return {
        "status": order.status,
        "released_item_count": released,
        "routed_back_to": "FACILITATOR" if facilitator_owns else "FARMER",
    }


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

    raw_items = [
        {
            "id": i.id,
            "practice_id": i.practice_id,
            "brand_cosh_id": i.brand_cosh_id,
            "brand_name": i.brand_name,
            "given_volume": float(i.given_volume) if i.given_volume else None,
            "volume_unit": i.volume_unit,
            "price": float(i.price) if i.price else None,
            "status": i.status,
        }
        for i in items
    ]
    # 2026-06-03 — Consolidate same-brand rows so the dealer's packing
    # list shows one line per brand+unit (summed qty + summed ₹).
    # Reuses the same helper that drives /farmer/purchased-items so
    # both farmer and dealer see the order through the same lens.
    consolidated = consolidate_purchased_items(raw_items)
    return {
        "order_id": order.id,
        "status": order.status,
        "date_from": order.date_from,
        "date_to": order.date_to,
        "farmer_name": farmer.name if farmer else None,
        "farmer_phone": farmer.phone if farmer else None,
        "items": consolidated,
        "total_amount": sum((r.get("price") or 0) for r in consolidated),
    }


# ── Dealer: NPK options (Batch 30, RootsTalk_NPK_Handling.pdf) ────────────────
#
# Returns the ranked Mixed list + enabled Straight list for a single
# NPK practice. The dealer screen posts back the picked Mixed (or
# none) and an optional `gap` override; the second call re-ranks
# Straights against the gap.
#
# Practice elements consumed:
#   N_DOSAGE, P_DOSAGE, K_DOSAGE — the SE's required nutrient kg
#   (UNIT, FORMULATION, APPLICATION_METHOD are advisory only here)

_NPK_L2_TYPES = {
    "CHEMICAL_FERTILIZERS_NPK_DOSAGES",
    "FERTIGATION_NPK_DOSAGES",
}


async def _fertigation_multiplier_from_practice(
    db: AsyncSession, practice: Practice, by_type: dict,
) -> int:
    """Compute the total number of applications across a Fertigation
    NPK practice's timeline given its `FERTIGATION_INTERVAL` element.

    Invariant (universal RootsTalk rule): a PO is a procurement event
    for the WHOLE timeline protocol, not a per-day-window purchase. So
    the multiplier counts total applications across the full timeline
    (from timeline start to timeline end), independent of the PO's own
    date_from / date_to. A farmer ordering mid-timeline still receives
    the full timeline's requirement. Do NOT "optimise" this by narrowing
    to remaining-applications-from-order-date; that would fragment
    fertigation protocols across multiple POs and break the invariant
    shared with bl06_volume_calc.py.

    Returns 1 (no multiplier) when:
    - Interval element is missing / blank / unparseable (SE meant a
      single application).
    - Timeline data is unresolvable (defensive fallback).

    Pre-existing bug this replaces (flagged 2026-07-13 in commit
    `bf26e11`): both `/npk-options` and `/npk-select` used to read
    `by_type.get("applications")` — lowercase, singular — which never
    matched the L2 spec's actual `NUMBER_OF_APPLICATIONS` element
    name. And `NUMBER_OF_APPLICATIONS` is `auto_calculated`, i.e. it
    was never actually stored as an Element row by SE authoring. So
    the multiplier silently stayed at 1, `given_volume` came out as
    per-application kg (not total × N), and farmers under-purchased
    for the timeline.
    """
    interval_el = by_type.get("FERTIGATION_INTERVAL")
    if interval_el is None or interval_el.value is None:
        return 1
    try:
        interval = int(str(interval_el.value).strip())
    except (TypeError, ValueError):
        return 1
    if interval < 1:
        return 1
    tl = (await db.execute(
        select(Timeline).where(Timeline.id == practice.timeline_id)
    )).scalar_one_or_none()
    if tl is None or tl.from_value is None or tl.to_value is None:
        return 1
    from app.modules.advisory.router import compute_number_of_applications
    n = compute_number_of_applications(
        int(tl.from_value), int(tl.to_value), interval,
    )
    return max(1, n)


def _candidate_to_dict(
    cand: NPKCandidate,
) -> dict:
    return {
        "cosh_id": cand.cosh_id,
        "name": cand.name,
        "n": cand.concentration.n,
        "p": cand.concentration.p,
        "k": cand.concentration.k,
        "class": classify_fertiliser(cand.concentration),
        "water_soluble": cand.water_soluble,
    }


@router.get("/dealer/orders/{order_id}/items/{item_id}/npk-options")
async def get_item_npk_options(
    order_id: str, item_id: str,
    picked_mixed_cosh_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Spec §2 — ranked Mixed list + (after Mixed pick) enabled Straights.

    Query params:
      picked_mixed_cosh_id  Optional; pass the dealer's Mixed selection
                            on the second call to get the Straight list
                            filtered by remaining gap.

    Response shape:
      {
        "is_npk_practice": bool,
        "fertigation": bool,
        "required_dose": {"n": ..., "p": ..., "k": ...},
        "ranked_mixed": [{"cosh_id", "name", "kg_product", "delivered":
                          {"n","p","k"}, "match_target"} ...],
        "enabled_straights": [_candidate_to_dict ...],
        "gap": {"n", "p", "k"},
        "cosh_skipped_count": int   # diagnostic; common_names lacking npk metadata
      }
    """
    await _assert_active_dealer(db, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    practice = (await db.execute(
        select(Practice).where(Practice.id == item.practice_id)
    )).scalar_one_or_none()
    if practice is None or practice.l2_type not in _NPK_L2_TYPES:
        return {
            "is_npk_practice": False,
            "fertigation": False,
            "required_dose": None,
            "ranked_mixed": [], "enabled_straights": [],
            "gap": None, "cosh_skipped_count": 0,
        }

    elements = (await db.execute(
        select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    by_type = {e.element_type: e for e in elements}

    def _val(et: str) -> float:
        e = by_type.get(et) or by_type.get(et.upper()) or by_type.get(et.lower())
        if e is None or e.value is None:
            return 0.0
        try:
            return float(e.value)
        except (TypeError, ValueError):
            return 0.0

    dose = Dose(n=_val("N_DOSAGE"), p=_val("P_DOSAGE"), k=_val("K_DOSAGE"))
    fertigation = practice.l2_type == "FERTIGATION_NPK_DOSAGES"

    # 2026-07-13 — Real fertigation multiplier (spec §5.2). Replaces
    # the pre-existing `by_type.get("applications")` lookup that
    # never matched a real Element (see helper docstring).
    applications_multiplier = 1
    if fertigation:
        applications_multiplier = await _fertigation_multiplier_from_practice(
            db, practice, by_type,
        )

    # Fertigation gate happens in the candidate loader (filters common
    # names down to those with a trade name in `npk_fertigation_products`).
    # `rank_mixed`'s `water_soluble_only` is then a no-op for this flow
    # since every candidate is already approved — but we keep it on for
    # belt-and-braces if a future loader stops gating.
    candidates, skipped = await load_fertiliser_candidates(
        db, fertigation=fertigation,
    )
    ranked = rank_mixed(
        candidates, dose, water_soluble_only=fertigation,
    )
    # Resolve the dealer's pick (if any) against the ranked list so the
    # gap is computed from the SAME MatchOutcome the dealer saw.
    picked = None
    if picked_mixed_cosh_id:
        for r in ranked:
            if r.candidate.cosh_id == picked_mixed_cosh_id:
                picked = r
                break

    gap = compute_gap_after_mixed(dose, picked)
    straights = npk_enabled_straights(
        gap, candidates, water_soluble_only=fertigation,
    )
    straights.sort(key=lambda c: c.name.lower())  # alphabetical per spec §3.1

    return {
        "is_npk_practice": True,
        "fertigation": fertigation,
        "applications_multiplier": applications_multiplier,
        "required_dose": {"n": dose.n, "p": dose.p, "k": dose.k},
        "ranked_mixed": [
            {
                "cosh_id": r.candidate.cosh_id,
                "name": r.candidate.name,
                "n": r.candidate.concentration.n,
                "p": r.candidate.concentration.p,
                "k": r.candidate.concentration.k,
                # `kg_product` is per-application for the ranking display.
                # `kg_product_total` is what the dealer actually buys.
                "kg_product": round(r.kg_product, 2),
                "kg_product_total": round(r.kg_product * applications_multiplier, 2),
                "delivered": {
                    "n": round(r.best.n_delivered, 2),
                    "p": round(r.best.p_delivered, 2),
                    "k": round(r.best.k_delivered, 2),
                },
                "match_target": r.best.target,
                "total_delivered": round(r.total_delivered, 2),
            }
            for r in ranked
        ],
        "enabled_straights": [_candidate_to_dict(c) for c in straights],
        "gap": {"n": gap.n, "p": gap.p, "k": gap.k},
        "cosh_skipped_count": skipped,
    }


# ── Dealer: NPK trade-name picker (Batch 30B) ─────────────────────────────────


@router.get("/dealer/orders/{order_id}/items/{item_id}/npk-trade-names")
async def get_npk_trade_names(
    order_id: str, item_id: str,
    common_name_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trade names available for a picked NPK common name.

    Chemical NPK    → walks tradename_commonname directly.
    Fertigation NPK → walks npk_fertigation_products (water-soluble pool).
    """
    await _assert_active_dealer(db, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    practice = (await db.execute(
        select(Practice).where(Practice.id == item.practice_id)
    )).scalar_one_or_none()
    if practice is None or practice.l2_type not in _NPK_L2_TYPES:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "NOT_AN_NPK_PRACTICE",
                "message": "This item is not an NPK dosage practice.",
            },
        )

    fertigation = practice.l2_type == "FERTIGATION_NPK_DOSAGES"
    lang = current_user.language_code or "en"
    if fertigation:
        rows = await trade_names_for_fertigation_npk(db, common_name_cosh_id, lang=lang)
    else:
        rows = await trade_names_for_chemical_npk(db, common_name_cosh_id, lang=lang)

    # Spec §3.1 — three-group layout (Recommended / My Brands / Other Brands).
    # NPK has no SE-recommended brand so Recommended is always empty; the PWA
    # hides empty sections. `trade_names` kept for backwards compatibility
    # (Batch 30B used it before grouping landed).
    grouped = await group_trade_names_for_dealer(db, rows, current_user.id)
    return {
        "common_name_cosh_id": common_name_cosh_id,
        "fertigation": fertigation,
        "trade_names": [
            {"cosh_id": tn_id, "name": name, "manufacturer_cosh_id": mfr}
            for tn_id, name, mfr in rows
        ],
        **grouped,
    }


# ── Dealer: NPK select — commit Mixed + Straight picks (Batch 30B) ────────────
#
# Spec §3: after the dealer picks a Mixed (optional) and up to 2
# Straights (per remaining gap), the brand selections are committed.
# Each pick becomes one OrderItem. All siblings share a synthesised
# `relation_id` with relation_type='AND' (spec §3.2: AND by default,
# no expert intervention). The original PENDING item is reused for
# the first pick; subsequent picks insert new OrderItem rows.

import uuid as _uuid_npk  # local alias — keep the top-of-file imports tidy


@router.post("/dealer/orders/{order_id}/items/{item_id}/npk-select")
async def npk_select(
    order_id: str, item_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Body shape:
      {
        "mixed":   null | {"common_name_cosh_id", "trade_name_cosh_id"},
        "straights": [
          {"target_nutrient": "N"|"P"|"K",
           "common_name_cosh_id", "trade_name_cosh_id"},
          ...
        ]
      }
    """
    await _assert_active_dealer(db, current_user.id)
    item = await _get_order_item(db, item_id, order_id)
    practice = (await db.execute(
        select(Practice).where(Practice.id == item.practice_id)
    )).scalar_one_or_none()
    if practice is None or practice.l2_type not in _NPK_L2_TYPES:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "NOT_AN_NPK_PRACTICE",
                    "message": "This item is not an NPK dosage practice."},
        )

    mixed = data.get("mixed")
    straights = data.get("straights") or []
    if not mixed and not straights:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "NPK_NO_SELECTION",
                    "message": "Provide a Mixed selection or at least one Straight."},
        )

    # Re-rank against the SE's dose so we compute kg_product exactly
    # the same way the dealer saw it. Refusing to trust client-supplied
    # kg keeps the math one place.
    elements = (await db.execute(
        select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    by_type = {e.element_type: e for e in elements}

    def _val(et: str) -> float:
        e = by_type.get(et) or by_type.get(et.upper()) or by_type.get(et.lower())
        if e is None or e.value is None:
            return 0.0
        try:
            return float(e.value)
        except (TypeError, ValueError):
            return 0.0

    dose = Dose(n=_val("N_DOSAGE"), p=_val("P_DOSAGE"), k=_val("K_DOSAGE"))
    fertigation = practice.l2_type == "FERTIGATION_NPK_DOSAGES"

    # 2026-07-13 — Real fertigation multiplier via
    # `_fertigation_multiplier_from_practice` (spec §5.2). Total
    # purchase = per-application dose × N applications across the
    # timeline. See helper docstring for the pre-existing lookup bug
    # this replaces.
    applications_multiplier = 1
    if fertigation:
        applications_multiplier = await _fertigation_multiplier_from_practice(
            db, practice, by_type,
        )

    candidates, _ = await load_fertiliser_candidates(db, fertigation=fertigation)
    by_cn = {c.cosh_id: c for c in candidates}
    ranked = rank_mixed(candidates, dose, water_soluble_only=fertigation)
    ranked_by_cn = {r.candidate.cosh_id: r for r in ranked}

    # 2026-07-13 — Dealer can override the auto-computed kg and enter
    # a price per pick. Both fields optional; when absent the ranking-
    # derived kg is used and price stays NULL (dealer can still tweak
    # via Edit Details post-commit). Values arrive as strings from JSON
    # via the PWA input; coerce defensively.
    def _opt_float(v) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # Build (common_name_cosh_id, trade_name_cosh_id, total_kg,
    # per_application_kg, price) tuples for every pick, in fixed
    # order: Mixed first, then Straights N, P, K.
    # 2026-07-13 — Split per-app kg from total kg. For fertigation
    # NPK the dealer procures TOTAL (per-app × N applications) but the
    # farmer's advisory displays PER-APP on each scheduled day per
    # spec §5.3 ("Apply 2 kg today"). Both get persisted:
    #   given_volume     = total kg (what dealer sold)
    #   estimated_volume = per-application kg (what farmer applies)
    # For chemical NPK (multiplier=1) they collapse to the same value.
    #
    # Dealer volume override applies to the TOTAL only — the per-app
    # dose stays at the SE-recommended per-application amount so a
    # dealer rounding up to pack sizes doesn't drift the farmer's
    # daily dose.
    picks: list[tuple[str, str, float, float, Optional[float]]] = []
    if mixed:
        cn_id = mixed.get("common_name_cosh_id")
        tn_id = mixed.get("trade_name_cosh_id")
        if not cn_id or not tn_id:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "NPK_INVALID_MIXED",
                        "message": "mixed needs common_name_cosh_id + trade_name_cosh_id"},
            )
        r = ranked_by_cn.get(cn_id)
        if r is None:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "NPK_MIXED_NOT_RANKED",
                        "message": f"Mixed {cn_id} not in the ranked list."},
            )
        per_app_kg = round(r.kg_product, 2)
        override_kg = _opt_float(mixed.get("given_volume"))
        total_kg = override_kg if override_kg is not None and override_kg > 0 \
            else round(per_app_kg * applications_multiplier, 2)
        picks.append((
            cn_id, tn_id, total_kg, per_app_kg,
            _opt_float(mixed.get("price")),
        ))

    # Gap after Mixed (if any). Straights are sized against this gap.
    picked_mixed_ranking = ranked_by_cn.get(mixed["common_name_cosh_id"]) if mixed else None
    gap = compute_gap_after_mixed(dose, picked_mixed_ranking)

    for s in straights:
        cn_id = s.get("common_name_cosh_id")
        tn_id = s.get("trade_name_cosh_id")
        target = s.get("target_nutrient")
        if not (cn_id and tn_id and target in ("N", "P", "K")):
            raise HTTPException(
                status_code=422,
                detail={"error_code": "NPK_INVALID_STRAIGHT",
                        "message": "straight needs common_name + trade_name + target_nutrient"},
            )
        cand = by_cn.get(cn_id)
        if cand is None:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "NPK_STRAIGHT_NOT_IN_POOL",
                        "message": f"Straight {cn_id} not in the candidate pool."},
            )
        kg = straight_kg_for_gap(cand.concentration, gap)
        if kg is None or kg <= 0:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "NPK_STRAIGHT_NO_GAP",
                        "message": f"No remaining gap for {target} — Straight {cn_id} not needed."},
            )
        per_app_kg = round(kg, 2)
        override_kg = _opt_float(s.get("given_volume"))
        total_kg = override_kg if override_kg is not None and override_kg > 0 \
            else round(per_app_kg * applications_multiplier, 2)
        picks.append((
            cn_id, tn_id, total_kg, per_app_kg,
            _opt_float(s.get("price")),
        ))

    # 2026-07-13 — Hard block: dealer must cover every non-zero
    # recommended nutrient (spec §2.3). Each Straight-X fully fills
    # its class's gap by construction (straight_kg_for_gap returns
    # exactly `gap.[nutrient]`). So the "not covered" set = every
    # nutrient with dose > 0, gap > 0 after Mixed, and no Straight
    # of that class picked. Fires 422 with which nutrients are
    # unfilled so the dealer can either add the missing Straight or
    # mark the whole item NOT_AVAILABLE.
    picked_straight_classes: set[str] = set()
    for s in straights:
        cn_id = s.get("common_name_cosh_id")
        if not cn_id or cn_id not in by_cn:
            continue
        cls = classify_fertiliser(by_cn[cn_id].concentration)
        if cls in ("STRAIGHT_N", "STRAIGHT_P", "STRAIGHT_K"):
            picked_straight_classes.add(cls)
    eps = 1e-6
    unfilled: list[str] = []
    if dose.n > eps and gap.n > eps and "STRAIGHT_N" not in picked_straight_classes:
        unfilled.append("N")
    if dose.p > eps and gap.p > eps and "STRAIGHT_P" not in picked_straight_classes:
        unfilled.append("P")
    if dose.k > eps and gap.k > eps and "STRAIGHT_K" not in picked_straight_classes:
        unfilled.append("K")
    if unfilled:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "NPK_UNFILLED_GAP",
                "message": (
                    "Recommendation not fully covered — "
                    + ", ".join(unfilled)
                    + " still needs a fertiliser. Fill the remaining gap or"
                    " mark this item unavailable."
                ),
                "unfilled": unfilled,
            },
        )

    # Validate each picked trade name actually belongs to its common name
    # (chemical chain or fertigation chain, depending on L2). Stops bogus
    # client-supplied trade_name_cosh_ids slipping through.
    for cn_id, tn_id, _total, _per_app, _price in picks:
        rows = (
            await trade_names_for_fertigation_npk(db, cn_id)
            if fertigation
            else await trade_names_for_chemical_npk(db, cn_id)
        )
        allowed = {t for t, _, _ in rows}
        if tn_id not in allowed:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "NPK_TRADE_NAME_NOT_IN_POOL",
                        "message": f"Trade name {tn_id} not approved for common name {cn_id}."},
            )

    # 2026-07-13 — Archive orphans from a previous /npk-select call for
    # this practice. Each call reuses the passed-in `item` for picks[0]
    # but creates fresh OrderItems for picks[1..N] with a NEW relation_id.
    # On re-submit (dealer taps "Edit details" / re-runs the flow), the
    # PREVIOUS submit's sibling items become orphans — their relation_id
    # no longer matches the reused item's — and stay in the DB as
    # duplicate AVAILABLE rows. Farmer/dealer both then see the same
    # brand twice, once in the new AND group and once standalone.
    # Fix: soft-archive any OTHER OrderItem for this practice on this
    # order that's still in a dealer-editable state (PENDING / AVAILABLE).
    # Items that have moved past the dealer's hands (SENT_FOR_APPROVAL /
    # APPROVED / POSTPONED / NOT_AVAILABLE / REJECTED) stay untouched —
    # re-selection shouldn't rewrite the farmer's decisions.
    from datetime import datetime as _dt_orphan, timezone as _tz_orphan
    orphans = (await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order_id,
            OrderItem.practice_id == item.practice_id,
            OrderItem.id != item.id,
            OrderItem.archived_at.is_(None),
            OrderItem.status.in_(
                [OrderItemStatus.PENDING, OrderItemStatus.AVAILABLE]
            ),
        )
    )).scalars().all()
    now_utc = _dt_orphan.now(_tz_orphan.utc)
    for orphan in orphans:
        orphan.archived_at = now_utc

    # AND-relation glue. The synthesised relation_id ties all picks
    # together; per spec §3.2 it's automatic — no expert involvement.
    relation_id = str(_uuid_npk.uuid4())
    created_items: list[str] = []
    for i, (cn_id, tn_id, kg_total, kg_per_app, price_val) in enumerate(picks):
        # First pick reuses the original PENDING item; subsequent picks
        # spawn new OrderItem rows on the same practice_id.
        target_item = item if i == 0 else OrderItem(
            order_id=order_id,
            practice_id=item.practice_id,
            timeline_id=item.timeline_id,
        )
        target_item.brand_cosh_id = tn_id
        # The brand_name field carries the EN trade name; resolve quickly
        # from the cores table so the dealer order detail renders it.
        tn_core = (await db.execute(
            select(_NPKCoshCoreItem).where(_NPKCoshCoreItem.cosh_id == tn_id)
        )).scalar_one_or_none()
        if tn_core is not None:
            target_item.brand_name = (tn_core.translations or {}).get("en") or tn_id
        # given_volume = total across the timeline (dealer procures);
        # estimated_volume = per-application dose (farmer applies each
        # scheduled day). For chemical NPK both collapse to the same
        # value because the multiplier is 1.
        target_item.given_volume = kg_total
        target_item.estimated_volume = kg_per_app
        # 2026-07-13 — Dealer-entered price flows through from the NPK
        # form. Optional; NULL when not entered (dealer can fill via
        # Edit Details later).
        target_item.price = price_val
        # NPK quantities are kg of product per spec §1.1; pin the unit.
        target_item.volume_unit = "kg"
        target_item.status = OrderItemStatus.AVAILABLE
        target_item.relation_id = relation_id
        target_item.relation_type = "AND"
        # All picks in a single Part/Option (compound AND), positions 1..N.
        target_item.relation_role = f"PART_1__OPT_1__POS_{i + 1}"
        if i > 0:
            db.add(target_item)
        await db.flush()
        created_items.append(target_item.id)

        await _record_event(
            db, lineage_id=target_item.lineage_id,
            event_type="MARKED_AVAILABLE",
            actor_user_id=current_user.id, actor_role="DEALER",
            order_id=order_id, order_item_id=target_item.id,
            prev_status=OrderItemStatus.PENDING.value,
            new_status=OrderItemStatus.AVAILABLE.value,
            metadata={
                "brand_cosh_id": tn_id,
                "common_name_cosh_id": cn_id,
                "given_volume": kg_total,
                "per_application_volume": kg_per_app,
                "price": price_val,
                "volume_unit": "kg",
                "npk_relation_id": relation_id,
            },
        )

    await db.commit()
    return {
        "relation_id": relation_id,
        "item_ids": created_items,
        "picks": [
            {
                "common_name_cosh_id": cn_id,
                "trade_name_cosh_id": tn_id,
                "kg_product": kg_total,
                "kg_per_application": kg_per_app,
                "price": price_val,
            }
            for cn_id, tn_id, kg_total, kg_per_app, price_val in picks
        ],
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

    # ── Subscription + farm area ───────────────────────────────────
    # 2026-06-20 — Subscription is now loaded unconditionally (was
    # lazy-loaded only for the farm-area branch) because it's also
    # needed as the package fallback for CHA-triggered timelines
    # (DAYS_AFTER_DETECTION), which have `package_id = None` by
    # design — those practices belong to a diagnosis recommendation,
    # not to a package's CCA. The sub's own package always has
    # `crop_cosh_id` set, so the crop-measure lookup downstream can
    # resolve cleanly even on CHA / SP / QA practices.
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == (
            select(Order.subscription_id).where(Order.id == order_id).scalar_subquery()
        ))
    )).scalar_one_or_none()
    if farm_area_acres is None and sub:
        farm_area_acres = float(sub.farm_area_acres) if sub.farm_area_acres else None
    # Farm-area is no longer a hard gate — plant-wise crops bind `Count`
    # via the timeline's TriggeredCHAEntry (CHA) or sub.number_of_plants
    # (CCA) instead. The downstream calculate_volume() decides whether
    # `Total_area` or `Count` is the required input based on the
    # formula text.

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

    # ── Plant count resolution (2026-06-30) ────────────────────────
    # For plant-wise crops, BL-06's `Count` variable binds the
    # treatable plant count. Rules:
    #   • CHA timeline (sp/pg/qa rec set): look up the most recent
    #     active TriggeredCHAEntry for this subscription + recommendation
    #     and use its `affected_plants_count`. NULL on QA paths where
    #     the farmer didn't fill the optional field at query submission.
    #   • CCA timeline (none of the rec fields set): use the farmer's
    #     declared total (`sub.number_of_plants`). CCA treatments are
    #     scheduled across the whole crop; the damaged-plants principle
    #     only kicks in when a pest has been diagnosed.
    #   • Area-wise crops: stays None; formula uses Total_area.
    plant_count: Optional[int] = None
    rec_id = (
        timeline.sp_recommendation_id
        or timeline.pg_recommendation_id
        or timeline.standard_response_id
    )
    if rec_id and sub:
        from app.modules.subscriptions.models import TriggeredCHAEntry
        cha_entry = (await db.execute(
            select(TriggeredCHAEntry).where(
                TriggeredCHAEntry.subscription_id == sub.id,
                TriggeredCHAEntry.recommendation_id == rec_id,
                TriggeredCHAEntry.status == "ACTIVE",
            ).order_by(TriggeredCHAEntry.triggered_at.desc())
        )).scalars().first()
        if cha_entry:
            plant_count = cha_entry.affected_plants_count
    elif sub and sub.number_of_plants:
        # CCA on a plant-wise crop — full orchard treatment.
        plant_count = int(sub.number_of_plants)

    # Try the timeline's own package first (CCA case). If null
    # (CHA / SP / QA timeline), fall back to the subscription's
    # package — every active subscription has one.
    pkg_id = timeline.package_id or (sub.package_id if sub else None)
    package = (await db.execute(
        select(Package).where(Package.id == pkg_id)
    )).scalar_one_or_none() if pkg_id else None
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
    # Fix 2026-06-01 — modern authoring stores element_type as
    # APPLICATION_METHOD (uppercase) while the legacy path was
    # "application_method". Index by lowercase so a single .get works
    # regardless of which authoring shape produced the row.
    elements_by_type = {
        (e.element_type or "").lower(): e for e in elements_rows
    }

    dosage_el = elements_by_type.get("dosage")
    dosage = float(dosage_el.value) if dosage_el and dosage_el.value else None

    # Fix 2026-06-01 — in the modern Cosh authoring, application_method
    # is stored as a cosh_ref to a Core row, not a free-text value. The
    # 304 BL-06 formulas key on the English name ("Foliar Spray"), so
    # resolve the cosh_ref via translations.en. Same for dosage_unit.
    from app.modules.sync.models import CoshCoreItem as _BL06CoshCore
    method_el = elements_by_type.get("application_method")
    application_method: Optional[str] = None
    if method_el:
        if method_el.value:
            application_method = method_el.value
        elif method_el.cosh_ref:
            core = (await db.execute(
                select(_BL06CoshCore).where(_BL06CoshCore.cosh_id == method_el.cosh_ref)
            )).scalar_one_or_none()
            if core:
                application_method = (core.translations or {}).get("en")
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
    if not dosage_unit:
        # Dosage unit lives on a separate DOSAGE_UNIT element in the
        # modern Cosh-driven authoring (cosh_ref → dosage_unit Core),
        # not on dosage_el.unit_cosh_id. Resolve via the same EN-name
        # path application_method uses above so the formula lookup
        # gets "ml/L", not a UUID.
        dosage_unit_el = elements_by_type.get("dosage_unit")
        if dosage_unit_el:
            if dosage_unit_el.cosh_ref:
                core = (await db.execute(
                    select(_BL06CoshCore).where(_BL06CoshCore.cosh_id == dosage_unit_el.cosh_ref)
                )).scalar_one_or_none()
                if core:
                    dosage_unit = (core.translations or {}).get("en")
            if not dosage_unit and dosage_unit_el.value:
                dosage_unit = dosage_unit_el.value
        if not dosage_unit and dosage_el and dosage_el.unit_cosh_id:
            # Legacy fallback — dosage element's own unit_cosh_id may
            # hold either a cosh_id (resolve to EN) or free text (use
            # directly).
            legacy_core = (await db.execute(
                select(_BL06CoshCore).where(_BL06CoshCore.cosh_id == dosage_el.unit_cosh_id)
            )).scalar_one_or_none()
            dosage_unit = (
                (legacy_core.translations or {}).get("en")
                if legacy_core else dosage_el.unit_cosh_id
            )

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
    # Fix 2026-06-01: " of water" is descriptive ("ml/L of water" ==
    # "ml/L" for formula purposes). Cosh's units_data Core ships both
    # forms; the SE picks whichever read naturally. Try the verbatim
    # match first, fall back to the suffix-stripped form.
    dosage_unit_alt = dosage_unit
    if " of water" in dosage_unit:
        dosage_unit_alt = dosage_unit.replace(" of water", "")

    formulas = (await db.execute(
        select(VolumeFormula).where(
            VolumeFormula.measure == measure,
            VolumeFormula.l2_practice == practice.l2_type,
            VolumeFormula.application_method == application_method,
            VolumeFormula.brand_unit == brand_unit,
            VolumeFormula.dosage_unit.in_([dosage_unit, dosage_unit_alt]),
            VolumeFormula.status == "ACTIVE",
        )
    )).scalars().all()

    if not formulas:
        # Heuristic: pair-mismatch detection. If brand_unit is solid (g/kg)
        # but dosage_unit is volumetric (ml/L, ppm/L, mg/L), the SE's
        # dosage doesn't make physical sense for this brand. Same in
        # reverse. We surface a targeted message so the diagnosis is
        # obvious — the formula table doesn't carry rows for
        # incompatible pairs, and silently saying "no formula" leaves
        # the dealer guessing.
        bu = (brand_unit or "").lower()
        du_norm = (dosage_unit_alt or "").lower()
        solid_brand = bu in ("g", "kg")
        liquid_brand = bu in ("ml", "l")
        volumetric_dose = "/l" in du_norm and not du_norm.startswith("g/") and not du_norm.startswith("mg")
        mass_dose = du_norm.startswith("g/") or du_norm.startswith("mg/")
        pair_mismatch = (
            (solid_brand and volumetric_dose) or (liquid_brand and mass_dose)
        )
        if pair_mismatch:
            return {
                "estimated_volume": None, "volume_unit": None,
                "message": (
                    f"Brand unit '{brand_unit}' and dosage unit "
                    f"'{dosage_unit}' don't match — check the practice "
                    "or pick a different brand."
                ),
                "error_code": "UNIT_PAIR_MISMATCH",
                "lookup_key": {
                    "measure": measure, "l2_practice": practice.l2_type,
                    "application_method": application_method,
                    "brand_unit": brand_unit, "dosage_unit": dosage_unit,
                },
            }
        return {
            "estimated_volume": None, "volume_unit": None,
            "message": (
                f"No formula found for measure={measure}, l2={practice.l2_type}, "
                f"method={application_method}, brand_unit={brand_unit}, "
                f"dosage_unit={dosage_unit}. (DATA_CONFIG_ERROR)"
            ),
            "error_code": "FORMULA_NOT_FOUND",
            "lookup_key": {
                "measure": measure, "l2_practice": practice.l2_type,
                "application_method": application_method,
                "brand_unit": brand_unit, "dosage_unit": dosage_unit,
            },
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

    # 2026-07-13 — Vol_per_plant reads from the VOLUME_PER_PLANT element
    # on the practice. Present on every plant-wise input L2 that ships a
    # per-plant dose.
    #
    # Unit normalisation (2026-07-13 evening): the seeded plant-wise
    # formulas were written assuming Vol_per_plant is in LITRES
    # (mirroring the AREA_WISE convention where `Volume_water=150`
    # is spray-solution litres per acre). But SEs enter physically-
    # realistic values in millilitres — a coconut palm gets ~1.5 mL
    # of spray solution per plant, not 1.5 L. Without normalisation
    # the estimate comes out 1000× too high — reported 2026-07-13 on
    # RT-26-000169 · Sun Bio Verbatim (got 1050 ml, correct is
    # 1.05 ml). Fix: resolve VOLUME_PER_PLANT_UNIT via Cosh core and
    # divide by 1000 when the unit is millilitres (ml/plant, mL/plant).
    # Non-volume units (g/plant, kg/plant) pass through as-is because
    # those formulas — Direct Soil Application etc — reuse
    # Vol_per_plant to mean "mass per plant" and expect the raw value.
    vol_per_plant_el = elements_by_type.get("volume_per_plant")
    vol_per_plant: Optional[float] = None
    if vol_per_plant_el and vol_per_plant_el.value:
        try:
            raw_vpp = float(vol_per_plant_el.value)
        except (TypeError, ValueError):
            raw_vpp = None
        if raw_vpp is not None:
            vpu_el = elements_by_type.get("volume_per_plant_unit")
            vpu_name: Optional[str] = None
            if vpu_el and vpu_el.cosh_ref:
                vpu_core = (await db.execute(
                    select(_BL06CoshCore).where(_BL06CoshCore.cosh_id == vpu_el.cosh_ref)
                )).scalar_one_or_none()
                if vpu_core:
                    vpu_name = (vpu_core.translations or {}).get("en")
            # Case-insensitive prefix match on the resolved English
            # unit name. "ml/plant", "mL/plant" → factor 1/1000
            # (millilitres → litres). Everything else passes through.
            factor = 1.0
            if vpu_name:
                n = vpu_name.strip().lower()
                if n.startswith("ml"):
                    factor = 1.0 / 1000.0
            vol_per_plant = raw_vpp * factor

    result = calculate_volume(
        formula=formula_row.formula,
        brand_unit=formula_row.brand_unit,
        dosage=dosage,
        farm_area_acres=farm_area_acres,
        frequency_days=practice.frequency_days,
        timeline_duration_days=timeline_duration_days,
        applications=applications,
        plant_count=plant_count,
        vol_per_plant=vol_per_plant,
    )
    if result is None:
        # Targeted message for the common plant-wise-CHA-without-count
        # case (QA path where the farmer left the optional field blank).
        # 2026-07-13 — Cover both legacy `Count` and the reference-doc
        # `Total_No_of_plants` variable names.
        needs_plant_count = any(
            var in (formula_row.formula or "")
            for var in ("Count", "Total_No_of_plants")
        )
        if measure == "PLANT_WISE" and needs_plant_count and plant_count is None:
            return {
                "estimated_volume": None, "volume_unit": None,
                "message": (
                    "Please check with the farmer how many plants are "
                    "affected, then enter the volume manually."
                ),
                "error_code": "PLANT_COUNT_MISSING",
            }
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

    # Pull active manufacturer rows in one query so we get both the
    # English baseline AND the full translations dict per cosh_id —
    # the materialised cache mirrors both so reads can localise without
    # an extra JOIN. (Was: `_resolve_names` which returns name-only.)
    from app.modules.sync.models import CoshCoreItem
    rows = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id.in_(mfr_ids),
            CoshCoreItem.core_type == COSH_INPUT_MANUFACTURERS_CORE,
            CoshCoreItem.status == "active",
        )
    )).scalars().all()
    out: dict[str, dict] = {}
    for r in rows:
        tr = r.translations or {}
        en_name = tr.get("en") or tr.get("English") or r.cosh_id
        out[r.cosh_id] = {"name": en_name, "translations": tr}
    return out


async def _rebuild_manufacturer_catalog(
    db: AsyncSession, *, only_category: str | None = None,
) -> int:
    """Truncate-and-reload the materialised catalog. Returns the
    total number of rows written. Pass `only_category` to refresh
    just one half (the other half's rows are untouched).

    2026-06-12 — Now mirrors the per-cosh_id translations dict alongside
    the English name so the /dealer/manufacturers-catalog endpoint can
    render in the dealer's chosen language. English column stays as
    the audit-trail fallback for any caller that doesn't yet thread a
    locale."""
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
        for cosh_id, entry in mfrs.items():
            db.add(DealerManufacturerCatalog(
                category=cat, manufacturer_cosh_id=cosh_id,
                manufacturer_name=entry["name"],
                manufacturer_translations=entry["translations"],
                refreshed_at=now,
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
    # 2026-06-12 — Surface the dealer's chosen language. Translations
    # column populated at refresh time mirrors cosh_core_items.
    # translations; null on rows refreshed before the migration ran,
    # in which case pick_translation falls through to manufacturer_name.
    lang = current_user.language_code or "en"
    return [
        {
            "cosh_id": r.manufacturer_cosh_id,
            "name": pick_translation(
                r.manufacturer_translations or {}, lang, r.manufacturer_name,
            ),
        }
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


@router.post("/admin/brand-cache/refresh")
async def admin_refresh_brand_cache(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Force-rebuild `brand_lookup_cache` from Cosh.

    Call after a Cosh sync that adds, renames, or retires trade names
    / manufacturers / formulations. Cache is also lazy-bootstrapped on
    first read for a common name when the cache is entirely empty, so
    a fresh deploy works without an explicit refresh. SA-or-CM only.
    """
    from app.modules.advisory.router import _assert_sa_or_cm
    from app.services.brand_cache import rebuild_brand_cache
    await _assert_sa_or_cm(db, current_user)
    written = await rebuild_brand_cache(db)
    return {"rows_written": written}


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
    # 2026-08-17 — Per-batch rework: allow delete when ANY batch has
    # been shared (dealer's "I'm done" signal on at least one round).
    pl = (await db.execute(
        select(PackingList)
        .where(
            PackingList.order_id == order_id,
            PackingList.first_shared_at.isnot(None),
        )
        .order_by(PackingList.approval_round.asc().nulls_first())
        .limit(1)
    )).scalar_one_or_none()
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
    return await _promoted_farmers(db, current_user.id, current_user.language_code or "en")


@router.get("/facilitator/promoted-farmers")
async def facilitator_promoted_farmers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_active_facilitator(db, current_user.id)
    return await _promoted_farmers(db, current_user.id, current_user.language_code or "en")


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
    # 2026-07-03 — Post-commit consistency guard. Rohan on Testorg
    # ended up with req_status=ACCEPTED but is_promoter=False after
    # a single accept call — a state no current code path should
    # produce. Log a warning if the flush somehow desynced the two
    # fields so we can spot recurrences.
    if bool(cp.is_promoter) is not True or cp.promoter_request_status != "ACCEPTED":
        import logging
        logging.getLogger(__name__).warning(
            "facilitator_accept post-commit state inconsistent: "
            "cp_id=%s is_promoter=%s req_status=%s user_id=%s",
            cp.id, cp.is_promoter, cp.promoter_request_status,
            current_user.id,
        )
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


async def _pending_stepdown_counts(
    db: AsyncSession, promoter_user_id: str, client_id: str,
) -> dict:
    """Snapshot of what's actually PROMOTER-scoped for this user at
    this client when they request stepdown. Powers the email body and
    the per-row badge on the CA Promoters list.

    Deliberately narrow (2026-08-10 correction): only items tied to
    the Promoter sub-role. Orders being facilitated and pending
    SubscriptionPaymentRequests are Facilitator-side work and continue
    regardless of stepdown outcome — counting them here would scare
    the CA into thinking there's handoff to arrange when there isn't.

    Fields:
      unassigned_units        — PromoterAllocation.units_balance held
                                by this promoter at this client
                                (reclaimed at CA-side approve)
      pending_assignments     — PromoterAssignment rows this promoter
                                sent that are still awaiting the
                                farmer's response (PENDING_FARMER_APPROVAL).
                                They become moot on stepdown — farmers
                                who haven't responded won't get this
                                promoter's onboarding if reassigned.
    """
    from app.modules.subscriptions.models import (
        PromoterAssignment, AssignmentStatus, Subscription,
    )
    from app.modules.subscriptions.promoter_allocation_models import (
        PromoterAllocation,
    )

    alloc = (await db.execute(
        select(PromoterAllocation.units_balance).where(
            PromoterAllocation.client_id == client_id,
            PromoterAllocation.promoter_user_id == promoter_user_id,
        )
    )).scalar_one_or_none()
    unassigned_units = int(alloc or 0)

    pending_assignments = (await db.execute(
        select(func.count(PromoterAssignment.id))
        .join(Subscription, Subscription.id == PromoterAssignment.subscription_id)
        .where(
            PromoterAssignment.promoter_user_id == promoter_user_id,
            PromoterAssignment.status == AssignmentStatus.PENDING_FARMER_APPROVAL,
            Subscription.client_id == client_id,
        )
    )).scalar_one() or 0

    return {
        "unassigned_units":     unassigned_units,
        "pending_assignments":  int(pending_assignments),
    }


async def _notify_ca_and_fms_of_stepdown_request(
    db: AsyncSession, cp, promoter_user, counts: dict,
) -> None:
    """Send the stepdown-request email to CA + every active Field
    Manager for the client. Called by both the F-P and Dealer
    stepdown endpoints. Non-fatal on delivery failure — the request
    itself is committed even if email delivery hiccups; the CA still
    sees the badge on their Promoters page.
    """
    from app.modules.clients.models import Client, ClientUser, ClientUserRole
    from app.modules.platform.models import StatusEnum
    from app.modules.clients.service import send_promoter_stepdown_request_email
    from app.config import settings as _settings

    client = (await db.execute(
        select(Client).where(Client.id == cp.client_id)
    )).scalar_one_or_none()
    if client is None:
        return

    fm_emails: set[str] = set()
    fm_rows = (await db.execute(
        select(User.email).select_from(ClientUser).join(
            User, User.id == ClientUser.user_id,
        ).where(
            ClientUser.client_id == cp.client_id,
            ClientUser.role == ClientUserRole.FIELD_MANAGER,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalars().all()
    for email in fm_rows:
        if email:
            fm_emails.add(email)

    recipients = list(fm_emails)
    if client.ca_email and client.ca_email not in fm_emails:
        recipients.insert(0, client.ca_email)

    if not recipients:
        return

    # 2026-08-10 — Deep-link to /promoters 404s for the recipient
    # because the CA portal requires an authenticated session at the
    # right client's short-name path. Send them to the branded login
    # instead; they navigate to Promoters after signing in.
    base = (_settings.frontend_base_url or '').rstrip('/')
    login_url = f"{base}/login/{client.short_name}" if client.short_name else base

    await send_promoter_stepdown_request_email(
        recipients=recipients,
        promoter_name=(promoter_user.name or promoter_user.phone or "A promoter"),
        promoter_type=cp.promoter_type,
        client_display_name=(client.display_name or client.full_name),
        login_url=login_url,
        unassigned_units=counts["unassigned_units"],
        pending_assignments=counts["pending_assignments"],
    )


async def _step_down_promoter_common(
    db: AsyncSession,
    current_user: User,
    client_promoter_id: str,
    promoter_type: str,   # DEALER | FACILITATOR
) -> dict:
    """Shared body for F-P + Dealer stepdown.

    Stepdown is a REQUEST, not a self-completion (2026-08-10). We
    flip `promoter_request_status='STEPDOWN_REQUESTED'` and stamp the
    timestamp; `is_promoter` STAYS True until the CA/FM approves via
    the CA-side revoke endpoint (which flips it False + reclaims
    allocation units). This preserves in-flight work while blocking
    new promoter-side actions (see per-endpoint guards) and gives
    the company control over the exit.
    """
    from app.modules.clients.models import ClientPromoter

    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.id == client_promoter_id,
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.promoter_type == promoter_type,
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
    if cp.promoter_request_status == "STEPDOWN_REQUESTED":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stepdown_already_requested",
                "message": (
                    "You have already requested to step down. The company is "
                    "reviewing your request."
                ),
            },
        )

    from datetime import datetime, timezone
    cp.promoter_request_status = "STEPDOWN_REQUESTED"
    cp.promoter_request_responded_at = datetime.now(timezone.utc)
    # `is_promoter` intentionally stays True — the CA/FM revoke is
    # what flips it to False + reclaims allocation units.
    await db.commit()
    await db.refresh(cp)

    # Fire notification AFTER the commit so the request is persisted
    # even if email delivery is flaky. Wrapped in try to keep the
    # response 200 on transient SMTP issues — the CA sees the badge
    # in the portal regardless.
    try:
        counts = await _pending_stepdown_counts(db, cp.user_id, cp.client_id)
        await _notify_ca_and_fms_of_stepdown_request(db, cp, current_user, counts)
    except Exception as exc:   # noqa: BLE001
        _orders_logger.warning(f"stepdown-request email failed for cp={cp.id}: {exc}")

    return {
        "id": cp.id,
        "is_promoter": cp.is_promoter,
        "promoter_request_status": cp.promoter_request_status,
        "promoter_request_responded_at": cp.promoter_request_responded_at,
    }


@router.put("/facilitator/promoter-status/{client_promoter_id}/step-down")
async def facilitator_step_down_promoter(
    client_promoter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator-side stepdown request. See `_step_down_promoter_common`
    for the shared semantics (stepdown-as-request pattern, 2026-08-10)."""
    return await _step_down_promoter_common(
        db, current_user, client_promoter_id, "FACILITATOR",
    )


@router.put("/dealer/promoter-status/{client_promoter_id}/step-down")
async def dealer_step_down_promoter(
    client_promoter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer-side stepdown request. See `_step_down_promoter_common`
    for the shared semantics. Dealers are multi-company per §11.2 —
    stepping down from one company doesn't touch any other Promoter
    row the dealer holds."""
    return await _step_down_promoter_common(
        db, current_user, client_promoter_id, "DEALER",
    )


@router.get("/dealer/onboarding-clients")
async def dealer_onboarding_clients(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List of Clients that have onboarded the caller as a Dealer.

    Mirror of /facilitator/onboarding-clients with two differences:
    - Filters on promoter_type=DEALER.
    - Drops `is_promoter_pundit` from the payload — dealers cannot be
      Promoter-Pundits (PP is facilitator-only).

    Deliberately *not* gated on `_assert_active_dealer` — a Dealer who
    has been deactivated (or never been onboarded) should see an empty
    list, not a 403."""
    from app.modules.clients.models import Client, ClientPromoter

    rows = (await db.execute(
        select(ClientPromoter, Client)
        .join(Client, Client.id == ClientPromoter.client_id)
        .where(
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.promoter_type == "DEALER",
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
            "promoter_request_status": cp.promoter_request_status,
            "website": c.website,
            "phone": c.support_phone or c.office_phone,
            "onboarded_at": cp.registered_at,
        }
        for cp, c in rows
    ]


# ── Dealer-side Promoter invitations (2026-06-23) ─────────────────────────────
# Mirror of the facilitator endpoints above. Dealers were auto-accepted
# pre-2026-06-23; now they require explicit A/R like facilitators do.
# Per §11.2 dealers remain multi-company Promoters — no exclusivity
# check on accept, unlike facilitators.

@router.get("/dealer/promoter-invitations")
async def dealer_promoter_invitations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List this Dealer's outstanding Promoter invitations —
    ClientPromoter rows in `PENDING` state — with enough Client
    branding to render an accept/decline card per invitation."""
    from app.modules.clients.models import Client, ClientPromoter

    rows = (await db.execute(
        select(ClientPromoter, Client)
        .join(Client, Client.id == ClientPromoter.client_id)
        .where(
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.promoter_type == "DEALER",
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


@router.put("/dealer/promoter-invitations/{client_promoter_id}/accept")
async def dealer_accept_promoter_invitation(
    client_promoter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer-side accept. Transitions PENDING → ACCEPTED, sets
    `is_promoter=True`, stamps `promoter_request_responded_at`.

    Unlike the facilitator endpoint, NO exclusivity check —
    dealers are multi-company Promoters per §11.2 (2026-06-23 user
    confirmation: "I confirm that dealers are multi-company
    promoters")."""
    from app.modules.clients.models import ClientPromoter

    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.id == client_promoter_id,
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.promoter_type == "DEALER",
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

    from datetime import datetime, timezone
    cp.is_promoter = True
    cp.promoter_request_status = "ACCEPTED"
    cp.promoter_request_responded_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(cp)
    # 2026-07-03 — Same consistency guard as the facilitator accept.
    if bool(cp.is_promoter) is not True or cp.promoter_request_status != "ACCEPTED":
        import logging
        logging.getLogger(__name__).warning(
            "dealer_accept post-commit state inconsistent: "
            "cp_id=%s is_promoter=%s req_status=%s user_id=%s",
            cp.id, cp.is_promoter, cp.promoter_request_status,
            current_user.id,
        )
    return {
        "id": cp.id,
        "client_id": cp.client_id,
        "is_promoter": cp.is_promoter,
        "promoter_request_status": cp.promoter_request_status,
        "promoter_request_responded_at": cp.promoter_request_responded_at,
    }


@router.put("/dealer/promoter-invitations/{client_promoter_id}/decline")
async def dealer_decline_promoter_invitation(
    client_promoter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer-side decline. Transitions PENDING → DECLINED.
    `is_promoter` stays False."""
    from app.modules.clients.models import ClientPromoter

    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.id == client_promoter_id,
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.promoter_type == "DEALER",
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

    from datetime import datetime, timezone
    cp.promoter_request_status = "DECLINED"
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
            "promoter_request_status": cp.promoter_request_status,
            # 2026-06-23 — PP designation lives on the ClientPromoter
            # row (FM-side flag). True means the Facilitator is acting
            # as Promoter-Pundit at this client.
            "is_promoter_pundit": cp.is_promoter_pundit,
            # 2026-06-23 — contact + website surfaced so the
            # /facilitator/onboarded-companies page can render Call +
            # Website icons per company row.
            "website": c.website,
            "phone": c.support_phone or c.office_phone,
            "onboarded_at": cp.registered_at,
        }
        for cp, c in rows
    ]


async def _promoted_farmers(db, promoter_user_id: str, lang: str = "en"):
    """List of farmers the promoter (dealer or facilitator) actively
    serves. 2026-06-23 — enriched for the card redesign: farmer photo
    so the promoter recognises who they're calling, crop name resolved
    from the package's crop_cosh_id, and area/plant context so the
    promoter has the farmer's growing scale on hand."""
    result = await db.execute(
        select(PromoterAssignment).where(
            PromoterAssignment.promoter_user_id == promoter_user_id,
            PromoterAssignment.status == AssignmentStatus.ACTIVE,
        )
    )
    assignments = result.scalars().all()
    if not assignments:
        return []

    sub_ids = [a.subscription_id for a in assignments]
    sub_rows = (await db.execute(
        select(Subscription).where(Subscription.id.in_(sub_ids))
    )).scalars().all()
    sub_by_id = {s.id: s for s in sub_rows}

    farmer_ids = {s.farmer_user_id for s in sub_rows}
    farmer_rows = (await db.execute(
        select(User).where(User.id.in_(farmer_ids))
    )).scalars().all() if farmer_ids else []
    farmer_by_id = {u.id: u for u in farmer_rows}

    pkg_ids = {s.package_id for s in sub_rows if s.package_id}
    pkg_by_id: dict[str, "Package"] = {}
    if pkg_ids:
        prows = (await db.execute(
            select(Package).where(Package.id.in_(pkg_ids))
        )).scalars().all()
        pkg_by_id = {p.id: p for p in prows}

    crop_cosh_ids = {p.crop_cosh_id for p in pkg_by_id.values() if p.crop_cosh_id}
    crop_name_by_cosh_id: dict[str, str] = {}
    if crop_cosh_ids:
        from app.modules.sync.models import CoshCoreItem
        crows = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(crop_cosh_ids))
        )).all()
        for cid, tr in crows:
            if isinstance(tr, dict):
                name = pick_translation(tr, lang, "")
                if name:
                    crop_name_by_cosh_id[cid] = name

    out = []
    for a in assignments:
        sub = sub_by_id.get(a.subscription_id)
        if not sub:
            continue
        farmer = farmer_by_id.get(sub.farmer_user_id)
        pkg = pkg_by_id.get(sub.package_id) if sub.package_id else None
        crop_name = (
            crop_name_by_cosh_id.get(pkg.crop_cosh_id)
            if (pkg and pkg.crop_cosh_id) else None
        )
        out.append({
            "subscription_id": sub.id,
            "farmer_user_id": sub.farmer_user_id,
            "farmer_name": farmer.name if farmer else None,
            "farmer_phone": farmer.phone if farmer else None,
            "farmer_photo_url": farmer.photo_url if farmer else None,
            "client_id": sub.client_id,
            "package_id": sub.package_id,
            "status": sub.status,
            "reference_number": sub.reference_number,
            "crop_start_date": sub.crop_start_date,
            # 2026-06-23 — Card enrichment fields.
            "crop_cosh_id": pkg.crop_cosh_id if pkg else None,
            "crop_name": crop_name,
            "farm_area_acres": float(sub.farm_area_acres) if sub.farm_area_acres else None,
            "area_unit": sub.area_unit,
            "number_of_plants": sub.number_of_plants,
            "planting_year": sub.planting_year,
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
    # Push farmer so they know the facilitator picked up their order.
    farmer = (await db.execute(
        select(User).where(User.id == order.farmer_user_id)
    )).scalar_one_or_none()
    if farmer and farmer.fcm_token:
        try:
            await send_fcm(
                token=farmer.fcm_token,
                title=FACILITATOR_ACCEPT_FARMER_FCM_TITLE,
                body=_fmt_order_body(FACILITATOR_ACCEPT_FARMER_FCM_BODY_TPL, order),
                data={
                    "type": "ORDER_ACCEPTED_BY_FACILITATOR",
                    "order_id": order.id,
                    "click_action": f"/crop-detail/{order.subscription_id}/orders",
                },
            )
        except Exception as e:
            _orders_logger.error(
                f"FCM send raised unexpectedly for farmer {farmer.id}: {e}"
            )
    return {"order_id": order_id, "status": order.status}


@router.put("/facilitator/orders/{order_id}/reject")
async def facilitator_reject_order(
    order_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator declines a fresh order (Phase 2 rework, 2026-08-14).
    Model B DRAFT flow unwound — source flagged returned-to-farmer,
    items released in place, no new order created.

    Facilitator loses visibility (their Routed / Pending pill filters
    on `!is_returned_to_farmer && facilitator_user_id == me`; flag
    flip drops the card from their queue). Farmer's Routed card picks
    up in "Returned" state with Send / Discard buttons.
    """
    await _assert_active_facilitator(db, current_user.id)
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.facilitator_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != OrderStatus.SENT:
        raise HTTPException(status_code=400, detail="Order can only be rejected when in SENT status")

    prev_status = order.status.value if hasattr(order.status, "value") else order.status

    items = (await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.archived_at.is_(None),
        )
    )).scalars().all()

    released = 0
    for it in items:
        if it.status in {
            OrderItemStatus.APPROVED, OrderItemStatus.REJECTED,
            OrderItemStatus.REROUTED, OrderItemStatus.SKIPPED,
            OrderItemStatus.REMOVED, OrderItemStatus.NOT_NEEDED,
            OrderItemStatus.NOT_AVAILABLE,
        }:
            continue
        prev_item = it.status.value if hasattr(it.status, "value") else it.status
        it.status = OrderItemStatus.NOT_AVAILABLE
        it.postponed_until = None
        await _record_event(
            db, lineage_id=it.lineage_id,
            event_type="RELEASED_BY_FACILITATOR_REJECT",
            actor_user_id=current_user.id, actor_role="FACILITATOR",
            order_id=order.id, order_item_id=it.id,
            prev_status=prev_item,
            new_status=OrderItemStatus.NOT_AVAILABLE.value,
        )
        released += 1

    if order.lineage_root_id is None:
        order.lineage_root_id = order.id

    order.is_returned_to_farmer = True
    order.return_reason = 'facilitator_declined'
    if order.facilitator_user_id and not order.released_facilitator_user_id:
        order.released_facilitator_user_id = order.facilitator_user_id
    # 2026-08-15 (Phase 2 facilitator-flow fix F1): clear
    # facilitator_user_id so the farmer's Send action (reroute-returned
    # items) doesn't 403 on the "facilitator owns this" defence. The
    # released_facilitator_user_id above preserves the chip context.
    order.facilitator_user_id = None

    await _record_event(
        db, lineage_id=order.id,
        event_type="REJECTED_BY_FACILITATOR",
        actor_user_id=current_user.id, actor_role="FACILITATOR",
        order_id=order.id,
        prev_status=prev_status,
        new_status=prev_status,
        metadata={
            "released_item_count": released,
            "reason": data.get("reason") if data else None,
        },
    )
    await db.commit()
    return {
        "order_id": order_id,
        "status": order.status,
        "released_item_count": released,
        "reason": (data or {}).get("reason"),
    }


@router.put("/facilitator/orders/{order_id}/return-to-farmer")
async def return_to_farmer(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator hands the returned items back to the farmer (Phase 2
    rework, 2026-08-15). Model B DRAFT unwound — flip flags on source
    instead of creating a new order.

    Gate (per user spec): refuse if the order has any in-flight work —
    postponed items, items pending farmer's approval, or items awaiting
    the dealer's Final Confirmation. Return-to-farmer is only valid
    when the dealer side is quiescent and unsold items exist.

    Effect on source order:
      - is_returned_to_farmer = true
      - is_returned_to_facilitator = false
      - released_facilitator_user_id preserved (chip context)
      - facilitator_user_id cleared (facilitator hands off)
      - return_reason = 'facilitator_returned'
    """
    await _assert_active_facilitator(db, current_user.id)
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.facilitator_user_id == current_user.id)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    items = (await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order_id,
            OrderItem.archived_at.is_(None),
        )
    )).scalars().all()

    postponed = sum(1 for i in items if i.status == OrderItemStatus.POSTPONED)
    sfa = sum(1 for i in items if i.status == OrderItemStatus.SENT_FOR_APPROVAL)
    awaiting_final_confirm = sum(
        1 for i in items
        if i.status == OrderItemStatus.APPROVED and i.final_confirmed_at is None
    )
    if postponed > 0 or sfa > 0 or awaiting_final_confirm > 0:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "order_not_quiescent",
                "message": (
                    "Return to farmer is only available once the dealer has "
                    "settled every item — no postponed items, no items with "
                    "the farmer for approval, and no items awaiting your "
                    "Final Confirmation."
                ),
                "postponed": postponed,
                "sent_for_approval": sfa,
                "awaiting_final_confirmation": awaiting_final_confirm,
            },
        )

    returned_set = {OrderItemStatus.NOT_AVAILABLE, OrderItemStatus.REJECTED}
    returned_count = sum(1 for i in items if i.status in returned_set)
    if returned_count == 0:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "nothing_to_return",
                "message": "No unsold items to return to the farmer.",
            },
        )

    prev_status = order.status.value if hasattr(order.status, "value") else order.status
    if order.lineage_root_id is None:
        order.lineage_root_id = order.id
    order.is_returned_to_farmer = True
    order.is_returned_to_facilitator = False
    order.return_reason = 'facilitator_returned'
    if order.facilitator_user_id and not order.released_facilitator_user_id:
        order.released_facilitator_user_id = order.facilitator_user_id
    order.facilitator_user_id = None

    await _record_event(
        db, lineage_id=order.id,
        event_type="RETURNED_TO_FARMER_BY_FACILITATOR",
        actor_user_id=current_user.id, actor_role="FACILITATOR",
        order_id=order.id,
        prev_status=prev_status,
        new_status=prev_status,
        metadata={
            "returned_item_count": returned_count,
        },
    )
    await db.commit()
    return {
        "order_id": order_id,
        "status": order.status,
        "returned_item_count": returned_count,
        "is_returned_to_farmer": True,
    }


@router.post("/facilitator/orders/{order_id}/reroute-returned", status_code=201)
async def facilitator_reroute_returned(
    order_id: str,
    data: Optional[dict] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator forwards returned items to another dealer.

    Spec (2026-06-06): when a dealer marks items NOT_AVAILABLE on a
    facilitator-routed order, the returned items belong to the
    facilitator's queue — NOT the farmer's. The facilitator either
    re-forwards them to a different dealer (this endpoint) or hands
    them back to the farmer via /return-to-farmer.

    Mirrors the farmer's /farmer/orders/{id}/reroute-returned shape
    but single-step: facilitator picks the new dealer at the point of
    reroute, so the new Order is created in SENT directly (no DRAFT
    waiting for a separate /send). Body:
      { dealer_user_id: str, include_postponed?: bool }

    Lineage: new order inherits source's lineage_root_id (or source.id
    if null). Source items become REROUTED; new items start PENDING
    on the fresh order. Both orders carry the same facilitator_user_id
    so the facilitator's list groups them under one chain.
    """
    await _assert_active_facilitator(db, current_user.id)
    order = (await db.execute(
        select(Order).where(
            Order.id == order_id,
            Order.facilitator_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    payload = data or {}
    new_dealer_id = payload.get("dealer_user_id")
    if not new_dealer_id:
        raise HTTPException(status_code=422, detail="dealer_user_id required")
    await _assert_active_dealer(db, new_dealer_id)
    include_postponed = bool(payload.get("include_postponed"))

    items_q = await db.execute(
        select(OrderItem).where(
            OrderItem.order_id == order.id,
            OrderItem.archived_at.is_(None),
        )
    )
    all_items = items_q.scalars().all()

    returned_set = {OrderItemStatus.NOT_AVAILABLE, OrderItemStatus.REJECTED}
    items_to_reroute = [it for it in all_items if it.status in returned_set]
    postponed_items = [it for it in all_items if it.status == OrderItemStatus.POSTPONED]

    if include_postponed:
        for pi in postponed_items:
            prev = pi.status.value if hasattr(pi.status, "value") else pi.status
            res = validate_item_transition(
                pi.status, OrderItemStatus.NOT_AVAILABLE.value, FACILITATOR,
            )
            if not res.allowed:
                _raise_transition(res)
            pi.status = OrderItemStatus.NOT_AVAILABLE
            await _record_event(
                db, lineage_id=pi.lineage_id,
                event_type="POSTPONED_CANCELLED_BY_FACILITATOR",
                actor_user_id=current_user.id, actor_role="FACILITATOR",
                order_id=order.id, order_item_id=pi.id,
                prev_status=prev, new_status=OrderItemStatus.NOT_AVAILABLE.value,
                metadata={"trigger": "facilitator_reroute_include_postponed"},
            )
            items_to_reroute.append(pi)

    if not items_to_reroute:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "nothing_to_reroute",
                "message": "No items in this order need re-routing.",
            },
        )

    new_lineage_root = order.lineage_root_id or order.id

    new_order = Order(
        subscription_id=order.subscription_id,
        farmer_user_id=order.farmer_user_id,
        client_id=order.client_id,
        category=order.category,
        date_from=order.date_from,
        date_to=order.date_to,
        # Facilitator-driven reroute lands as SENT directly: the
        # facilitator picked the dealer at the time of reroute, no
        # DRAFT step needed.
        status=OrderStatus.SENT,
        dealer_user_id=new_dealer_id,
        facilitator_user_id=current_user.id,
        locked_timelines=order.locked_timelines,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        lineage_root_id=new_lineage_root,
        # Facilitator reroute-returned inherits the Order ID.
        reference_number=order.reference_number,
    )
    db.add(new_order)
    await db.flush()

    if order.lineage_root_id is None:
        order.lineage_root_id = order.id

    for it in items_to_reroute:
        prev_status = it.status.value if hasattr(it.status, "value") else it.status
        new_item = OrderItem(
            order_id=new_order.id,
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
            actor_user_id=current_user.id, actor_role="FACILITATOR",
            order_id=order.id, order_item_id=it.id,
            prev_status=prev_status,
            new_status=OrderItemStatus.REROUTED.value,
            metadata={
                "to_order_id": new_order.id,
                "to_order_item_id": new_item.id,
                "to_dealer_user_id": new_dealer_id,
                "reason": "facilitator_reroute",
            },
        )
        await _record_event(
            db, lineage_id=it.lineage_id,
            event_type="REROUTED_TO",
            actor_user_id=current_user.id, actor_role="FACILITATOR",
            order_id=new_order.id, order_item_id=new_item.id,
            prev_status=OrderItemStatus.REROUTED.value,
            new_status=OrderItemStatus.PENDING.value,
            metadata={
                "from_order_id": order.id,
                "from_order_item_id": it.id,
                "reason": "facilitator_reroute",
            },
        )
        it.status = OrderItemStatus.REROUTED

    await _update_order_status(db, order.id)
    await db.commit()
    # Facilitator-driven reroute births a new order in SENT with the
    # picked dealer as recipient; farmer name lookup keeps the copy
    # consistent with the farmer-initiated push.
    farmer = (await db.execute(
        select(User).where(User.id == new_order.farmer_user_id)
    )).scalar_one_or_none()
    await _push_order_sent_recipient(
        db, new_order,
        (farmer.name if farmer else None) or "a farmer",
    )
    return {
        "new_order_id": new_order.id,
        "rerouted_count": len(items_to_reroute),
        "dealer_user_id": new_dealer_id,
    }


# ── Facilitator: Nearby dealers for forwarding ───────────────────────────────

@router.get("/facilitator/orders/{order_id}/lookup-dealer")
async def facilitator_lookup_dealer_for_order(
    order_id: str,
    phone: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    lang: str = Depends(get_locale),
):
    """Facilitator-side phone lookup for the
    `/facilitator/orders/{id}/route-to-dealer` flow. Same response
    shape as `/farmer/subscriptions/{id}/lookup-recipient` so the
    PWA reuses `RecipientLookupCard`.

    Brand-lock check uses the ORDER's items
    (`_order_has_locked_brand_items`), not practice_ids passed in.
    Facilitators only ever forward to dealers — a phone belonging
    to a non-dealer (e.g. facilitator-only) returns
    `not_dealer_or_facilitator`. The dealer must additionally be
    onboarded by `order.client_id` when has_locked is True; without
    a brand-locked item, any active dealer is allowed (matches the
    farmer-side rule in
    `/farmer/subscriptions/{id}/lookup-recipient`).
    """
    from app.modules.auth.service import get_user_by_phone
    from app.modules.clients.models import Client, ClientPromoter
    from app.services.i18n_cosh import resolve_names_by_cosh_id

    await _assert_active_facilitator(db, current_user.id)
    order = (await db.execute(
        select(Order).where(
            Order.id == order_id,
            Order.facilitator_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    has_locked = await _order_has_locked_brand_items(db, order.id)

    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) < 10:
        return {"found": False, "reason": "phone_not_registered", "phone": phone}
    normalised = "+91" + digits[-10:]
    target = await get_user_by_phone(db, normalised)
    if target is None:
        return {"found": False, "reason": "phone_not_registered", "phone": normalised}

    if target.id == current_user.id:
        return {
            "found": True, "user_id": target.id, "phone": target.phone,
            "name": target.name, "can_receive": False, "reason": "self",
        }

    cp_rows = (await db.execute(
        select(ClientPromoter.promoter_type)
        .where(
            ClientPromoter.user_id == target.id,
            ClientPromoter.promoter_type.in_(("FACILITATOR", "DEALER")),
            ClientPromoter.status == "ACTIVE",
        )
    )).scalars().all()
    roles_held = set(cp_rows)
    is_active = bool(cp_rows)

    company = (await db.execute(
        select(Client).where(Client.id == order.client_id)
    )).scalar_one_or_none()
    client_name = (company.display_name or company.short_name) if company else None

    loc_ids = {cid for cid in (target.state_cosh_id, target.district_cosh_id) if cid}
    loc_names = await resolve_names_by_cosh_id(db, loc_ids, lang) if loc_ids else {}

    base = {
        "found": True,
        "user_id": target.id,
        "phone": target.phone,
        "name": target.name,
        "photo_url": target.photo_url,
        "state_name": loc_names.get(target.state_cosh_id) if target.state_cosh_id else None,
        "district_name": loc_names.get(target.district_cosh_id) if target.district_cosh_id else None,
        "is_active": is_active,
        "client_name": client_name,
        "has_locked_brand": has_locked,
    }

    if "DEALER" not in roles_held:
        # Facilitator-only or no-role user — facilitators don't
        # forward to facilitators. The PWA copy on this side reads
        # "Not a dealer — facilitators only forward to dealers."
        return {**base, "role": None, "can_receive": False,
                "reason": "not_dealer_or_facilitator"}

    if has_locked and not await _is_dealer_onboarded_by_client(
        db, target.id, order.client_id,
    ):
        return {**base, "role": "DEALER", "can_receive": False,
                "reason": "dealer_not_onboarded"}

    return {**base, "role": "DEALER", "can_receive": True, "reason": "ok"}


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

    # Bulk-resolve crop names in the Facilitator's chosen locale; falls
    # back to EN → cosh_id via pick_translation.
    crop_ids = {pkg.crop_cosh_id for _, _, pkg, _ in rows if pkg.crop_cosh_id}
    crop_name_by_id: dict[str, str] = {}
    if crop_ids:
        lang = current_user.language_code or "en"
        for r in (await db.execute(
            select(CoshCoreItem).where(CoshCoreItem.cosh_id.in_(crop_ids))
        )).scalars().all():
            tr = r.translations or {}
            crop_name_by_id[r.cosh_id] = pick_translation(tr, lang, r.cosh_id)

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
    origin_lat: float | None = None,
    origin_lng: float | None = None,
    training_dealer_user_id: str | None = None,
) -> dict:
    """Shared core for both the order-based and new-order
    eligible-recipients endpoints. Returns the 5 nearest onboarded
    dealers (licence-matched) and the 5 nearest onboarded facilitators
    for the order's client, regardless of brand-lock. The picker is
    always a reference list of vetted recipients; the phone-entry
    escape hatch (unlocked orders → any RootsTalk-registered dealer)
    remains at the send-action layer.

    `has_locked_brand` is still returned — the frontend uses it to
    decide whether the phone-entry escape hatch is offered.
    """
    from math import radians, sin, cos, asin, sqrt
    from app.modules.clients.models import ClientPromoter

    cat_to_plural = {"PESTICIDE": "PESTICIDES", "FERTILIZER": "FERTILISERS"}
    required_plural = cat_to_plural.get((category or "").upper())

    # Origin for haversine — caller may override (Current Location
    # toggle in the PWA) else fall back to the farmer's saved
    # profile coords.
    if origin_lat is not None and origin_lng is not None:
        farmer_lat, farmer_lng = float(origin_lat), float(origin_lng)
    else:
        farmer_lat = float(current_user.gps_lat) if current_user.gps_lat else 0.0
        farmer_lng = float(current_user.gps_lng) if current_user.gps_lng else 0.0

    def _dist_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        rlat1, rlon1, rlat2, rlon2 = map(radians, [lat1, lon1, lat2, lon2])
        a = sin((rlat2 - rlat1) / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin((rlon2 - rlon1) / 2) ** 2
        return 2 * 6371 * asin(sqrt(a))

    promoter_rows = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.client_id == client_id,
            ClientPromoter.status == "ACTIVE",
        )
    )).scalars().all()
    onboarded_dealer_ids = {cp.user_id for cp in promoter_rows if cp.promoter_type == "DEALER"}
    onboarded_facilitator_ids = {cp.user_id for cp in promoter_rows if cp.promoter_type == "FACILITATOR"}

    # Training Dealer EXCLUSIVITY (2026-08-09): when a Training Dealer
    # is designated on a training-child client, the dealer list returns
    # ONLY that dealer — the slot exists so training orders don't leak
    # to the client's real dealers. If no Training Dealer is set, the
    # dealer list falls through to real onboarded dealers (they still
    # get training orders under the "Training" pill in their PWA).
    # Licence-category check still applies.
    dealers: list[dict] = []
    if training_dealer_user_id:
        td_profile = (await db.execute(
            select(DealerProfile).where(DealerProfile.user_id == training_dealer_user_id)
        )).scalar_one_or_none()
        if td_profile and (not required_plural or required_plural in (td_profile.sell_categories or [])) \
                and td_profile.shop_gps_lat and td_profile.shop_gps_lng:
            td_user = (await db.execute(
                select(User).where(User.id == training_dealer_user_id)
            )).scalar_one_or_none()
            if td_user:
                dist = _dist_km(
                    farmer_lat, farmer_lng,
                    float(td_profile.shop_gps_lat), float(td_profile.shop_gps_lng),
                )
                dealers.append({
                    "user_id": td_user.id,
                    "name": td_user.name,
                    "phone": td_user.phone,
                    "shop_name": td_profile.shop_name,
                    "is_training_dealer": True,
                    "shop_address": td_profile.shop_address,
                    "sell_categories": td_profile.sell_categories or [],
                    "distance_km": round(dist, 1),
                    "shop_gps_lat": float(td_profile.shop_gps_lat),
                    "shop_gps_lng": float(td_profile.shop_gps_lng),
                })
        # (No fallback if the Training Dealer profile is unusable — an
        # explicitly-designated dealer that can't be materialised should
        # surface as an empty list rather than silently reverting to
        # real dealers; the CA sees "no dealer available" and can fix
        # the Training Dealer picker in the CA portal.)
    else:
        if onboarded_dealer_ids:
            profiles = (await db.execute(
                select(DealerProfile).where(DealerProfile.user_id.in_(onboarded_dealer_ids))
            )).scalars().all()
            for profile in profiles:
                if required_plural and required_plural not in (profile.sell_categories or []):
                    continue
                if not profile.shop_gps_lat or not profile.shop_gps_lng:
                    continue
                dealer = (await db.execute(select(User).where(User.id == profile.user_id))).scalar_one_or_none()
                if not dealer:
                    continue
                dist = _dist_km(farmer_lat, farmer_lng,
                                float(profile.shop_gps_lat), float(profile.shop_gps_lng))
                dealers.append({
                    "user_id": dealer.id,
                    "name": dealer.name,
                    "phone": dealer.phone,
                    "shop_name": profile.shop_name,
                    "is_training_dealer": False,
                    "shop_address": profile.shop_address,
                    "sell_categories": profile.sell_categories or [],
                    "distance_km": round(dist, 1),
                    "shop_gps_lat": float(profile.shop_gps_lat),
                    "shop_gps_lng": float(profile.shop_gps_lng),
                })
        dealers.sort(key=lambda x: x["distance_km"])

    facilitators: list[dict] = []
    if onboarded_facilitator_ids:
        fac_users = (await db.execute(
            select(User).where(User.id.in_(onboarded_facilitator_ids))
        )).scalars().all()
        for fac in fac_users:
            if not fac.gps_lat or not fac.gps_lng:
                continue
            dist = _dist_km(farmer_lat, farmer_lng,
                            float(fac.gps_lat), float(fac.gps_lng))
            facilitators.append({
                "user_id": fac.id,
                "name": fac.name,
                "phone": fac.phone,
                "distance_km": round(dist, 1),
                "gps_lat": float(fac.gps_lat),
                "gps_lng": float(fac.gps_lng),
            })
    facilitators.sort(key=lambda x: x["distance_km"])

    return {
        "category": (category or "").upper() or None,
        "has_locked_brand": has_locked,
        "locked_brand_explainer": (
            "This order has a brand-locked item — only the company's "
            "onboarded dealers can fulfil it."
        ) if has_locked else None,
        "dealers": dealers[:5],
        "facilitators": facilitators[:5],
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

    **Training Dealer exception (2026-08-10)**: a user designated as
    `Client.training_dealer_user_id` on any ACTIVE training-child also
    passes this gate. The Training Dealer is by design a "dummy" — the
    CA picks a phone number that ISN'T onboarded so training orders
    are isolated from real dealers (see `_validate_training_dealer_candidate`).
    Requiring onboarding here would contradict the whole point of the
    slot: the CA can designate them, the farmer's picker will show
    them, they'll receive the order — and then this gate would 403
    them out of every dealer action. Empty case handled.
    """
    from app.modules.clients.models import ClientPromoter, Client

    onboarded = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == user_id,
            ClientPromoter.promoter_type == "DEALER",
            ClientPromoter.status == "ACTIVE",
        ).limit(1)
    )).scalar_one_or_none()
    if onboarded is not None:
        return

    training_slot = (await db.execute(
        select(Client.id).where(
            Client.training_dealer_user_id == user_id,
            Client.is_training == True,  # noqa: E712
            Client.training_status == "ACTIVE",
        ).limit(1)
    )).scalar_one_or_none()
    if training_slot is not None:
        return

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


_ORDER_ITEM_ACTIVE_STATUSES = {
    OrderItemStatus.PENDING,
    OrderItemStatus.AVAILABLE,
    OrderItemStatus.POSTPONED,
    OrderItemStatus.SENT_FOR_APPROVAL,
}


async def _update_order_status(db: AsyncSession, order_id: str):
    result = await db.execute(select(OrderItem).where(OrderItem.order_id == order_id))
    items = result.scalars().all()
    approval_items = [i for i in items if i.status in [OrderItemStatus.SENT_FOR_APPROVAL, OrderItemStatus.APPROVED]]
    approved = [i for i in items if i.status == OrderItemStatus.APPROVED]
    active = [i for i in items if i.status in _ORDER_ITEM_ACTIVE_STATUSES]
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one()
    if len(approved) == len(approval_items) and len(approved) > 0:
        order.status = OrderStatus.COMPLETED
    elif len(approved) > 0:
        order.status = OrderStatus.PARTIALLY_APPROVED
    # 2026-08-12 — Removed the "all-items-terminal-non-approved →
    # COMPLETED" branch. It hid the abandoned order from every surface
    # (farmer's Returned pill, facilitator's, dealer's own queue).
    # Correct behaviour: leave the order in its previous non-terminal
    # status so the returned_count > 0 gate on the Returned pill fires,
    # AND the dealer's Abort remains legal (Abort refuses on COMPLETED).
    # Dealer's explicit "Decline order" (now allowed from PROCESSING
    # too) provides the finalisation path when they know they have
    # nothing available.
    await _maybe_flip_returned_state(db, order, items)


# 2026-08-17 — Auto-flip is_returned_to_farmer (or _to_facilitator) when
# the order goes quiescent with unsold items still present. Without this,
# the farmer's Routed-card Send / Discard buttons render (frontend infers
# from active===0 && returned>0 counts) but /discard 400s because the DB
# flag was never set. Only Cancel + dealer whole-order decline used to
# set the flag; the natural dealer-marks-some-NA + farmer-approves-rest
# quiescence path was invisible to the flag. Follows the "invariant at
# every write path" rule: this is the extracted shared helper — every
# item-status mutation runs through _update_order_status (which calls
# here), and the four endpoints that skip _update_order_status
# (reject_order_item, cancel_postponed_item, final_confirm_item,
# final_confirm_all) call this helper directly.
async def _maybe_flip_returned_state(
    db: AsyncSession,
    order: Order,
    items: list[OrderItem] | None = None,
) -> None:
    if order.is_returned_to_farmer or order.is_returned_to_facilitator:
        return
    if order.status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED):
        return
    if items is None:
        items = (await db.execute(
            select(OrderItem).where(
                OrderItem.order_id == order.id,
                OrderItem.archived_at.is_(None),
            )
        )).scalars().all()
    else:
        items = [i for i in items if i.archived_at is None]
    # Mirror the /farmer/subscriptions/{id}/orders serializer's
    # active_item_count + returned_count computation exactly — that's
    # what the PWA reads to decide send-discard vs cancel modes.
    active = sum(
        1 for i in items if i.status in (
            OrderItemStatus.PENDING,
            OrderItemStatus.AVAILABLE,
            OrderItemStatus.POSTPONED,
            OrderItemStatus.SENT_FOR_APPROVAL,
        )
    ) + sum(
        1 for i in items
        if i.status == OrderItemStatus.APPROVED and i.final_confirmed_at is None
    )
    returned = sum(
        1 for i in items
        if i.status in (OrderItemStatus.NOT_AVAILABLE, OrderItemStatus.REJECTED)
    )
    if active > 0 or returned == 0:
        return
    # Route the flag to whoever's holding the order. Symmetric with the
    # facilitator_owns branching in dealer_decline_order.
    if order.facilitator_user_id:
        order.is_returned_to_facilitator = True
        routed_back_to = "FACILITATOR"
    else:
        order.is_returned_to_farmer = True
        routed_back_to = "FARMER"
    order.return_reason = 'dealer_declined'
    if order.dealer_user_id and not order.released_dealer_user_id:
        order.released_dealer_user_id = order.dealer_user_id
    prev_status = order.status.value if hasattr(order.status, "value") else order.status
    await _record_event(
        db, lineage_id=order.id,
        event_type="RETURNED_ON_QUIESCENCE",
        actor_user_id=None, actor_role="SYSTEM",
        order_id=order.id,
        prev_status=prev_status,
        new_status=prev_status,
        metadata={
            "routed_back_to": routed_back_to,
            "returned_item_count": returned,
        },
    )
