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

        active_timelines = []
        for tl in timelines:
            # BL-04 window logic
            if tl.from_type.value == "DAS":
                # Days After Sowing: active when from_value <= day_offset <= to_value
                if tl.from_value <= day_offset <= tl.to_value:
                    active_timelines.append((tl, day_offset))
            elif tl.from_type.value == "DBS":
                # Days Before Sowing: active when negative day_offset falls in window
                # from_value and to_value are positive "days before sowing"
                # window: -to_value <= day_offset <= -from_value
                if -tl.to_value <= day_offset <= -tl.from_value:
                    active_timelines.append((tl, day_offset))
            # CALENDAR type: skip for now (requires absolute date mapping)

        from app.services.bl03_deduplication import (
            deduplicate_advisory, TimelineWindow as TLWindow,
            PracticeStub as PStub, PracticeElement as PEl,
        )
        from app.services.bl02_conditional import (
            filter_practices_by_conditionals,
            ConditionalQuestion as CQ, PracticeConditionalLink as PCL,
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

        # ── Build CCA timeline stubs with BL-02 conditional filtering ─────────
        tl_windows: list[TLWindow] = []
        tl_date_map: dict = {}   # id → (from_date, to_date, day_num)
        pending_questions_by_tl: dict = {}   # tl.id → {question info}

        for tl, day_num in active_timelines:
            p_result = await db.execute(
                select(Practice).where(Practice.timeline_id == tl.id).order_by(Practice.display_order)
            )
            all_practices = p_result.scalars().all()
            all_practice_ids = [p.id for p in all_practices]

            # BL-02: Load conditional questions for this timeline
            cond_q_result = await db.execute(
                select(ConditionalQuestion).where(ConditionalQuestion.timeline_id == tl.id)
                .order_by(ConditionalQuestion.display_order)
            )
            cond_questions = cond_q_result.scalars().all()

            # Load practice_conditionals links
            pc_result = await db.execute(
                select(PracticeConditional).where(PracticeConditional.practice_id.in_(all_practice_ids))
            )
            pc_rows = pc_result.scalars().all()

            # Run BL-02 filter
            bl02_result = filter_practices_by_conditionals(
                all_practice_ids=all_practice_ids,
                questions=[CQ(q.id, q.question_text, q.display_order) for q in cond_questions],
                practice_links=[PCL(r.practice_id, r.question_id,
                                    r.answer.value if hasattr(r.answer, 'value') else str(r.answer))
                                for r in pc_rows],
                today_answers=today_answers,
            )

            if not bl02_result.all_questions_answered and bl02_result.pending_question:
                pending_questions_by_tl[tl.id] = {
                    "question_id": bl02_result.pending_question.id,
                    "question_text": bl02_result.pending_question.question_text,
                    "display_order": bl02_result.pending_question.display_order,
                }

            visible_ids = set(bl02_result.visible_practices)
            visible_practices = [p for p in all_practices if p.id in visible_ids]

            practice_stubs: list[PStub] = []
            for p in visible_practices:
                el_result = await db.execute(
                    select(Element).where(Element.practice_id == p.id).order_by(Element.display_order)
                )
                elements = el_result.scalars().all()
                practice_stubs.append(PStub(
                    id=p.id,
                    l0_type=p.l0_type.value if hasattr(p.l0_type, 'value') else str(p.l0_type),
                    l1_type=p.l1_type, l2_type=p.l2_type,
                    display_order=p.display_order, is_special_input=p.is_special_input,
                    relation_id=p.relation_id,
                    elements=[PEl(element_type=el.element_type, cosh_ref=el.cosh_ref,
                                  value=el.value, unit_cosh_id=el.unit_cosh_id)
                              for el in elements],
                ))

            # Calendar dates for this timeline
            if tl.from_type.value == "DAS":
                from_d = crop_start + timedelta(days=tl.from_value)
                to_d = crop_start + timedelta(days=tl.to_value)
            elif tl.from_type.value == "DBS":
                from_d = crop_start - timedelta(days=tl.from_value)
                to_d = crop_start - timedelta(days=tl.to_value)
            else:
                from_d = to_d = today

            tl_window = TLWindow(
                id=tl.id, name=tl.name, from_date=from_d, to_date=to_d,
                created_at=tl.created_at.date() if hasattr(tl.created_at, 'date') else today,
                practices=practice_stubs, source="CCA",
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
                    from_d = cha.triggered_at.date() + timedelta(days=sp_tl.from_value)
                    to_d = cha.triggered_at.date() + timedelta(days=sp_tl.to_value)
                    if not (from_d <= today <= to_d):
                        continue  # Not active today
                    sp_practices = (await db.execute(
                        select(SPPractice).where(SPPractice.timeline_id == sp_tl.id).order_by(SPPractice.display_order)
                    )).scalars().all()
                    stubs = [PStub(id=p.id,
                                  l0_type=p.l0_type if isinstance(p.l0_type, str) else str(p.l0_type),
                                  l1_type=p.l1_type, l2_type=p.l2_type,
                                  display_order=p.display_order, is_special_input=p.is_special_input,
                                  relation_id=None,
                                  elements=[PEl(element_type=el.element_type, cosh_ref=el.cosh_ref,
                                                value=el.value, unit_cosh_id=el.unit_cosh_id)
                                            for el in (await db.execute(
                                                select(SPElement).where(SPElement.practice_id == p.id)
                                            )).scalars().all()])
                             for p in sp_practices]
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
                    from_d = cha.triggered_at.date() + timedelta(days=pg_tl.from_value)
                    to_d = cha.triggered_at.date() + timedelta(days=pg_tl.to_value)
                    if not (from_d <= today <= to_d):
                        continue
                    pg_practices = (await db.execute(
                        select(PGPractice).where(PGPractice.timeline_id == pg_tl.id).order_by(PGPractice.display_order)
                    )).scalars().all()
                    stubs = [PStub(id=p.id,
                                  l0_type=p.l0_type if isinstance(p.l0_type, str) else str(p.l0_type),
                                  l1_type=p.l1_type, l2_type=p.l2_type,
                                  display_order=p.display_order, is_special_input=p.is_special_input,
                                  relation_id=None,
                                  elements=[PEl(element_type=el.element_type, cosh_ref=el.cosh_ref,
                                                value=el.value, unit_cosh_id=el.unit_cosh_id)
                                            for el in (await db.execute(
                                                select(PGElement).where(PGElement.practice_id == p.id)
                                            )).scalars().all()])
                             for p in pg_practices]
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
                        "elements": [{"element_type": el.element_type, "cosh_ref": el.cosh_ref,
                                      "value": el.value, "unit_cosh_id": el.unit_cosh_id}
                                     for el in p.elements],
                    }
                    for p in dedup_tl.visible_practices
                ],
            }
            # Include BL-02 pending question for this timeline (if any)
            if tl.id in pending_questions_by_tl:
                tl_entry["pending_conditional_question"] = pending_questions_by_tl[tl.id]
                tl_entry["has_pending_question"] = True
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
