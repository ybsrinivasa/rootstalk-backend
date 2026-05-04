import secrets
import string
from datetime import datetime, timedelta, timezone, date
from math import radians, cos, sin, asin, sqrt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.subscriptions.models import (
    Subscription, SubscriptionWaitlist, SubscriptionPool,
    AlertRecipient, PromoterAssignment,
    SubscriptionStatus, SubscriptionType, PromoterType, AssignmentStatus,
    SubscriptionPaymentRequest, FarmerSubscriptionHistory,
    ConditionalAnswer, TriggeredCHAEntry,
)
from app.modules.advisory.models import (
    Package, Parameter, Variable, PackageVariable, Timeline, Practice, Element,
    ConditionalQuestion, PracticeConditional,
)
from app.modules.clients.models import Client
from app.modules.advisory.models import PGRecommendation, PGTimeline, PGPractice, PGElement
from app.modules.advisory.models import SPRecommendation, SPTimeline, SPPractice, SPElement
from app.modules.platform.models import UserRole, RoleType
from app.modules.orders.models import DealerProfile
from app.modules.clients.models import Client, ClientLocation, ClientStatus

router = APIRouter(tags=["Subscriptions"])

WAITLIST_EXPIRY_DAYS = 3
PAYMENT_REQUEST_EXPIRY_HOURS = 72


def _is_frequency_due_today(frequency_days, timeline_from_date, today_date) -> bool:
    """Frequency-based practice display filter.

    For a frequency-based practice, returns True only on prescribed application
    days within the timeline window. Day numbering is 1-based from timeline start.
    With frequency_days = 2, the practice appears on Days 1, 3, 5, ... (offset 0).
    Formula: (day_in_timeline - 1) % frequency_days == 0.

    Returns True for non-frequency practices (treat as always-due if in window),
    so this can be used as a uniform post-BL-04 filter.
    """
    if not frequency_days or frequency_days < 1:
        return True
    if timeline_from_date is None:
        return True
    day_in_timeline = (today_date - timeline_from_date).days + 1  # 1-based
    if day_in_timeline < 1:
        return False
    return (day_in_timeline - 1) % frequency_days == 0


# ── Subscription Pool (CA) ─────────────────────────────────────────────────────

class PoolPurchase(BaseModel):
    units: int


@router.post("/client/{client_id}/subscription-pool/purchase", status_code=201)
async def purchase_pool_units(
    client_id: str,
    request: PoolPurchase,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pool = SubscriptionPool(client_id=client_id, units_purchased=request.units)
    db.add(pool)
    await db.commit()
    balance = await _get_pool_balance(db, client_id)
    return {"detail": f"{request.units} units added", "balance": balance}


@router.get("/client/{client_id}/subscription-pool/balance")
async def get_pool_balance(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    balance = await _get_pool_balance(db, client_id)
    return {"client_id": client_id, "available_units": balance}


# ── PoP Guided Elimination (BL-01) ─────────────────────────────────────────────

@router.get("/farmer/packages")
async def get_available_packages(
    crop_cosh_id: str,
    district_cosh_id: str,
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns all ACTIVE packages for a crop+district+client."""
    from app.modules.advisory.models import PackageLocation, PackageStatus
    result = await db.execute(
        select(Package)
        .join(PackageLocation, PackageLocation.package_id == Package.id)
        .where(
            Package.client_id == client_id,
            Package.crop_cosh_id == crop_cosh_id,
            Package.status == PackageStatus.ACTIVE,
            PackageLocation.district_cosh_id == district_cosh_id,
        )
    )
    packages = result.scalars().all()
    return [{"id": p.id, "name": p.name, "description": p.description, "package_type": p.package_type} for p in packages]


@router.get("/farmer/packages/guided-step")
async def guided_elimination_step(
    crop_cosh_id: str,
    district_cosh_id: str,
    client_id: str,
    answers: str = "",  # "param_id:var_id,param_id:var_id" previous answers
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    BL-01: PoP Guided Elimination.
    Returns next parameter question with only valid variables,
    or the final package if one remains.
    """
    from app.modules.advisory.models import PackageLocation, PackageStatus

    # Load remaining pool
    q = (select(Package)
         .join(PackageLocation, PackageLocation.package_id == Package.id)
         .where(
             Package.client_id == client_id,
             Package.crop_cosh_id == crop_cosh_id,
             Package.status == PackageStatus.ACTIVE,
             PackageLocation.district_cosh_id == district_cosh_id,
         ))

    # Apply previous answers to narrow pool
    parsed_answers = {}
    if answers:
        for pair in answers.split(","):
            if ":" in pair:
                param_id, var_id = pair.split(":", 1)
                parsed_answers[param_id] = var_id

    remaining_packages = (await db.execute(q)).scalars().all()

    for param_id, var_id in parsed_answers.items():
        remaining_packages = [
            p for p in remaining_packages
            if await _package_has_variable(db, p.id, param_id, var_id)
        ]

    if len(remaining_packages) == 1:
        pkg = remaining_packages[0]
        return {"done": True, "package": {"id": pkg.id, "name": pkg.name, "description": pkg.description}}

    if len(remaining_packages) == 0:
        return {"done": False, "error": "No packages match — data configuration issue"}

    # Find next Parameter (most variables across remaining pool, not yet answered)
    pkg_ids = [p.id for p in remaining_packages]
    answered_params = set(parsed_answers.keys())

    all_pvs = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id.in_(pkg_ids))
    )).scalars().all()

    param_var_counts: dict = {}
    for pv in all_pvs:
        if pv.parameter_id in answered_params:
            continue
        if pv.parameter_id not in param_var_counts:
            param_var_counts[pv.parameter_id] = set()
        param_var_counts[pv.parameter_id].add(pv.variable_id)

    if not param_var_counts:
        pkg = remaining_packages[0]
        return {"done": True, "package": {"id": pkg.id, "name": pkg.name, "description": pkg.description}}

    next_param_id = max(param_var_counts, key=lambda p: len(param_var_counts[p]))
    valid_var_ids = param_var_counts[next_param_id]

    param = (await db.execute(select(Parameter).where(Parameter.id == next_param_id))).scalar_one_or_none()
    variables = (await db.execute(
        select(Variable).where(Variable.id.in_(valid_var_ids))
    )).scalars().all()

    if len(variables) == 1:
        # Auto-select single-option parameter
        return await guided_elimination_step(
            crop_cosh_id, district_cosh_id, client_id,
            answers=f"{answers},{next_param_id}:{variables[0].id}" if answers else f"{next_param_id}:{variables[0].id}",
            db=db, current_user=current_user
        )

    return {
        "done": False,
        "parameter": {"id": next_param_id, "name": param.name if param else next_param_id},
        "variables": [{"id": v.id, "name": v.name} for v in variables],
        "remaining_count": len(remaining_packages),
    }


# ── Discovery Endpoints ────────────────────────────────────────────────────────

@router.get("/farmer/discover/crops")
async def discover_crops(
    district_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All crops that have at least one ACTIVE package in the given district."""
    from app.modules.advisory.models import PackageLocation, PackageStatus
    result = await db.execute(
        select(Package.crop_cosh_id)
        .join(PackageLocation, PackageLocation.package_id == Package.id)
        .where(
            Package.client_id != None,  # noqa
            Package.status == PackageStatus.ACTIVE,
            PackageLocation.district_cosh_id == district_cosh_id,
        )
        .distinct()
    )
    crops = result.scalars().all()
    return [{"crop_cosh_id": c} for c in crops]


@router.get("/farmer/discover/companies")
async def discover_companies(
    crop_cosh_id: str,
    district_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All companies (clients) with at least one ACTIVE package for this crop+district."""
    from app.modules.advisory.models import PackageLocation, PackageStatus
    from app.modules.clients.models import Client, ClientStatus
    result = await db.execute(
        select(Package.client_id)
        .join(PackageLocation, PackageLocation.package_id == Package.id)
        .where(
            Package.client_id != None,  # noqa
            Package.crop_cosh_id == crop_cosh_id,
            Package.status == PackageStatus.ACTIVE,
            PackageLocation.district_cosh_id == district_cosh_id,
        )
        .distinct()
    )
    client_ids = result.scalars().all()

    companies = []
    for client_id in client_ids:
        client = (await db.execute(
            select(Client).where(Client.id == client_id, Client.status == ClientStatus.ACTIVE)
        )).scalar_one_or_none()
        if client:
            companies.append({
                "id": client.id,
                "display_name": client.display_name,
                "tagline": client.tagline,
                "logo_url": client.logo_url,
                "primary_colour": client.primary_colour,
            })
    return companies


# ── Self-Subscription ─────────────────────────────────────────────────────────

class SubscribeRequest(BaseModel):
    package_id: str
    client_id: str
    subscription_type: str = "SELF"
    promoter_user_id: Optional[str] = None


@router.post("/farmer/subscriptions", status_code=201)
async def create_subscription(
    request: SubscribeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Confirm subscription. Checks pool — activates or waitlists."""
    balance = await _get_pool_balance(db, request.client_id)

    sub = Subscription(
        farmer_user_id=current_user.id,
        client_id=request.client_id,
        package_id=request.package_id,
        promoter_user_id=request.promoter_user_id,
        subscription_type=request.subscription_type,
        status=SubscriptionStatus.WAITLISTED,
    )
    db.add(sub)
    await db.flush()

    if balance > 0:
        sub.status = SubscriptionStatus.ACTIVE
        sub.subscription_date = datetime.now(timezone.utc)
        sub.reference_number = await _generate_reference_for_sub(db, sub.client_id)
        await _consume_pool_unit(db, request.client_id)
    else:
        # Waitlisted — 3-day expiry
        expires_at = datetime.now(timezone.utc) + timedelta(days=WAITLIST_EXPIRY_DAYS)
        db.add(SubscriptionWaitlist(subscription_id=sub.id, expires_at=expires_at))

    await db.commit()
    await db.refresh(sub)
    return {
        "id": sub.id,
        "status": sub.status,
        "reference_number": sub.reference_number,
        "message": "Subscription active." if sub.status == SubscriptionStatus.ACTIVE else "Subscription waitlisted — company has 3 days to top up pool.",
    }


@router.get("/farmer/subscriptions")
async def list_farmer_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Subscription).where(Subscription.farmer_user_id == current_user.id)
        .order_by(Subscription.created_at.desc())
    )
    subs = result.scalars().all()
    return [{"id": s.id, "package_id": s.package_id, "client_id": s.client_id,
             "status": s.status, "reference_number": s.reference_number,
             "crop_start_date": s.crop_start_date, "subscription_date": s.subscription_date} for s in subs]


@router.put("/farmer/subscriptions/{subscription_id}/start-date")
async def set_start_date(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-05b: Set or update crop start date — shifts all timeline dates, respects locks."""
    from app.services.bl05_lock_detection import compute_date_shifts, TimelineDateRange, OrderItemStub
    from app.modules.orders.models import Order, OrderItem
    from datetime import date as dt_date

    sub = await _get_subscription(db, subscription_id, current_user.id)
    new_start_raw = data.get("crop_start_date")

    # Crop start date can be set or updated, but never cleared.
    if not new_start_raw:
        raise HTTPException(
            status_code=422,
            detail="Crop start date cannot be empty. You can update it but not remove it.",
        )

    # First ever start date — just set it
    if not sub.crop_start_date:
        sub.crop_start_date = new_start_raw
        await db.commit()
        return {"detail": "Start date set", "crop_start_date": sub.crop_start_date}

    # Parse old and new dates
    old_start = sub.crop_start_date.date() if hasattr(sub.crop_start_date, 'date') else sub.crop_start_date
    from datetime import datetime
    if isinstance(new_start_raw, str):
        new_start = datetime.fromisoformat(new_start_raw.replace("Z", "+00:00")).date()
    else:
        new_start = new_start_raw

    today = dt_date.today()

    # Get active order items for lock detection
    order_result = await db.execute(
        select(Order).where(Order.subscription_id == sub.id)
    )
    orders = order_result.scalars().all()
    active_items: list[OrderItemStub] = []
    for order in orders:
        items_result = await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
        for item in items_result.scalars().all():
            active_items.append(OrderItemStub(
                timeline_id=item.timeline_id,
                order_from_date=order.date_from.date() if hasattr(order.date_from, 'date') else order.date_from,
                order_to_date=order.date_to.date() if hasattr(order.date_to, 'date') else order.date_to,
                status=item.status,
            ))

    # Load all timelines for this subscription's package
    tl_result = await db.execute(
        select(Timeline).where(Timeline.package_id == sub.package_id)
    )
    timelines = tl_result.scalars().all()

    # Build timeline date ranges (compute dates relative to old start)
    from datetime import timedelta
    tl_ranges: list[TimelineDateRange] = []
    for tl in timelines:
        if tl.from_type.value == "DAS":
            from_d = old_start + timedelta(days=tl.from_value)
            to_d = old_start + timedelta(days=tl.to_value)
        elif tl.from_type.value == "DBS":
            from_d = old_start - timedelta(days=tl.from_value)
            to_d = old_start - timedelta(days=tl.to_value)
        else:
            continue
        tl_ranges.append(TimelineDateRange(id=tl.id, from_date=from_d, to_date=to_d))

    # ── Also include triggered CHA timelines (PG/SP) for lock detection ─────────
    # Per spec §6.6: "Both conditions apply equally to CCA and CHA timelines."
    # CHA timelines are anchored to triggered_at (the date the farmer confirmed
    # the diagnosis), NOT to crop_start_date. They must be checked for VIEWED +
    # PO locks but their dates do NOT shift when crop_start_date changes
    # (is_cha=True signals this to compute_date_shifts).
    cha_entries = (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.subscription_id == sub.id,
            TriggeredCHAEntry.status == "ACTIVE",
        )
    )).scalars().all()
    for cha in cha_entries:
        triggered_d = cha.triggered_at.date() if hasattr(cha.triggered_at, 'date') else cha.triggered_at
        if cha.recommendation_type == "SP":
            sp_timelines = (await db.execute(
                select(SPTimeline).where(SPTimeline.sp_recommendation_id == cha.recommendation_id)
            )).scalars().all()
            for sp_tl in sp_timelines:
                from_d = triggered_d + timedelta(days=sp_tl.from_value)
                to_d = triggered_d + timedelta(days=sp_tl.to_value)
                tl_ranges.append(TimelineDateRange(
                    id=f"sp_{sp_tl.id}", from_date=from_d, to_date=to_d, is_cha=True,
                ))
        elif cha.recommendation_type == "PG":
            pg_timelines = (await db.execute(
                select(PGTimeline).where(PGTimeline.pg_recommendation_id == cha.recommendation_id)
            )).scalars().all()
            for pg_tl in pg_timelines:
                from_d = triggered_d + timedelta(days=pg_tl.from_value)
                to_d = triggered_d + timedelta(days=pg_tl.to_value)
                tl_ranges.append(TimelineDateRange(
                    id=f"pg_{pg_tl.id}", from_date=from_d, to_date=to_d, is_cha=True,
                ))

    # Compute shifts
    shifts, delta_days = compute_date_shifts(tl_ranges, old_start, new_start, today, active_items)

    # Update start date
    sub.crop_start_date = new_start_raw

    # Shift active orders by delta
    for order in orders:
        if hasattr(order.date_from, 'date'):
            order.date_from = order.date_from + timedelta(days=delta_days)
            order.date_to = order.date_to + timedelta(days=delta_days)

    await db.commit()
    return {
        "detail": "Start date updated",
        "crop_start_date": sub.crop_start_date,
        "delta_days": delta_days,
        "timelines_shifted": len(shifts),
        "locked_timelines": sum(1 for s in shifts if s.was_locked),
    }


@router.get("/farmer/subscriptions/{subscription_id}/advisory/next-date")
async def get_next_advisory_date(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the date of the next upcoming DAS practice for this subscription."""
    from datetime import date as dt_date

    sub = await _get_subscription(db, subscription_id, current_user.id)
    if not sub.crop_start_date:
        return {"next_date": None, "reason": "no_start_date"}

    start = sub.crop_start_date.date() if hasattr(sub.crop_start_date, 'date') else sub.crop_start_date
    today = dt_date.today()
    day_offset = (today - start).days

    timelines = (await db.execute(
        select(Timeline).where(Timeline.package_id == sub.package_id)
    )).scalars().all()

    upcoming = [
        tl for tl in timelines
        if (tl.from_type.value if hasattr(tl.from_type, 'value') else str(tl.from_type)) == "DAS"
        and int(tl.from_value) > day_offset
    ]
    if not upcoming:
        return {"next_date": None, "reason": "no_more_practices"}

    next_tl = min(upcoming, key=lambda t: int(t.from_value))
    next_date = start + timedelta(days=int(next_tl.from_value))
    return {
        "next_date": next_date.isoformat(),
        "timeline_name": next_tl.name,
        "days_until": int(next_tl.from_value) - day_offset,
    }


# ── Promoter Assignment Flow ───────────────────────────────────────────────────

class PromoterAssignRequest(BaseModel):
    farmer_phone: str
    package_id: str
    client_id: str
    promoter_type: str = "DEALER"


@router.post("/promoter/assignments/initiate", status_code=201)
async def initiate_assignment(
    request: PromoterAssignRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Promoter assigns advisory to farmer. Farmer must approve."""
    from app.modules.auth.service import get_user_by_phone
    farmer = await get_user_by_phone(db, request.farmer_phone)
    if not farmer:
        raise HTTPException(status_code=404, detail="Farmer not found. They must be registered in the PWA first.")

    sub = Subscription(
        farmer_user_id=farmer.id,
        client_id=request.client_id,
        package_id=request.package_id,
        promoter_user_id=current_user.id,
        subscription_type=SubscriptionType.ASSIGNED,
        status=SubscriptionStatus.WAITLISTED,
    )
    db.add(sub)
    await db.flush()

    assignment = PromoterAssignment(
        subscription_id=sub.id,
        promoter_user_id=current_user.id,
        promoter_type=request.promoter_type,
        status=AssignmentStatus.PENDING_FARMER_APPROVAL,
    )
    db.add(assignment)
    await db.commit()
    return {"subscription_id": sub.id, "assignment_id": assignment.id, "status": "Awaiting farmer approval"}


@router.get("/promoter/farmer-lookup")
async def promoter_farmer_lookup(
    phone: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check if farmer is registered and return their basic info for promoter assignment."""
    from app.modules.auth.service import get_user_by_phone
    farmer = await get_user_by_phone(db, phone)
    if not farmer:
        raise HTTPException(status_code=404, detail="No farmer found with this phone number. They must register in the RootsTalk app first.")
    return {
        "id": farmer.id,
        "name": farmer.name,
        "phone": farmer.phone,
        "state_cosh_id": farmer.state_cosh_id,
        "district_cosh_id": farmer.district_cosh_id,
    }


@router.get("/dealer/district-advisories")
async def dealer_district_advisories(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns active packages in the dealer's registered district — helps dealer understand what farmers are being advised to buy."""
    from app.modules.advisory.models import PackageLocation, PackageStatus

    if not current_user.district_cosh_id:
        return []

    result = await db.execute(
        select(Package, Client)
        .join(PackageLocation, PackageLocation.package_id == Package.id)
        .join(Client, Client.id == Package.client_id)
        .where(
            Package.client_id != None,  # noqa
            Package.status == PackageStatus.ACTIVE,
            PackageLocation.district_cosh_id == current_user.district_cosh_id,
            Client.status == ClientStatus.ACTIVE,
        )
        .order_by(Package.crop_cosh_id)
    )
    rows = result.all()
    return [
        {
            "package_id": pkg.id,
            "package_name": pkg.name,
            "crop_cosh_id": pkg.crop_cosh_id,
            "client_id": client.id,
            "client_name": client.display_name,
            "client_colour": client.primary_colour,
        }
        for pkg, client in rows
    ]


@router.get("/farmer/assignments/pending")
async def farmer_pending_assignments(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns subscriptions assigned by a Promoter that are awaiting farmer approval."""
    result = await db.execute(
        select(Subscription, PromoterAssignment)
        .join(PromoterAssignment, PromoterAssignment.subscription_id == Subscription.id)
        .where(
            Subscription.farmer_user_id == current_user.id,
            Subscription.subscription_type == SubscriptionType.ASSIGNED,
            PromoterAssignment.status == AssignmentStatus.PENDING_FARMER_APPROVAL,
        )
    )
    rows = result.all()
    promoter_ids = [assignment.promoter_user_id for _, assignment in rows]
    promoters = {}
    for pid in set(promoter_ids):
        p = (await db.execute(select(User).where(User.id == pid))).scalar_one_or_none()
        if p:
            promoters[pid] = {"name": p.name, "phone": p.phone}

    return [
        {
            "subscription_id": sub.id,
            "client_id": sub.client_id,
            "package_id": sub.package_id,
            "promoter": promoters.get(assignment.promoter_user_id, {}),
            "promoter_type": assignment.promoter_type,
            "created_at": assignment.created_at,
        }
        for sub, assignment in rows
    ]


@router.get("/farmer/assignments/{subscription_id}/details")
async def get_assignment_details(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns full details of a pending assignment for farmer review."""
    sub = await _get_subscription(db, subscription_id, current_user.id)

    assignment = (await db.execute(
        select(PromoterAssignment).where(PromoterAssignment.subscription_id == subscription_id)
    )).scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Not an assignment")

    package = (await db.execute(
        select(Package).where(Package.id == sub.package_id)
    )).scalar_one_or_none()
    client = (await db.execute(
        select(Client).where(Client.id == sub.client_id)
    )).scalar_one_or_none()

    # Parameter-variable selections for this package (plain-language summary)
    pvs = (await db.execute(
        select(PackageVariable, Parameter, Variable)
        .join(Parameter, Parameter.id == PackageVariable.parameter_id)
        .join(Variable, Variable.id == PackageVariable.variable_id)
        .where(PackageVariable.package_id == sub.package_id)
    )).all()
    pv_summary = [{"parameter": p.name, "variable": v.name} for _, p, v in pvs]

    promoter = (await db.execute(
        select(User).where(User.id == assignment.promoter_user_id)
    )).scalar_one_or_none()

    return {
        "subscription_id": sub.id,
        "company": {
            "id": client.id,
            "name": client.display_name,
            "logo_url": client.logo_url,
            "primary_colour": client.primary_colour,
            "tagline": client.tagline,
        } if client else None,
        "crop_cosh_id": package.crop_cosh_id if package else None,
        "package_description": package.description if package else None,
        "duration_days": package.duration_days if package else None,
        "package_type": package.package_type.value if package and package.package_type else None,
        "parameter_variables": pv_summary,
        "promoter": {"name": promoter.name, "phone": promoter.phone} if promoter else None,
        "promoter_type": assignment.promoter_type.value if hasattr(assignment.promoter_type, "value") else assignment.promoter_type,
        "subscription_price": 199,  # hardcoded for now
        "paid_by_company": True,
    }


@router.put("/farmer/assignments/{subscription_id}/respond")
async def respond_to_assignment(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer approves or rejects Promoter assignment."""
    sub = await _get_subscription(db, subscription_id, current_user.id)
    approved = data.get("approved", False)

    assignment_result = await db.execute(
        select(PromoterAssignment).where(PromoterAssignment.subscription_id == subscription_id)
    )
    assignment = assignment_result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if assignment:
        assignment.status = AssignmentStatus.ACTIVE if approved else AssignmentStatus.REJECTED_BY_FARMER
        assignment.farmer_responded_at = now

    if approved:
        balance = await _get_pool_balance(db, sub.client_id)
        if balance > 0:
            sub.status = SubscriptionStatus.ACTIVE
            sub.subscription_date = now
            sub.reference_number = await _generate_reference_for_sub(db, sub.client_id)
            await _consume_pool_unit(db, sub.client_id)
        # else stays WAITLISTED — company has 3 days
    else:
        sub.status = SubscriptionStatus.CANCELLED

    await db.commit()
    return {"status": sub.status, "reference_number": sub.reference_number}


# ── Payment Delegation ────────────────────────────────────────────────────────

class PaymentDelegateRequest(BaseModel):
    requested_from_user_id: Optional[str] = None
    delegate_phone: Optional[str] = None  # phone-based lookup (e.g. "+919876543210")
    role: Optional[str] = None  # DEALER or FACILITATOR (informational)


@router.post("/farmer/subscriptions/{subscription_id}/delegate-payment")
async def delegate_payment(
    subscription_id: str,
    request: PaymentDelegateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = await _get_subscription(db, subscription_id, current_user.id)

    # Resolve delegate user — either by explicit ID or by phone number
    resolved_user_id = request.requested_from_user_id
    if not resolved_user_id and request.delegate_phone:
        from app.modules.auth.service import get_user_by_phone
        delegate_user = await get_user_by_phone(db, request.delegate_phone)
        if not delegate_user:
            raise HTTPException(status_code=404, detail="No registered user found with that phone number.")
        resolved_user_id = delegate_user.id
    if not resolved_user_id:
        raise HTTPException(status_code=422, detail="Provide either requested_from_user_id or delegate_phone.")

    expires_at = datetime.now(timezone.utc) + timedelta(hours=PAYMENT_REQUEST_EXPIRY_HOURS)
    pr = SubscriptionPaymentRequest(
        subscription_id=subscription_id,
        farmer_user_id=current_user.id,
        requested_from_user_id=resolved_user_id,
        expires_at=expires_at,
    )
    db.add(pr)
    await db.commit()
    return {"detail": "Payment request sent", "expires_at": expires_at}


@router.delete("/farmer/subscriptions/{subscription_id}/delegate-payment")
async def cancel_delegation(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer cancels a pending payment delegation. They can then choose someone else
    or pay themselves."""
    sub = await _get_subscription(db, subscription_id, current_user.id)
    pending = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.subscription_id == sub.id,
            SubscriptionPaymentRequest.status == "PENDING",
        )
    )).scalars().all()
    for pr in pending:
        pr.status = "CANCELLED"
    await db.commit()
    return {"detail": f"{len(pending)} pending request(s) cancelled"}


@router.get("/dealer/payment-requests")
async def list_payment_requests(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.requested_from_user_id == current_user.id,
            SubscriptionPaymentRequest.status == "PENDING",
        )
    )
    return result.scalars().all()


@router.put("/dealer/payment-requests/{request_id}/pay")
async def pay_subscription(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer/facilitator pays — becomes Promoter for this subscription."""
    result = await db.execute(
        select(SubscriptionPaymentRequest).where(SubscriptionPaymentRequest.id == request_id)
    )
    pr = result.scalar_one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Payment request not found")

    pr.status = "PAID"
    sub = (await db.execute(select(Subscription).where(Subscription.id == pr.subscription_id))).scalar_one()
    sub.promoter_user_id = current_user.id

    balance = await _get_pool_balance(db, sub.client_id)
    if balance > 0:
        sub.status = SubscriptionStatus.ACTIVE
        sub.subscription_date = datetime.now(timezone.utc)
        sub.reference_number = await _generate_reference_for_sub(db, sub.client_id)
        await _consume_pool_unit(db, sub.client_id)

    await db.commit()
    return {"status": sub.status, "reference_number": sub.reference_number}


@router.put("/dealer/payment-requests/{request_id}/decline")
async def decline_payment(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(SubscriptionPaymentRequest).where(SubscriptionPaymentRequest.id == request_id)
    )
    pr = result.scalar_one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Not found")
    pr.status = "DECLINED"
    await db.commit()
    return {"detail": "Declined"}


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_pool_balance(db: AsyncSession, client_id: str) -> int:
    result = await db.execute(
        select(
            func.coalesce(func.sum(SubscriptionPool.units_purchased), 0) -
            func.coalesce(func.sum(SubscriptionPool.units_consumed), 0)
        ).where(SubscriptionPool.client_id == client_id)
    )
    return result.scalar() or 0


async def _consume_pool_unit(db: AsyncSession, client_id: str):
    result = await db.execute(
        select(SubscriptionPool)
        .where(SubscriptionPool.client_id == client_id, SubscriptionPool.units_consumed < SubscriptionPool.units_purchased)
        .order_by(SubscriptionPool.purchased_at)
        .limit(1)
    )
    pool = result.scalar_one_or_none()
    if pool:
        pool.units_consumed += 1


async def _get_subscription(db: AsyncSession, subscription_id: str, farmer_user_id: str) -> Subscription:
    result = await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == farmer_user_id,
        )
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return sub


async def _package_has_variable(db: AsyncSession, package_id: str, parameter_id: str, variable_id: str) -> bool:
    result = await db.execute(
        select(PackageVariable).where(
            PackageVariable.package_id == package_id,
            PackageVariable.parameter_id == parameter_id,
            PackageVariable.variable_id == variable_id,
        )
    )
    return result.scalar_one_or_none() is not None


def _generate_reference(short_name: str = "") -> str:
    """BL-15: Generate coded reference number — [SHORT_NAME][YY]-[4DIGIT_SEQ]
    e.g. SEEDS26-0047. Falls back to RT-XXXXXXXX if no short_name available."""
    year = str(datetime.now(timezone.utc).year)[2:]
    seq = "".join(secrets.choice(string.digits) for _ in range(4))
    if short_name:
        prefix = short_name.upper()[:8]
        return f"{prefix}{year}-{seq}"
    chars = string.ascii_uppercase + string.digits
    return "RT-" + "".join(secrets.choice(chars) for _ in range(10))


async def _generate_reference_for_sub(db: AsyncSession, client_id: str) -> str:
    """Fetch client short_name and generate coded reference."""
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    short_name = client.short_name if client else ""
    return _generate_reference(short_name)


# ── Farmer: Subscription Payment (RazorPay Rs. 199) ──────────────────────────

@router.post("/farmer/subscriptions/{subscription_id}/payment/create-order")
async def create_payment_order(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a RazorPay order so the farmer can pay Rs. 199 to activate their subscription."""
    from app.services.payment_service import create_subscription_order
    sub = await _get_subscription(db, subscription_id, current_user.id)
    if sub.status == SubscriptionStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Subscription is already active")
    order = create_subscription_order(receipt=subscription_id[:20])
    return order


@router.post("/farmer/subscriptions/{subscription_id}/payment/verify")
async def verify_payment(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Verify RazorPay signature and activate the subscription."""
    from app.services.payment_service import verify_payment_signature
    sub = await _get_subscription(db, subscription_id, current_user.id)
    valid = verify_payment_signature(
        data["razorpay_order_id"],
        data["razorpay_payment_id"],
        data["razorpay_signature"],
    )
    if not valid:
        raise HTTPException(status_code=400, detail="Payment verification failed — invalid signature")

    sub.status = SubscriptionStatus.ACTIVE
    sub.subscription_date = datetime.now(timezone.utc)
    if not sub.reference_number:
        sub.reference_number = await _generate_reference_for_sub(db, sub.client_id)
    await db.commit()
    return {"status": sub.status, "reference_number": sub.reference_number}


# ── Farmer: Alert preferences ─────────────────────────────────────────────────

@router.post("/farmer/subscriptions/{subscription_id}/alert-preferences")
async def set_alert_preferences(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set who receives alerts for this subscription.
    data: { send_to_self: bool, promoter_user_id: str | null }
    """
    sub = await _get_subscription(db, subscription_id, current_user.id)

    # Clear existing recipients
    existing = (await db.execute(
        select(AlertRecipient).where(AlertRecipient.subscription_id == sub.id)
    )).scalars().all()
    for r in existing:
        r.status = "INACTIVE"

    if data.get("send_to_self", True):
        db.add(AlertRecipient(
            subscription_id=sub.id,
            recipient_user_id=current_user.id,
            recipient_type="FARMER",
            status="ACTIVE",
        ))

    if data.get("promoter_user_id"):
        db.add(AlertRecipient(
            subscription_id=sub.id,
            recipient_user_id=data["promoter_user_id"],
            recipient_type="PROMOTER",
            status="ACTIVE",
        ))

    await db.commit()
    return {"detail": "Alert preferences updated"}


@router.get("/farmer/subscriptions/{subscription_id}/alert-preferences")
async def get_alert_preferences(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = await _get_subscription(db, subscription_id, current_user.id)
    result = await db.execute(
        select(AlertRecipient).where(
            AlertRecipient.subscription_id == sub.id,
            AlertRecipient.status == "ACTIVE",
        )
    )
    recipients = result.scalars().all()
    return [
        {"recipient_user_id": r.recipient_user_id, "recipient_type": r.recipient_type}
        for r in recipients
    ]


# ── Dealer/Facilitator: Payment on behalf of farmer ───────────────────────────

@router.post("/dealer/payment-requests/{request_id}/create-order")
async def dealer_create_payment_order(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer/Facilitator creates a RazorPay order to pay Rs. 199 for a farmer."""
    from app.services.payment_service import create_subscription_order
    pr = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.id == request_id,
            SubscriptionPaymentRequest.requested_from_user_id == current_user.id,
            SubscriptionPaymentRequest.status == "PENDING",
        )
    )).scalar_one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Payment request not found or already handled")
    order = create_subscription_order(receipt=request_id[:20])
    return order


@router.post("/dealer/payment-requests/{request_id}/verify")
async def dealer_verify_payment(
    request_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer/Facilitator verifies payment and activates farmer's subscription."""
    from app.services.payment_service import verify_payment_signature
    pr = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.id == request_id,
            SubscriptionPaymentRequest.requested_from_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Payment request not found")

    valid = verify_payment_signature(
        data["razorpay_order_id"],
        data["razorpay_payment_id"],
        data["razorpay_signature"],
    )
    if not valid:
        raise HTTPException(status_code=400, detail="Payment verification failed")

    pr.status = "PAID"
    pr.razorpay_payment_id = data["razorpay_payment_id"]

    # Activate the farmer's subscription
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == pr.subscription_id)
    )).scalar_one_or_none()
    if sub and sub.status == SubscriptionStatus.WAITLISTED:
        sub.status = SubscriptionStatus.ACTIVE
        sub.subscription_date = datetime.now(timezone.utc)
        if not sub.reference_number:
            sub.reference_number = await _generate_reference_for_sub(db, sub.client_id)

    await db.commit()
    return {
        "status": "PAID",
        "subscription_status": sub.status if sub else None,
        "reference_number": sub.reference_number if sub else None,
    }


# ── Farmer: My subscriptions alias (used by PWA home page) ────────────────────

@router.get("/farmer/my-subscriptions")
async def my_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Subscription)
        .where(Subscription.farmer_user_id == current_user.id)
        .order_by(Subscription.created_at.desc())
    )
    subs = result.scalars().all()
    return [
        {
            "id": s.id, "client_id": s.client_id, "package_id": s.package_id,
            "status": s.status, "crop_start_date": s.crop_start_date,
            "reference_number": s.reference_number, "subscription_type": s.subscription_type,
            "farm_area_acres": float(s.farm_area_acres) if s.farm_area_acres is not None else None,
            "area_unit": s.area_unit,
            "farm_area_confirmed_at": s.farm_area_confirmed_at,
        }
        for s in subs
    ]


# ── CHA: Dismiss and history ──────────────────────────────────────────────────

@router.put("/farmer/cha/{entry_id}/dismiss")
async def dismiss_cha(
    entry_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer dismisses a CHA entry (problem resolved or not relevant)."""
    entry = (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.id == entry_id,
            TriggeredCHAEntry.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="CHA entry not found")
    entry.status = "DISMISSED"
    await db.commit()
    return {"status": "DISMISSED"}


@router.get("/farmer/cha-history")
async def get_cha_history(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All triggered CHA entries for this farmer — active and dismissed."""
    result = await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.farmer_user_id == current_user.id
        ).order_by(TriggeredCHAEntry.triggered_at.desc())
    )
    entries = result.scalars().all()
    return [
        {
            "id": e.id,
            "problem_cosh_id": e.problem_cosh_id,
            "problem_name": e.problem_name,
            "recommendation_type": e.recommendation_type,
            "triggered_by": e.triggered_by,
            "triggered_at": e.triggered_at,
            "status": e.status,
        }
        for e in entries
    ]


# ── BL-02: Conditional question answer ────────────────────────────────────────

class ConditionalAnswerRequest(BaseModel):
    subscription_id: str
    question_id: str
    answer: str  # "YES" | "NO" | "BLANK"


@router.post("/farmer/advisory/conditional-answer", status_code=201)
async def submit_conditional_answer(
    request: ConditionalAnswerRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-02: Store farmer's YES/NO answer to a conditional question for today."""
    from datetime import date
    if request.answer not in ("YES", "NO", "BLANK"):
        raise HTTPException(status_code=422, detail="answer must be YES, NO, or BLANK")

    today = date.today()

    # Upsert: replace today's answer if already exists
    existing = (await db.execute(
        select(ConditionalAnswer).where(
            ConditionalAnswer.subscription_id == request.subscription_id,
            ConditionalAnswer.question_id == request.question_id,
            ConditionalAnswer.answer_date == today,
        )
    )).scalar_one_or_none()

    if existing:
        existing.answer = request.answer
    else:
        db.add(ConditionalAnswer(
            subscription_id=request.subscription_id,
            question_id=request.question_id,
            answer_date=today,
            answer=request.answer,
        ))

    await db.commit()
    return {"detail": "Answer recorded", "answer": request.answer}


# ── Farmer: Daily advisory (BL-02 + BL-03 + BL-04 + triggered CHA) ────────────

@router.get("/farmer/advisory/today")
async def get_today_advisory(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return today's active practices for all the farmer's ACTIVE subscriptions.
    Applies BL-04 (DAS/DBS window) + BL-02 (conditional filtering) +
    BL-03 (deduplication across CCA + triggered CHA timelines).
    """
    today = date.today()

    # All ACTIVE subscriptions with a crop_start_date
    subs_result = await db.execute(
        select(Subscription).where(
            Subscription.farmer_user_id == current_user.id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.crop_start_date != None,  # noqa: E711
        )
    )
    subs = subs_result.scalars().all()
    if not subs:
        return []

    out = []
    for sub in subs:
        # Get the active package
        pkg_result = await db.execute(
            select(Package).where(Package.id == sub.package_id, Package.status == "ACTIVE")
        )
        pkg = pkg_result.scalar_one_or_none()
        if not pkg:
            continue

        crop_start = sub.crop_start_date.date() if hasattr(sub.crop_start_date, 'date') else sub.crop_start_date
        day_offset = (today - crop_start).days  # positive = days after sowing

        # Load all timelines for this package
        tl_result = await db.execute(
            select(Timeline).where(Timeline.package_id == pkg.id).order_by(Timeline.display_order)
        )
        timelines = tl_result.scalars().all()

        # ── Phase 3: load existing snapshots for this subscription ────────────
        # If a snapshot exists for a timeline, its frozen window drives BL-04
        # (Rule 3) and its frozen content drives rendering (Rules 1 & 2).
        from app.modules.subscriptions.snapshot_models import LockedTimelineSnapshot
        from app.services.snapshot_render import (
            cca_calendar_dates, cca_window_active,
            cha_calendar_dates,
            metadata_from_content, metadata_from_master_cca,
            render_cca_from_content, render_cha_from_content,
            resolve_cca_content, resolve_cha_content,
        )

        existing_cca_snaps = (await db.execute(
            select(LockedTimelineSnapshot).where(
                LockedTimelineSnapshot.subscription_id == sub.id,
                LockedTimelineSnapshot.source == "CCA",
            )
        )).scalars().all()
        cca_snap_by_tl: dict = {s.timeline_id: s for s in existing_cca_snaps}

        active_timelines = []
        for tl in timelines:
            existing_snap = cca_snap_by_tl.get(tl.id)
            meta = (
                metadata_from_content(existing_snap.content)
                if existing_snap is not None
                else metadata_from_master_cca(tl)
            )
            if cca_window_active(meta, day_offset):
                active_timelines.append((tl, day_offset, meta))

        from app.services.bl03_deduplication import (
            deduplicate_advisory, TimelineWindow as TLWindow,
            PracticeStub as PStub, PracticeElement as PEl,
        )
        from app.modules.orders.models import Order, OrderItem
        from datetime import timedelta

        if not active_timelines:
            out.append({
                "subscription_id": sub.id,
                "client_id": sub.client_id,
                "package_id": sub.package_id,
                "package_name": pkg.name,
                "crop_cosh_id": pkg.crop_cosh_id,
                "crop_start_date": sub.crop_start_date,
                "day_offset": day_offset,
                "reference_number": sub.reference_number,
                "timelines": [],
            })
            continue

        # ── Load today's conditional answers for this subscription ────────────
        cond_rows = (await db.execute(
            select(ConditionalAnswer).where(
                ConditionalAnswer.subscription_id == sub.id,
                ConditionalAnswer.answer_date == today,
            )
        )).scalars().all()
        today_answers: dict[str, str] = {r.question_id: r.answer for r in cond_rows}

        # ── Build CCA timeline stubs from snapshot content (Rules 1-3) ──────
        tl_windows: list[TLWindow] = []
        tl_date_map: dict = {}   # id → (from_date, to_date, day_num)
        pending_questions_by_tl: dict = {}   # tl.id → {question info}
        blank_paths_by_tl: dict = {}         # tl.id → list of {question_id, question_text, farmer_answer}

        for tl, day_num, meta in active_timelines:
            # Snapshot is the source of truth for content. If missing,
            # resolve_cca_content takes one synchronously (lock-on-view) so
            # downstream rendering always reads frozen data.
            content, _locked = await resolve_cca_content(db, sub.id, tl.id)
            rendered = render_cca_from_content(content, today_answers)

            if rendered.pending_question:
                pending_questions_by_tl[tl.id] = rendered.pending_question
            if rendered.blank_paths:
                blank_paths_by_tl[tl.id] = rendered.blank_paths

            from_d, to_d = cca_calendar_dates(meta, crop_start)

            tl_window = TLWindow(
                id=tl.id,
                name=(content.get("timeline") or {}).get("name") or tl.name,
                from_date=from_d, to_date=to_d,
                created_at=tl.created_at.date() if hasattr(tl.created_at, 'date') else today,
                practices=rendered.practice_stubs, source="CCA",
            )
            tl_windows.append(tl_window)
            tl_date_map[tl.id] = (from_d, to_d, day_num)

        # ── Load triggered CHA timelines (from diagnosis or FarmPundit queries) ─
        cha_entries = (await db.execute(
            select(TriggeredCHAEntry).where(
                TriggeredCHAEntry.subscription_id == sub.id,
                TriggeredCHAEntry.status == "ACTIVE",
            )
        )).scalars().all()

        for cha in cha_entries:
            if cha.recommendation_type == "SP":
                sp_timelines = (await db.execute(
                    select(SPTimeline).where(SPTimeline.sp_recommendation_id == cha.recommendation_id)
                )).scalars().all()
                for sp_tl in sp_timelines:
                    # CHA window check uses snapshot's frozen offsets if a
                    # snapshot exists, else master.
                    sp_snap = (await db.execute(
                        select(LockedTimelineSnapshot).where(
                            LockedTimelineSnapshot.subscription_id == sub.id,
                            LockedTimelineSnapshot.timeline_id == sp_tl.id,
                            LockedTimelineSnapshot.source == "SP",
                        )
                    )).scalar_one_or_none()
                    if sp_snap is not None:
                        meta = metadata_from_content(sp_snap.content)
                    else:
                        meta = metadata_from_content({"timeline": {
                            "from_type": "DAS",
                            "from_value": int(sp_tl.from_value),
                            "to_value": int(sp_tl.to_value),
                        }})
                    from_d, to_d = cha_calendar_dates(meta, cha.triggered_at.date())
                    if not (from_d <= today <= to_d):
                        continue  # Not active today
                    content, _locked = await resolve_cha_content(db, sub.id, sp_tl.id, "SP")
                    stubs = render_cha_from_content(content)
                    cha_tl_id = f"cha-sp-{sp_tl.id}"
                    problem_label = cha.problem_name or problem_cosh_id
                    tl_windows.append(TLWindow(
                        id=cha_tl_id, name=f"CHA — {problem_label}: {sp_tl.name}",
                        from_date=from_d, to_date=to_d,
                        created_at=cha.triggered_at.date() if hasattr(cha.triggered_at, 'date') else today,
                        practices=stubs, source="CHA",
                    ))
                    tl_date_map[cha_tl_id] = (from_d, to_d, 0)
            elif cha.recommendation_type == "PG":
                pg_timelines = (await db.execute(
                    select(PGTimeline).where(PGTimeline.pg_recommendation_id == cha.recommendation_id)
                )).scalars().all()
                for pg_tl in pg_timelines:
                    pg_snap = (await db.execute(
                        select(LockedTimelineSnapshot).where(
                            LockedTimelineSnapshot.subscription_id == sub.id,
                            LockedTimelineSnapshot.timeline_id == pg_tl.id,
                            LockedTimelineSnapshot.source == "PG",
                        )
                    )).scalar_one_or_none()
                    if pg_snap is not None:
                        meta = metadata_from_content(pg_snap.content)
                    else:
                        meta = metadata_from_content({"timeline": {
                            "from_type": "DAS",
                            "from_value": int(pg_tl.from_value),
                            "to_value": int(pg_tl.to_value),
                        }})
                    from_d, to_d = cha_calendar_dates(meta, cha.triggered_at.date())
                    if not (from_d <= today <= to_d):
                        continue
                    content, _locked = await resolve_cha_content(db, sub.id, pg_tl.id, "PG")
                    stubs = render_cha_from_content(content)
                    cha_tl_id = f"cha-pg-{pg_tl.id}"
                    problem_label = cha.problem_name or problem_cosh_id
                    tl_windows.append(TLWindow(
                        id=cha_tl_id, name=f"CHA — {problem_label}: {pg_tl.name}",
                        from_date=from_d, to_date=to_d,
                        created_at=cha.triggered_at.date() if hasattr(cha.triggered_at, 'date') else today,
                        practices=stubs, source="CHA",
                    ))
                    tl_date_map[cha_tl_id] = (from_d, to_d, 0)

        # ── BL-03 deduplication across CCA + CHA timelines ───────────────────
        order_result = await db.execute(select(Order).where(Order.subscription_id == sub.id))
        approved_ids: set[str] = set()
        for order in order_result.scalars().all():
            items_result = await db.execute(
                select(OrderItem).where(OrderItem.order_id == order.id, OrderItem.status == "APPROVED")
            )
            for item in items_result.scalars().all():
                approved_ids.add(item.practice_id)

        deduped = deduplicate_advisory(tl_windows, approved_practice_ids=approved_ids)

        # ── Build response ────────────────────────────────────────────────────
        timeline_data = []
        for dedup_tl in deduped:
            tl = dedup_tl.timeline
            from_d, to_d, day_num = tl_date_map[tl.id]
            # Frequency filter: hide frequency-based practices that aren't due today.
            # Non-frequency practices (frequency_days NULL) are always shown if in window.
            freq_filtered_practices = [
                p for p in dedup_tl.visible_practices
                if _is_frequency_due_today(p.frequency_days, from_d, today)
            ]
            tl_entry: dict = {
                "id": tl.id,
                "name": tl.name,
                "source": tl.source,  # CCA | CHA
                "from_date": from_d.isoformat(),
                "to_date": to_d.isoformat(),
                "day_number": day_num,
                "suppressed_count": len(dedup_tl.suppressed),
                "practices": [
                    {
                        "id": p.id, "l0_type": p.l0_type,
                        "l1_type": p.l1_type, "l2_type": p.l2_type,
                        "display_order": p.display_order, "is_special_input": p.is_special_input,
                        "relation_id": p.relation_id,
                        "relation_role": p.relation_role,
                        "relation_type": p.relation_type,
                        "frequency_days": p.frequency_days,
                        "is_frequency_due_today": True,  # always True — list is already filtered
                        "elements": [{"element_type": el.element_type, "cosh_ref": el.cosh_ref,
                                      "value": el.value, "unit_cosh_id": el.unit_cosh_id}
                                     for el in p.elements],
                    }
                    for p in freq_filtered_practices
                ],
            }
            # Include BL-02 pending question for this timeline (if any)
            if tl.id in pending_questions_by_tl:
                tl_entry["pending_conditional_question"] = pending_questions_by_tl[tl.id]
                tl_entry["has_pending_question"] = True
            # Per spec §6.4: blank-path questions for this timeline (named, with farmer's answer)
            if tl.id in blank_paths_by_tl:
                tl_entry["blank_path_questions"] = blank_paths_by_tl[tl.id]
            timeline_data.append(tl_entry)

        out.append({
            "subscription_id": sub.id,
            "client_id": sub.client_id,
            "package_id": sub.package_id,
            "package_name": pkg.name,
            "crop_cosh_id": pkg.crop_cosh_id,
            "crop_start_date": sub.crop_start_date,
            "day_offset": day_offset,
            "reference_number": sub.reference_number,
            "timelines": timeline_data,
        })

        # VIEWED-locks are now taken inline by resolve_cca_content /
        # resolve_cha_content before each timeline is rendered (Phase 3).
        # The Phase 2 trailing call has been removed.

    return out


# ── Farmer: Unsubscribe ───────────────────────────────────────────────────────

@router.put("/farmer/subscriptions/{subscription_id}/unsubscribe")
async def unsubscribe(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Self-subscribed: cancel freely. Company-assigned: returns 400 (request required)."""
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Active subscription not found")

    if sub.subscription_type == SubscriptionType.SELF:
        sub.status = SubscriptionStatus.CANCELLED
        await db.commit()
        return {"detail": "Unsubscribed successfully", "status": sub.status}
    else:
        raise HTTPException(
            status_code=400,
            detail="Company-assigned subscriptions cannot be cancelled by the farmer. Please contact your company."
        )


# ── Farmer: Active advisories in district ─────────────────────────────────────

@router.get("/farmer/active-advisories-in-district")
async def active_advisories_in_district(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns active packages from clients serving the farmer's district."""
    farmer_district = current_user.district_cosh_id
    if not farmer_district:
        return []

    location_result = await db.execute(
        select(ClientLocation).where(
            ClientLocation.district_cosh_id == farmer_district,
            ClientLocation.status == "ACTIVE",
        )
    )
    client_ids = list({loc.client_id for loc in location_result.scalars().all()})
    if not client_ids:
        return []

    # Get active packages for these clients
    pkg_result = await db.execute(
        select(Package).where(
            Package.client_id.in_(client_ids),
            Package.status == "ACTIVE",
        ).order_by(Package.name)
    )
    packages = pkg_result.scalars().all()

    # Get farmer's already subscribed client+package combos to exclude them
    sub_result = await db.execute(
        select(Subscription).where(
            Subscription.farmer_user_id == current_user.id,
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.WAITLISTED]),
        )
    )
    existing_pkg_ids = {s.package_id for s in sub_result.scalars().all()}

    out = []
    for pkg in packages:
        if pkg.id in existing_pkg_ids:
            continue
        client = (await db.execute(select(Client).where(Client.id == pkg.client_id))).scalar_one_or_none()
        out.append({
            "package_id": pkg.id,
            "package_name": pkg.name,
            "crop_cosh_id": pkg.crop_cosh_id,
            "client_id": pkg.client_id,
            "company_name": client.display_name or client.full_name if client else None,
            "company_logo": client.logo_url if client else None,
            "primary_colour": client.primary_colour if client else None,
        })
    return out


# ── Farmer: Nearby dealers (for Ordering Screen) ─────────────────────────────

@router.get("/farmer/subscriptions/{subscription_id}/nearby-dealers")
async def nearby_dealers_for_farmer(
    subscription_id: str,
    order_type: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns up to 5 nearest dealers + Promoter pinned first.
    order_type: PESTICIDE | FERTILISER | SEED — filters by sell_categories."""
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    farmer_lat = lat or (float(current_user.gps_lat) if current_user.gps_lat else 0.0)
    farmer_lng = lng or (float(current_user.gps_lng) if current_user.gps_lng else 0.0)

    promoter_user_id = await _get_promoter(db, subscription_id, PromoterType.DEALER)

    category_map = {"PESTICIDE": "PESTICIDES", "FERTILISER": "FERTILISERS", "SEED": "SEEDS"}
    required_cat = category_map.get(order_type or "") if order_type else None

    profiles = (await db.execute(select(DealerProfile))).scalars().all()
    results = []
    for profile in profiles:
        if required_cat and required_cat not in (profile.sell_categories or []):
            continue
        if not profile.shop_gps_lat or not profile.shop_gps_lng:
            continue
        dist = _haversine_sub(farmer_lat, farmer_lng,
                              float(profile.shop_gps_lat), float(profile.shop_gps_lng))
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
                "is_promoter": dealer.id == promoter_user_id,
            })

    results.sort(key=lambda x: (0 if x["is_promoter"] else 1, x["distance_km"]))
    return results[:5]


@router.get("/farmer/subscriptions/{subscription_id}/nearby-facilitators")
async def nearby_facilitators_for_farmer(
    subscription_id: str,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns up to 5 nearest facilitators + Promoter pinned first."""
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    farmer_lat = lat or (float(current_user.gps_lat) if current_user.gps_lat else 0.0)
    farmer_lng = lng or (float(current_user.gps_lng) if current_user.gps_lng else 0.0)

    promoter_user_id = await _get_promoter(db, subscription_id, PromoterType.FACILITATOR)

    facilitator_role_rows = (await db.execute(
        select(UserRole).where(UserRole.role_type == RoleType.FACILITATOR)
    )).scalars().all()
    facilitator_ids = {r.user_id for r in facilitator_role_rows}

    results = []
    for uid in facilitator_ids:
        fac = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
        if not fac or not fac.gps_lat or not fac.gps_lng:
            continue
        dist = _haversine_sub(farmer_lat, farmer_lng,
                              float(fac.gps_lat), float(fac.gps_lng))
        results.append({
            "user_id": fac.id,
            "name": fac.name,
            "phone": fac.phone,
            "distance_km": round(dist, 1),
            "is_promoter": fac.id == promoter_user_id,
        })

    results.sort(key=lambda x: (0 if x["is_promoter"] else 1, x["distance_km"]))
    return results[:5]


# ── Farmer: Pre-start inputs (DBS practices + seed varieties) ─────────────────

@router.get("/farmer/subscriptions/{subscription_id}/pre-start-inputs")
async def get_pre_start_inputs(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns DBS (days before sowing) INPUT practices for pre-start ordering."""
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    tl_result = await db.execute(
        select(Timeline).where(
            Timeline.package_id == sub.package_id,
        )
    )
    timelines = tl_result.scalars().all()

    dbs_timelines = [tl for tl in timelines if tl.from_type.value == "DBS"]

    out = []
    for tl in dbs_timelines:
        practices = (await db.execute(
            select(Practice).where(Practice.timeline_id == tl.id).order_by(Practice.display_order)
        )).scalars().all()
        input_practices = [p for p in practices if p.l0_type.value == "INPUT"]
        if input_practices:
            out.append({
                "timeline_id": tl.id,
                "timeline_name": tl.name,
                "days_before_sowing_from": tl.from_value,
                "days_before_sowing_to": tl.to_value,
                "practices": [
                    {
                        "id": p.id,
                        "l0_type": p.l0_type.value,
                        "l1_type": p.l1_type,
                        "l2_type": p.l2_type,
                        "display_order": p.display_order,
                    }
                    for p in input_practices
                ],
            })
    return out


# ── Farmer: Missed items (expired practices) ──────────────────────────────────

@router.get("/farmer/subscriptions/{subscription_id}/missed-items")
async def get_missed_items(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns practices whose application window has fully passed."""
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    if not sub.crop_start_date:
        return []

    today = datetime.now(timezone.utc).date()
    crop_start = sub.crop_start_date.date() if hasattr(sub.crop_start_date, 'date') else sub.crop_start_date
    day_offset = (today - crop_start).days

    tl_result = await db.execute(
        select(Timeline).where(Timeline.package_id == sub.package_id)
    )
    timelines = tl_result.scalars().all()

    missed = []
    for tl in timelines:
        is_missed = False
        window_end = None
        if tl.from_type.value == "DAS":
            if day_offset > tl.to_value:
                is_missed = True
                window_end = crop_start + timedelta(days=tl.to_value)
        elif tl.from_type.value == "DBS":
            if day_offset > -tl.to_value:
                is_missed = True
                window_end = crop_start - timedelta(days=tl.to_value)

        if is_missed:
            practices = (await db.execute(
                select(Practice).where(Practice.timeline_id == tl.id).order_by(Practice.display_order)
            )).scalars().all()
            if practices:
                missed.append({
                    "timeline_id": tl.id,
                    "timeline_name": tl.name,
                    "from_type": tl.from_type.value,
                    "from_value": tl.from_value,
                    "to_value": tl.to_value,
                    "window_end": window_end,
                    "practices": [
                        {
                            "id": p.id,
                            "l0_type": p.l0_type.value,
                            "l1_type": p.l1_type,
                            "l2_type": p.l2_type,
                        }
                        for p in practices
                    ],
                })
    return missed


# ── Farmer: Expert setting (mode + available experts) ────────────────────────

@router.get("/farmer/subscriptions/{subscription_id}/expert-setting")
async def get_expert_setting(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns current expert preference + Promoter-Pundit if any + company's available pundits."""
    from app.modules.farmpundit.models import (
        FarmPunditPreference, FarmPunditProfile, ClientFarmPundit,
    )
    from app.modules.clients.models import ClientPromoter

    sub = await _get_subscription(db, subscription_id, current_user.id)

    # 1) Current preference (farmer's specific choice for this subscription)
    pref = (await db.execute(
        select(FarmPunditPreference).where(FarmPunditPreference.subscription_id == subscription_id)
    )).scalar_one_or_none()

    preferred_pundit = None
    if pref:
        row = (await db.execute(
            select(FarmPunditProfile, User)
            .join(User, User.id == FarmPunditProfile.user_id)
            .where(FarmPunditProfile.id == pref.pundit_id)
        )).first()
        if row:
            profile, user_obj = row
            preferred_pundit = {
                "pundit_id": profile.id,
                "name": user_obj.name,
                "phone": user_obj.phone,
            }

    # 2) Promoter-Pundit (active promoter who is also marked as Promoter-Pundit on this client)
    promoter_pundit = None
    try:
        assignment = (await db.execute(
            select(PromoterAssignment).where(
                PromoterAssignment.subscription_id == subscription_id,
                PromoterAssignment.status == AssignmentStatus.ACTIVE,
            )
        )).scalar_one_or_none()
        if assignment:
            cp = (await db.execute(
                select(ClientPromoter).where(
                    ClientPromoter.user_id == assignment.promoter_user_id,
                    ClientPromoter.client_id == sub.client_id,
                )
            )).scalar_one_or_none()
            # Also find their FarmPundit profile and the ClientFarmPundit (is_promoter_pundit lives there)
            promoter_user = (await db.execute(
                select(User).where(User.id == assignment.promoter_user_id)
            )).scalar_one_or_none()
            pp_profile = None
            if promoter_user:
                pp_profile = (await db.execute(
                    select(FarmPunditProfile).where(FarmPunditProfile.user_id == promoter_user.id)
                )).scalar_one_or_none()
            cfp = None
            if pp_profile:
                cfp = (await db.execute(
                    select(ClientFarmPundit).where(
                        ClientFarmPundit.client_id == sub.client_id,
                        ClientFarmPundit.pundit_id == pp_profile.id,
                        ClientFarmPundit.is_promoter_pundit == True,  # noqa: E712
                        ClientFarmPundit.status == "ACTIVE",
                    )
                )).scalar_one_or_none()
            if cp and cfp and pp_profile and promoter_user:
                promoter_pundit = {
                    "pundit_id": pp_profile.id,
                    "name": promoter_user.name,
                    "phone": promoter_user.phone,
                }
    except Exception:
        promoter_pundit = None

    # 3) Company's available experts (FarmPundits onboarded by this client)
    company_experts = []
    try:
        rows = (await db.execute(
            select(ClientFarmPundit, FarmPunditProfile, User)
            .join(FarmPunditProfile, FarmPunditProfile.id == ClientFarmPundit.pundit_id)
            .join(User, User.id == FarmPunditProfile.user_id)
            .where(
                ClientFarmPundit.client_id == sub.client_id,
                ClientFarmPundit.status == "ACTIVE",
            )
        )).all()
        company_experts = [
            {
                "pundit_id": p.id,
                "name": u.name,
                "phone": u.phone,
                "role": link.role.value if hasattr(link.role, "value") else link.role,
            }
            for link, p, u in rows
        ]
    except Exception:
        company_experts = []

    if pref:
        mode = "SPECIFIC"
    elif promoter_pundit:
        mode = "PROMOTER_PUNDIT"
    else:
        mode = "REGULAR_TEAM"

    return {
        "mode": mode,
        "preferred_pundit": preferred_pundit,
        "promoter_pundit": promoter_pundit,
        "company_experts": company_experts,
    }


# ── Farmer: Seed availability check ──────────────────────────────────────────

@router.get("/farmer/subscriptions/{subscription_id}/seed-availability")
async def check_seed_availability(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns true if any seed/seedling varieties are linked to this subscription's PoP."""
    from app.modules.seed_mgmt.models import VarietyPoP
    sub = await _get_subscription(db, subscription_id, current_user.id)
    count = (await db.execute(
        select(func.count(VarietyPoP.id)).where(
            VarietyPoP.package_id == sub.package_id,
            VarietyPoP.status == "ACTIVE",
        )
    )).scalar() or 0
    return {"has_varieties": count > 0, "count": int(count)}


# ── Farmer: Update tentative/soft-confirmed farm area (does not lock) ────────

@router.put("/farmer/subscriptions/{subscription_id}/farm-area")
async def update_farm_area(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update tentative or soft-confirmed farm area. Rejected if hard-locked."""
    sub = await _get_subscription(db, subscription_id, current_user.id)
    if sub.farm_area_confirmed_at:
        raise HTTPException(status_code=400, detail="Farm area is locked and cannot be changed")
    new_area = data.get("farm_area_acres")
    if new_area is None:
        raise HTTPException(status_code=422, detail="farm_area_acres required")
    sub.farm_area_acres = new_area
    if data.get("area_unit"):
        sub.area_unit = data["area_unit"]
    await db.commit()
    return {
        "farm_area_acres": float(sub.farm_area_acres),
        "area_unit": sub.area_unit,
        "farm_area_confirmed_at": sub.farm_area_confirmed_at,
    }


# ── Farmer: Confirm farm area (locks it in) ──────────────────────────────────

@router.post("/farmer/subscriptions/{subscription_id}/farm-area/confirm")
async def confirm_farm_area(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Locks in the farm area. Required at start date or first DAS order."""
    sub = await _get_subscription(db, subscription_id, current_user.id)
    new_area = data.get("farm_area_acres")
    if new_area is not None:
        sub.farm_area_acres = new_area
    if data.get("area_unit"):
        sub.area_unit = data["area_unit"]
    if not sub.farm_area_acres:
        raise HTTPException(status_code=422, detail="farm_area_acres required to confirm")
    sub.farm_area_confirmed_at = datetime.now(timezone.utc)
    await db.commit()
    return {
        "farm_area_acres": float(sub.farm_area_acres),
        "area_unit": sub.area_unit,
        "confirmed_at": sub.farm_area_confirmed_at,
    }


# ── Farmer: Buy-all DBS pesticides or fertilisers (single consolidated order) ──

@router.post("/farmer/subscriptions/{subscription_id}/orders/buy-all-dbs")
async def buy_all_dbs(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a single consolidated order for all DBS pesticides OR fertilisers across all DBS timelines.
    data: { category: 'PESTICIDE' | 'FERTILISER', dealer_user_id?, facilitator_user_id? }
    """
    from datetime import date as dt_date, timedelta
    from app.modules.orders.models import Order, OrderItem, OrderStatus, OrderItemStatus

    sub = await _get_subscription(db, subscription_id, current_user.id)
    category = (data or {}).get("category")
    if category not in ("PESTICIDE", "FERTILISER"):
        raise HTTPException(status_code=422, detail="category must be PESTICIDE or FERTILISER")

    # ── Acreage soft-confirm on first DBS order ───────────────────────────
    # Tentative → Soft confirmed. Allow update unless already hard-locked.
    new_acreage = (data or {}).get("farm_area_acres")
    new_unit = (data or {}).get("area_unit") or "acres"

    if not sub.farm_area_acres:
        # First time — must provide acreage
        if not new_acreage:
            raise HTTPException(status_code=422, detail="farm_area_acres required for first order")
        sub.farm_area_acres = new_acreage
        sub.area_unit = new_unit
    elif new_acreage:
        # Soft update allowed (not yet hard-locked)
        if sub.farm_area_confirmed_at:
            raise HTTPException(status_code=400, detail="Farm area is locked and cannot be changed")
        sub.farm_area_acres = new_acreage
        sub.area_unit = new_unit
    # Note: do NOT set farm_area_confirmed_at — DBS is soft confirm only

    # Find all DBS timelines for this PoP
    timelines = (await db.execute(
        select(Timeline).where(Timeline.package_id == sub.package_id)
    )).scalars().all()
    dbs_timelines = [tl for tl in timelines if tl.from_type.value == "DBS"]
    if not dbs_timelines:
        raise HTTPException(status_code=400, detail="No DBS practices for this advisory")

    # Collect input practices matching the category
    matching_practices: list[Practice] = []
    for tl in dbs_timelines:
        practices = (await db.execute(
            select(Practice).where(Practice.timeline_id == tl.id).order_by(Practice.display_order)
        )).scalars().all()
        for p in practices:
            if p.l0_type.value != "INPUT":
                continue
            l1 = (p.l1_type or "").upper()
            if category == "PESTICIDE" and "PEST" in l1:
                matching_practices.append(p)
            elif category == "FERTILISER" and ("FERT" in l1 or "FERTI" in l1):
                matching_practices.append(p)

    if not matching_practices:
        raise HTTPException(status_code=400, detail=f"No DBS {category.lower()} practices found")

    # ── Timeline-type integrity sanity check (defensive) ──────────────────
    # All matching practices were filtered from dbs_timelines, so all of
    # their timelines are guaranteed to be DBS. We re-verify here so the
    # type-isolation guarantee is explicit and any future regression is
    # caught early.
    dbs_tl_ids = {tl.id for tl in dbs_timelines}
    bad = [p for p in matching_practices if p.timeline_id not in dbs_tl_ids]
    if bad:
        raise HTTPException(
            status_code=500,
            detail="Internal error: non-DBS practice slipped into DBS-only order",
        )

    # ── Relation completeness expansion ──────────────────────────────────
    # Per Practice Relations spec §8: when ordering a relation, ALL practices
    # from ALL Options of ALL Parts go in — the dealer resolves which Option
    # to fulfil per Part. So for any matched practice that participates in a
    # relation, pull in its sibling INPUT practices (DBS-bound only) even if
    # they don't match the category filter (e.g. a Pesticide OR-alternative
    # to a Fertiliser). The order_type stays as requested by the farmer; the
    # dealer side handles the mixed-category case (TODO Build C: dealer UI
    # may need to recognise mixed-category siblings in a relation order).
    relation_ids_in_set = {p.relation_id for p in matching_practices if p.relation_id}
    if relation_ids_in_set:
        matched_ids = {p.id for p in matching_practices}
        sibling_practices = (await db.execute(
            select(Practice).where(
                Practice.relation_id.in_(relation_ids_in_set),
                Practice.id.notin_(matched_ids),
            )
        )).scalars().all()
        for sp in sibling_practices:
            l0 = sp.l0_type.value if hasattr(sp.l0_type, 'value') else str(sp.l0_type)
            if l0 == "INPUT" and sp.timeline_id in dbs_tl_ids:
                matching_practices.append(sp)

    # ── Date-range computation ────────────────────────────────────────────
    # If crop_start_date is set, derive a focused buying window from the
    # actual DBS values of the practices being ordered (DBS values are
    # days BEFORE sowing; larger value = earlier date).
    # If not set, fall back to a generic today + 14 days window.
    today = dt_date.today()
    relevant_tl_ids = {p.timeline_id for p in matching_practices}
    relevant_dbs_pairs = [
        (int(tl.from_value), int(tl.to_value))
        for tl in dbs_timelines if tl.id in relevant_tl_ids
    ]

    if sub.crop_start_date and relevant_dbs_pairs:
        start = sub.crop_start_date.date() if hasattr(sub.crop_start_date, 'date') else sub.crop_start_date
        # from_value is the larger # of days before sowing (earliest);
        # to_value is the smaller # of days before sowing (latest).
        earliest_buy = start - timedelta(days=max(v[0] for v in relevant_dbs_pairs))
        latest_buy = start - timedelta(days=min(v[1] for v in relevant_dbs_pairs))
        date_from = max(today, earliest_buy)
        date_to = max(date_from, latest_buy)
    else:
        date_from = today
        date_to = today + timedelta(days=14)

    order = Order(
        subscription_id=subscription_id,
        farmer_user_id=current_user.id,
        client_id=sub.client_id,
        dealer_user_id=(data or {}).get("dealer_user_id"),
        facilitator_user_id=(data or {}).get("facilitator_user_id"),
        date_from=date_from,
        date_to=date_to,
        status=OrderStatus.SENT,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    db.add(order)
    await db.flush()

    # Resolve relation_type per relation_id once (to avoid N queries)
    rel_type_map: dict[str, str] = {}
    rel_ids_for_items = {p.relation_id for p in matching_practices if p.relation_id}
    if rel_ids_for_items:
        from app.modules.advisory.models import Relation
        rel_rows = (await db.execute(
            select(Relation).where(Relation.id.in_(rel_ids_for_items))
        )).scalars().all()
        rel_type_map = {
            r.id: (r.relation_type.value if hasattr(r.relation_type, 'value') else str(r.relation_type))
            for r in rel_rows
        }

    # ── Take snapshots BEFORE creating items (Phase 3.2) ─────────────────
    # Items carry a permanent pointer to the locked snapshot.
    from app.services.snapshot import take_snapshot
    import logging as _logging
    _po_logger = _logging.getLogger(__name__)

    timeline_ids_in_order = {p.timeline_id for p in matching_practices if p.timeline_id}
    snap_id_by_tl: dict[str, Optional[str]] = {}
    for tl_id in timeline_ids_in_order:
        try:
            snap = await take_snapshot(
                db, subscription_id, tl_id, "PURCHASE_ORDER", source="CCA",
            )
            snap_id_by_tl[tl_id] = snap.id
        except Exception as exc:  # noqa: BLE001 — best-effort
            _po_logger.warning(
                "PO snapshot capture failed sub=%s tl=%s: %s",
                subscription_id, tl_id, exc,
            )
            snap_id_by_tl[tl_id] = None

    for p in matching_practices:
        db.add(OrderItem(
            order_id=order.id,
            practice_id=p.id,
            timeline_id=p.timeline_id,
            relation_id=p.relation_id,
            relation_type=rel_type_map.get(p.relation_id) if p.relation_id else None,
            relation_role=p.relation_role,
            snapshot_id=snap_id_by_tl.get(p.timeline_id),
            status=OrderItemStatus.PENDING,
        ))

    await db.commit()
    await db.refresh(order)
    return {
        "order_id": order.id,
        "item_count": len(matching_practices),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "category": category,
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_promoter(db, subscription_id: str, promoter_type: PromoterType) -> Optional[str]:
    result = (await db.execute(
        select(PromoterAssignment).where(
            PromoterAssignment.subscription_id == subscription_id,
            PromoterAssignment.promoter_type == promoter_type,
            PromoterAssignment.status == AssignmentStatus.ACTIVE,
        ).order_by(PromoterAssignment.assigned_at.desc()).limit(1)
    )).scalar_one_or_none()
    return result.promoter_user_id if result else None


def _haversine_sub(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return R * 2 * asin(sqrt(a))


# ── Phase 4.1: Admin debug endpoints for snapshots ──────────────────────────
#
# Two read-only endpoints for the SA support team. When a farmer reports
# "I'm seeing old advice" or a dealer says "the brand-lock is wrong",
# the SA can call these to verify exactly what was frozen at lock time
# (and when, and why) for the affected subscription.

def _require_sa_for_snapshots(current_user: User):
    from app.config import settings as _settings
    if current_user.email != _settings.sa_email:
        raise HTTPException(
            status_code=403, detail="Super Admin access required"
        )


@router.get("/admin/subscriptions/{subscription_id}/snapshots")
async def admin_list_subscription_snapshots(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA debug — list every locked-timeline snapshot recorded for one
    subscription, with timestamps and triggers. Content body is *not*
    returned here (use the per-snapshot endpoint for that)."""
    _require_sa_for_snapshots(current_user)

    from app.modules.subscriptions.snapshot_models import LockedTimelineSnapshot
    rows = (await db.execute(
        select(LockedTimelineSnapshot)
        .where(LockedTimelineSnapshot.subscription_id == subscription_id)
        .order_by(LockedTimelineSnapshot.locked_at.asc())
    )).scalars().all()

    return [
        {
            "id": s.id,
            "subscription_id": s.subscription_id,
            "timeline_id": s.timeline_id,
            "source": s.source,                  # CCA | PG | SP
            "lock_trigger": s.lock_trigger,      # PURCHASE_ORDER | VIEWED | BACKFILL
            "locked_at": s.locked_at,
            "schema_version": (s.content or {}).get("schema_version"),
            "practice_count": len((s.content or {}).get("practices") or []),
        }
        for s in rows
    ]


@router.get("/admin/snapshots/{snapshot_id}")
async def admin_get_snapshot(
    snapshot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA debug — full content of a single snapshot. Returns the entire
    JSONB payload so the SA can compare against current master tables."""
    _require_sa_for_snapshots(current_user)

    from app.modules.subscriptions.snapshot_models import LockedTimelineSnapshot
    row = (await db.execute(
        select(LockedTimelineSnapshot)
        .where(LockedTimelineSnapshot.id == snapshot_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    return {
        "id": row.id,
        "subscription_id": row.subscription_id,
        "timeline_id": row.timeline_id,
        "source": row.source,
        "lock_trigger": row.lock_trigger,
        "locked_at": row.locked_at,
        "content": row.content,
    }
