"""
Neytiri RM (Relationship Manager) support desk backend.
Provides: fast user search, user context view, alert queue, case log (items 1–6).
"""
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func, and_
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.rm.models import RMCase
from app.modules.clients.models import Client, ClientLocation
from app.modules.subscriptions.models import (
    Subscription, SubscriptionStatus, PromoterAssignment, AssignmentStatus,
)
from app.modules.advisory.models import Package, Timeline, Practice, Element
from app.modules.orders.models import Order, OrderStatus
from app.modules.farmpundit.models import Query as FarmPunditQuery, QueryStatus

router = APIRouter(tags=["RM Support"])


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Fast user search — item #1
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/rm/users/search")
async def search_users(
    q: str = "",
    client_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fast user search by name or phone across all roles and clients.
    Optional client_id filter narrows to users with subscriptions under that client.
    Returns up to 20 matches.
    """
    if len(q.strip()) < 2:
        return []

    query = select(User).where(
        or_(
            User.name.ilike(f"%{q.strip()}%"),
            User.phone.ilike(f"%{q.strip()}%"),
        )
    ).order_by(User.name).limit(20)

    result = await db.execute(query)
    users = result.scalars().all()

    # If filtering by client, keep only users subscribed/assigned to that client
    if client_id:
        sub_result = await db.execute(
            select(Subscription.farmer_user_id).where(Subscription.client_id == client_id)
        )
        promoter_result = await db.execute(
            select(PromoterAssignment.promoter_user_id).where(
                PromoterAssignment.status == AssignmentStatus.ACTIVE
            )
        )
        client_user_ids = (
            {r[0] for r in sub_result.all()} |
            {r[0] for r in promoter_result.all()}
        )
        users = [u for u in users if u.id in client_user_ids]

    out = []
    for u in users:
        subs = (await db.execute(
            select(Subscription.client_id).where(
                Subscription.farmer_user_id == u.id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            ).limit(3)
        )).scalars().all()
        client_names = []
        for cid in subs:
            c = (await db.execute(select(Client.display_name, Client.full_name).where(Client.id == cid))).first()
            if c:
                client_names.append(c[0] or c[1])
        out.append({
            "id": u.id,
            "name": u.name,
            "phone": u.phone,
            "email": u.email,
            "district": u.district_cosh_id,
            "state": u.state_cosh_id,
            "clients": client_names[:3],
        })
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# 2. User context view — item #2 & #3
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/rm/users/{user_id}/context")
async def user_context(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full context view for any user: profile, subscriptions, orders, queries, alerts."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Subscriptions
    subs_result = await db.execute(
        select(Subscription).where(Subscription.farmer_user_id == user_id).order_by(Subscription.created_at.desc())
    )
    subs = subs_result.scalars().all()
    subscription_data = []
    for s in subs:
        client = (await db.execute(select(Client).where(Client.id == s.client_id))).scalar_one_or_none()
        pkg = (await db.execute(select(Package).where(Package.id == s.package_id))).scalar_one_or_none()
        subscription_data.append({
            "id": s.id,
            "status": s.status,
            "reference_number": s.reference_number,
            "crop_start_date": s.crop_start_date,
            "client_name": (client.display_name or client.full_name) if client else None,
            "package_name": pkg.name if pkg else None,
            "crop_cosh_id": pkg.crop_cosh_id if pkg else None,
        })

    # Active orders (as farmer)
    orders_result = await db.execute(
        select(Order).where(
            Order.farmer_user_id == user_id,
            Order.status.notin_([OrderStatus.COMPLETED, OrderStatus.CANCELLED, OrderStatus.EXPIRED]),
        ).order_by(Order.created_at.desc()).limit(5)
    )
    orders = orders_result.scalars().all()
    order_data = [{"id": o.id, "status": o.status, "date_from": o.date_from, "date_to": o.date_to,
                   "dealer_user_id": o.dealer_user_id} for o in orders]

    # FarmPundit queries (as farmer)
    queries_result = await db.execute(
        select(FarmPunditQuery).where(FarmPunditQuery.farmer_user_id == user_id)
        .order_by(FarmPunditQuery.created_at.desc()).limit(5)
    )
    queries = queries_result.scalars().all()
    query_data = [{"id": q.id, "status": q.status, "title": q.title,
                   "created_at": q.created_at} for q in queries]

    # Promoter assignments (as dealer/facilitator)
    promoter_result = await db.execute(
        select(PromoterAssignment).where(
            PromoterAssignment.promoter_user_id == user_id,
            PromoterAssignment.status == AssignmentStatus.ACTIVE,
        ).limit(5)
    )
    promoter_data = [{"subscription_id": p.subscription_id, "type": p.promoter_type,
                      "assigned_at": p.assigned_at} for p in promoter_result.scalars().all()]

    # Case log for this user
    cases_result = await db.execute(
        select(RMCase).where(RMCase.user_id == user_id).order_by(RMCase.created_at.desc()).limit(10)
    )
    cases = cases_result.scalars().all()
    case_data = [{"id": c.id, "category": c.category, "description": c.description,
                  "resolution_status": c.resolution_status, "is_escalated": c.is_escalated,
                  "created_at": c.created_at} for c in cases]

    return {
        "profile": {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "email": user.email,
            "language_code": user.language_code,
            "state": user.state_cosh_id,
            "district": user.district_cosh_id,
            "sub_district": user.sub_district_cosh_id,
            "address": user.address_line,
            "gps_lat": float(user.gps_lat) if user.gps_lat else None,
            "gps_lng": float(user.gps_lng) if user.gps_lng else None,
        },
        "subscriptions": subscription_data,
        "active_orders": order_data,
        "query_history": query_data,
        "promoter_assignments": promoter_data,
        "case_log": case_data,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Alert queue — item #4 & #5
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/rm/alerts")
async def rm_alert_queue(
    alert_type: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    days_pending_min: Optional[int] = None,
    client_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Alert queue for RM.
    alert_type: START_DATE | INPUT_DUE | ALL (default ALL)
    Returns farmers needing attention with their contact details and alert context.
    """
    now = datetime.now(timezone.utc)
    out = []

    # ── START_DATE alerts: ACTIVE subscriptions with no crop_start_date ────────
    if alert_type in (None, "ALL", "START_DATE"):
        q = select(Subscription).where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.crop_start_date.is_(None),
        )
        if client_id:
            q = q.where(Subscription.client_id == client_id)
        subs = (await db.execute(q)).scalars().all()

        for s in subs:
            farmer = (await db.execute(select(User).where(User.id == s.farmer_user_id))).scalar_one_or_none()
            if not farmer:
                continue
            if state_cosh_id and farmer.state_cosh_id != state_cosh_id:
                continue
            if district_cosh_id and farmer.district_cosh_id != district_cosh_id:
                continue
            days_since_sub = (now - s.created_at.replace(tzinfo=timezone.utc)).days if s.created_at else 0
            if days_pending_min and days_since_sub < days_pending_min:
                continue

            client = (await db.execute(select(Client).where(Client.id == s.client_id))).scalar_one_or_none()
            # Find the alert receiver (dealer/facilitator promoter)
            promoter = (await db.execute(
                select(PromoterAssignment).where(
                    PromoterAssignment.subscription_id == s.id,
                    PromoterAssignment.status == AssignmentStatus.ACTIVE,
                )
            )).scalar_one_or_none()
            promoter_user = None
            if promoter:
                promoter_user = (await db.execute(select(User).where(User.id == promoter.promoter_user_id))).scalar_one_or_none()

            out.append({
                "alert_type": "START_DATE",
                "subscription_id": s.id,
                "farmer_id": farmer.id,
                "farmer_name": farmer.name,
                "farmer_phone": farmer.phone,
                "farmer_district": farmer.district_cosh_id,
                "farmer_state": farmer.state_cosh_id,
                "client_name": (client.display_name or client.full_name) if client else None,
                "days_pending": days_since_sub,
                "alert_receiver_name": promoter_user.name if promoter_user else None,
                "alert_receiver_phone": promoter_user.phone if promoter_user else None,
                "alert_receiver_type": promoter.promoter_type.value if promoter else None,
            })

    # ── INPUT_DUE alerts: current advisory window has unordered INPUT practices ──
    # (High cost — limit to first 50 subscriptions matching filter for performance)
    if alert_type in (None, "ALL", "INPUT_DUE"):
        q = select(Subscription).where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.crop_start_date.isnot(None),
        )
        if client_id:
            q = q.where(Subscription.client_id == client_id)
        q = q.limit(100)
        subs = (await db.execute(q)).scalars().all()
        today_date = now.date()

        for s in subs:
            crop_start = s.crop_start_date.date() if hasattr(s.crop_start_date, 'date') else s.crop_start_date
            if not crop_start:
                continue
            day_offset = (today_date - crop_start).days

            farmer = (await db.execute(select(User).where(User.id == s.farmer_user_id))).scalar_one_or_none()
            if not farmer:
                continue
            if state_cosh_id and farmer.state_cosh_id != state_cosh_id:
                continue
            if district_cosh_id and farmer.district_cosh_id != district_cosh_id:
                continue

            # Check for active DAS timelines with INPUT practices not ordered
            tl_result = await db.execute(select(Timeline).where(Timeline.package_id == s.package_id))
            has_overdue = False
            overdue_practice = None
            for tl in tl_result.scalars().all():
                if tl.from_type.value == "DAS" and tl.from_value <= day_offset <= tl.to_value:
                    p_result = await db.execute(
                        select(Practice).where(Practice.timeline_id == tl.id)
                    )
                    for p in p_result.scalars().all():
                        if p.l0_type.value == "INPUT":
                            # Check if there's an active order for this practice
                            order_check = (await db.execute(
                                select(Order).where(
                                    Order.farmer_user_id == s.farmer_user_id,
                                    Order.status.notin_([OrderStatus.COMPLETED, OrderStatus.CANCELLED, OrderStatus.EXPIRED]),
                                ).limit(1)
                            )).scalar_one_or_none()
                            if not order_check:
                                has_overdue = True
                                overdue_practice = f"{p.l1_type or p.l0_type.value}"
                                break
                if has_overdue:
                    break

            if not has_overdue:
                continue

            days_since_start = day_offset
            if days_pending_min and days_since_start < days_pending_min:
                continue

            client = (await db.execute(select(Client).where(Client.id == s.client_id))).scalar_one_or_none()
            out.append({
                "alert_type": "INPUT_DUE",
                "subscription_id": s.id,
                "farmer_id": farmer.id,
                "farmer_name": farmer.name,
                "farmer_phone": farmer.phone,
                "farmer_district": farmer.district_cosh_id,
                "farmer_state": farmer.state_cosh_id,
                "client_name": (client.display_name or client.full_name) if client else None,
                "days_pending": days_since_start,
                "overdue_practice": overdue_practice,
                "alert_receiver_name": None,
                "alert_receiver_phone": None,
                "alert_receiver_type": None,
            })

    out.sort(key=lambda x: x["days_pending"], reverse=True)
    return out[:200]


# ═══════════════════════════════════════════════════════════════════════════════
# 5a. Cross-tenant alerts summary — powers the SA RM home dashboard tiles
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/rm/alerts-summary")
async def rm_alerts_summary(
    sla_hours: int = 48,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One-shot cross-client aggregator for the SA RM Support Desk
    home. Mirrors the five CA Farmer Support alert types + open-cases
    + a "top clients by alert volume" breakdown so the SA can see
    which tenant is drowning right now.

    Counts:
      start_date_pending   — ACTIVE sub with crop_start_date IS NULL
      input_due            — pre-existing /admin/rm/alerts INPUT_DUE
                             count (approx; iterates timelines)
      missed_inputs        — INPUT practice whose window has closed
                             and no order was placed
      open_expert_queries  — FarmPunditQuery not RESPONDED/REJECTED/
                             EXPIRED, older than sla_hours (default 48)
      lapsed_farmers       — users who ever subscribed with any client
                             but hold no ACTIVE sub right now
      open_cases           — RMCase.resolution_status = 'OPEN'

    Top clients: summed across the first FOUR alert types (not
    open_cases + lapsed_farmers — those are SA-maintained state, not
    farmer-signal). Returns up to 5 clients sorted by total DESC.

    2026-11-28 — SA-gated. All existing /admin/rm/* endpoints are
    gate-less; that's a latent tightening target but out of scope here.
    This new endpoint takes the explicit _require_sa gate.
    """
    if not (settings.sa_email and current_user.email == settings.sa_email):
        raise HTTPException(
            status_code=403,
            detail={"code": "sa_only", "message": "SA access required."},
        )

    now = datetime.now(timezone.utc)
    today_date = now.date()
    sla_cutoff = now - timedelta(hours=max(1, sla_hours))

    # ── start_date_pending: cheap SQL ───────────────────────────────
    sdp_by_client_rows = (await db.execute(
        select(Subscription.client_id, func.count())
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.crop_start_date.is_(None),
        )
        .group_by(Subscription.client_id)
    )).all()
    sdp_by_client = {cid: cnt for cid, cnt in sdp_by_client_rows}
    start_date_pending = sum(sdp_by_client.values())

    # ── open_expert_queries: cheap SQL + per-client ────────────────
    oeq_by_client_rows = (await db.execute(
        select(FarmPunditQuery.client_id, func.count())
        .where(
            FarmPunditQuery.status.notin_([
                QueryStatus.RESPONDED,
                QueryStatus.REJECTED,
                QueryStatus.EXPIRED,
            ]),
            FarmPunditQuery.created_at <= sla_cutoff,
        )
        .group_by(FarmPunditQuery.client_id)
    )).all()
    oeq_by_client = {cid: cnt for cid, cnt in oeq_by_client_rows}
    open_expert_queries = sum(oeq_by_client.values())

    # ── lapsed_farmers: distinct farmer with NO active sub anywhere ─
    ever_farmer_ids = (await db.execute(
        select(Subscription.farmer_user_id).distinct()
    )).scalars().all()
    active_farmer_ids = set((await db.execute(
        select(Subscription.farmer_user_id).distinct()
        .where(Subscription.status == SubscriptionStatus.ACTIVE)
    )).scalars().all())
    lapsed_farmers = len([fid for fid in ever_farmer_ids if fid not in active_farmer_ids])

    # ── open_cases: cheap SQL ───────────────────────────────────────
    open_cases = (await db.execute(
        select(func.count(RMCase.id)).where(
            RMCase.resolution_status == "OPEN",
        )
    )).scalar() or 0

    # ── input_due + missed_inputs: iterate subs (bounded) ──────────
    # Expensive when many active subs; cap at 500 per type to keep
    # the summary responsive. These counts are "at least N" when
    # the cap is hit — a conscious tradeoff for a dashboard tile.
    # UI will show the number verbatim; drill-down on /rm/alerts
    # uses the exact per-type endpoint.
    input_due_by_client: dict[str, int] = {}
    missed_inputs_by_client: dict[str, int] = {}
    input_due = 0
    missed_inputs = 0
    SUB_SCAN_CAP = 500

    live_subs = (await db.execute(
        select(Subscription).where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.crop_start_date.isnot(None),
        ).limit(SUB_SCAN_CAP)
    )).scalars().all()

    for s in live_subs:
        crop_start = s.crop_start_date.date() if hasattr(s.crop_start_date, 'date') else s.crop_start_date
        if not crop_start:
            continue
        day_offset = (today_date - crop_start).days

        tls = (await db.execute(
            select(Timeline).where(Timeline.package_id == s.package_id)
        )).scalars().all()
        for tl in tls:
            if tl.from_type.value != "DAS":
                continue
            # Per-timeline order check — short-circuit expensive
            # practice scans when an order already covers the timeline.
            has_order = (await db.execute(
                select(Order.id)
                .where(
                    Order.subscription_id == s.id,
                    Order.status.notin_([OrderStatus.CANCELLED, OrderStatus.EXPIRED]),
                ).limit(1)
            )).scalar()
            if has_order:
                continue
            has_input = (await db.execute(
                select(Practice.id).where(
                    Practice.timeline_id == tl.id,
                    Practice.l0_type == "INPUT",
                ).limit(1)
            )).scalar()
            if not has_input:
                continue
            if tl.from_value <= day_offset <= tl.to_value:
                input_due += 1
                input_due_by_client[s.client_id] = input_due_by_client.get(s.client_id, 0) + 1
                break
            if day_offset > tl.to_value:
                missed_inputs += 1
                missed_inputs_by_client[s.client_id] = missed_inputs_by_client.get(s.client_id, 0) + 1
                break

    # ── top clients by farmer-signal alert volume ───────────────────
    tenant_totals: dict[str, int] = {}
    for cid, c in sdp_by_client.items():
        tenant_totals[cid] = tenant_totals.get(cid, 0) + c
    for cid, c in input_due_by_client.items():
        tenant_totals[cid] = tenant_totals.get(cid, 0) + c
    for cid, c in missed_inputs_by_client.items():
        tenant_totals[cid] = tenant_totals.get(cid, 0) + c
    for cid, c in oeq_by_client.items():
        tenant_totals[cid] = tenant_totals.get(cid, 0) + c

    top_client_ids = sorted(tenant_totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_clients: list[dict] = []
    if top_client_ids:
        cid_to_name = {}
        rows = (await db.execute(
            select(Client.id, Client.display_name, Client.full_name)
            .where(Client.id.in_([cid for cid, _ in top_client_ids]))
        )).all()
        for cid, dn, fn in rows:
            cid_to_name[cid] = dn or fn or cid
        for cid, total in top_client_ids:
            top_clients.append({
                "client_id": cid,
                "client_name": cid_to_name.get(cid, cid),
                "total_alerts": total,
            })

    return {
        "totals": {
            "start_date_pending": start_date_pending,
            "input_due": input_due,
            "missed_inputs": missed_inputs,
            "open_expert_queries": open_expert_queries,
            "lapsed_farmers": lapsed_farmers,
            "open_cases": open_cases,
        },
        "top_clients": top_clients,
        "sub_scan_capped": len(live_subs) >= SUB_SCAN_CAP,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Case log — item #6
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/rm/cases")
async def list_cases(
    user_id: Optional[str] = None,
    resolution_status: Optional[str] = None,
    is_escalated: Optional[bool] = None,
    client_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(RMCase).order_by(RMCase.created_at.desc())
    if user_id:
        q = q.where(RMCase.user_id == user_id)
    if resolution_status:
        q = q.where(RMCase.resolution_status == resolution_status)
    if is_escalated is not None:
        q = q.where(RMCase.is_escalated == is_escalated)
    if client_id:
        q = q.where(RMCase.client_id == client_id)
    result = await db.execute(q.limit(200))
    cases = result.scalars().all()

    out = []
    for c in cases:
        user = (await db.execute(select(User).where(User.id == c.user_id))).scalar_one_or_none()
        out.append({
            "id": c.id,
            "user_id": c.user_id,
            "user_name": user.name if user else None,
            "user_phone": user.phone if user else None,
            "category": c.category,
            "description": c.description,
            "call_log": c.call_log,
            "resolution_status": c.resolution_status,
            "is_escalated": c.is_escalated,
            "escalated_note": c.escalated_note,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
        })
    return out


@router.post("/admin/rm/cases", status_code=201)
async def create_case(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not data.get("user_id") or not data.get("description"):
        raise HTTPException(status_code=422, detail="user_id and description required")
    case = RMCase(
        user_id=data["user_id"],
        raised_by_user_id=current_user.id,
        client_id=data.get("client_id"),
        category=data.get("category", "OTHER"),
        description=data["description"],
        call_log=data.get("call_log"),
        resolution_status="OPEN",
        is_escalated=data.get("is_escalated", False),
        escalated_note=data.get("escalated_note"),
    )
    db.add(case)
    await db.commit()
    await db.refresh(case)
    return {"id": case.id, "resolution_status": case.resolution_status}


@router.put("/admin/rm/cases/{case_id}")
async def update_case(
    case_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    case = (await db.execute(select(RMCase).where(RMCase.id == case_id))).scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    for field in ["category", "description", "call_log", "resolution_status", "is_escalated", "escalated_note"]:
        if field in data:
            setattr(case, field, data[field])
    if data.get("is_escalated") and not case.escalated_by_user_id:
        case.escalated_by_user_id = current_user.id
    await db.commit()
    return {"id": case_id, "resolution_status": case.resolution_status}
