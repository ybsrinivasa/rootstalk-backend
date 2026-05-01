import secrets
import string
from datetime import datetime, timedelta, timezone, date
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
)
from app.modules.advisory.models import Package, Parameter, Variable, PackageVariable, Timeline, Practice, Element

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
        sub.reference_number = _generate_reference()
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
    """BL-05b: Set or update crop start date."""
    sub = await _get_subscription(db, subscription_id, current_user.id)
    sub.crop_start_date = data.get("crop_start_date")
    await db.commit()
    return {"detail": "Start date updated", "crop_start_date": sub.crop_start_date}


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
            sub.reference_number = _generate_reference()
            await _consume_pool_unit(db, sub.client_id)
        # else stays WAITLISTED — company has 3 days
    else:
        sub.status = SubscriptionStatus.CANCELLED

    await db.commit()
    return {"status": sub.status, "reference_number": sub.reference_number}


# ── Payment Delegation ────────────────────────────────────────────────────────

class PaymentDelegateRequest(BaseModel):
    requested_from_user_id: str


@router.post("/farmer/subscriptions/{subscription_id}/delegate-payment")
async def delegate_payment(
    subscription_id: str,
    request: PaymentDelegateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = await _get_subscription(db, subscription_id, current_user.id)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=PAYMENT_REQUEST_EXPIRY_HOURS)
    pr = SubscriptionPaymentRequest(
        subscription_id=subscription_id,
        farmer_user_id=current_user.id,
        requested_from_user_id=request.requested_from_user_id,
        expires_at=expires_at,
    )
    db.add(pr)
    await db.commit()
    return {"detail": "Payment request sent", "expires_at": expires_at}


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
        sub.reference_number = _generate_reference()
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


def _generate_reference() -> str:
    chars = string.ascii_uppercase + string.digits
    return "RT-" + "".join(secrets.choice(chars) for _ in range(10))


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
        sub.reference_number = _generate_reference()
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
            sub.reference_number = _generate_reference()

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


# ── Farmer: Daily advisory (BL-04 timeline window filter) ─────────────────────

@router.get("/farmer/advisory/today")
async def get_today_advisory(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return today's active practices for all the farmer's ACTIVE subscriptions.
    Applies BL-04 window logic (DAS/DBS). Deduplication (BL-03) is deferred to v2.
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

        if not active_timelines:
            # Still return the subscription but with empty timelines (so start-date gate shows)
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

        timeline_data = []
        for tl, day_num in active_timelines:
            p_result = await db.execute(
                select(Practice).where(Practice.timeline_id == tl.id).order_by(Practice.display_order)
            )
            practices = p_result.scalars().all()

            practice_data = []
            for p in practices:
                el_result = await db.execute(
                    select(Element).where(Element.practice_id == p.id).order_by(Element.display_order)
                )
                elements = el_result.scalars().all()
                practice_data.append({
                    "id": p.id,
                    "l0_type": p.l0_type,
                    "l1_type": p.l1_type,
                    "l2_type": p.l2_type,
                    "display_order": p.display_order,
                    "is_special_input": p.is_special_input,
                    "elements": [
                        {
                            "element_type": el.element_type,
                            "cosh_ref": el.cosh_ref,
                            "value": el.value,
                            "unit_cosh_id": el.unit_cosh_id,
                        }
                        for el in elements
                    ],
                })

            timeline_data.append({
                "id": tl.id,
                "name": tl.name,
                "from_type": tl.from_type,
                "from_value": tl.from_value,
                "to_value": tl.to_value,
                "day_number": day_num,
                "practices": practice_data,
            })

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
