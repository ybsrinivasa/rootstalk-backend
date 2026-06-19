import re
from datetime import datetime, timedelta, timezone, date
from math import radians, cos, sin, asin, sqrt
from fastapi import APIRouter, Depends, HTTPException, Request
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
from app.modules.advisory.models import PGRecommendation, SPRecommendation, Timeline
from app.modules.platform.models import UserRole, RoleType
from app.modules.orders.models import DealerProfile, OrderItem, OrderItemStatus
from app.modules.clients.models import Client, ClientLocation, ClientStatus
from app.services.i18n_cosh import pick_translation, resolve_names_by_cosh_id
from app.services.bl11_subscription_state import (
    DEALER as BL11_DEALER, FARMER as BL11_FARMER,
    is_self_unsubscribable, validate_transition as validate_sub_transition,
)
from app.services.bl15_reference import (
    client_code_from_short_name, format_reference, parse_sequence,
    reference_prefix, two_digit_year,
)

router = APIRouter(tags=["Subscriptions"])

WAITLIST_EXPIRY_DAYS = 3
PAYMENT_REQUEST_EXPIRY_HOURS = 24   # 2026-05-29: tightened from 72h per the cancel-and-route spec


def _raise_sub_transition(res, status_code: int = 400) -> None:
    """Convert a TransitionResult.allowed=False into an HTTPException
    carrying the stable error_code in the detail payload."""
    raise HTTPException(
        status_code=status_code,
        detail={"error_code": res.error_code, "message": res.message},
    )


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


# NOTE: the free POST /client/{id}/subscription-pool/purchase endpoint
# was removed in Phase B (2026-05-04 commit d5e7b1a93f28). Pool top-ups
# now flow exclusively through Razorpay — see /payment/create-order and
# /payment/verify below. The PoolPurchase model is retained because
# legacy pytest fixtures may still import it; it is no longer used by
# any route handler.


@router.get("/client/{client_id}/subscription-pool/balance")
async def get_pool_balance(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.promoter_pool import is_enterprise_licensed
    from app.modules.subscriptions.models import EnterpriseLicense

    balance = await _get_pool_balance(db, client_id)
    el_active = await is_enterprise_licensed(db, client_id)
    enterprise_to_date = None
    if el_active:
        lic = (await db.execute(
            select(EnterpriseLicense).where(
                EnterpriseLicense.client_id == client_id,
                EnterpriseLicense.status == "ACTIVE",
            )
        )).scalar_one_or_none()
        if lic is not None:
            enterprise_to_date = lic.to_date
    return {
        "client_id": client_id,
        "available_units": balance,
        # `balance` alias kept for the historical Client Portal shape;
        # the page reads `r.data.balance`. Both keys now mean the same
        # raw on-pool number (purchased - consumed), unaffected by the
        # EL flag. The frontend should branch on `unlimited` for what
        # to display.
        "balance": balance,
        "unlimited": el_active,
        "enterprise_to_date": enterprise_to_date,
    }


@router.get("/client/{client_id}/subscription-pool/can-assign")
async def get_pool_can_assign(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Phase C — proactive guard for the promoter PWA flow.

    Returns whether the *current_user as promoter* is allowed to start
    a new assignment for this client. Gates on the promoter's personal
    allocation balance (Phase C model), not the company-wide pool. The
    PWA uses this on company-select so promoters don't waste a full
    BL-01 walk only to be rejected at the final step.
    """
    from app.services.promoter_pool import get_promoter_balance
    balance = await get_promoter_balance(db, client_id, current_user.id)
    return {
        "client_id": client_id,
        "available_units": balance,   # promoter's own balance, not company's
        "can_assign": balance > 0,
    }


@router.get("/client/{client_id}/subscription-pool/quote")
async def get_pool_quote(
    client_id: str,
    units: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Phase A.1 — preview pricing for a pool top-up.

    Returns gross / discount / total in paise (integers, never float).
    Formula: Total = [N × 199] − [0.5 × N^1.4887593].

    The Client Portal calls this on every change of the unit count to
    show a live "you save ₹X / total ₹Y" preview before the CA confirms
    the purchase. No DB writes; safe to call repeatedly.
    """
    from app.services.subscription_pricing import (
        MAX_UNITS, MIN_UNITS, PER_UNIT_GROSS_PAISE, quote_for,
    )
    try:
        q = quote_for(units)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "client_id": client_id,
        "units": q.units,
        "per_unit_gross_paise": PER_UNIT_GROSS_PAISE,
        "gross_paise": q.gross_paise,
        "discount_paise": q.discount_paise,
        "total_paise": q.total_paise,
        "per_unit_effective_paise": q.per_unit_effective_paise,
        "min_units": MIN_UNITS,
        "max_units": MAX_UNITS,
        # Convenience strings for direct display (rupees with 2 decimals).
        "gross_rupees": f"{q.gross_paise / 100:.2f}",
        "discount_rupees": f"{q.discount_paise / 100:.2f}",
        "total_rupees": f"{q.total_paise / 100:.2f}",
    }


# ── Phase C: Per-promoter allocations ──────────────────────────────────────────

class PromoterAllocateRequest(BaseModel):
    units: int


@router.get("/client/{client_id}/promoter-allocations")
async def list_promoter_allocations(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA-side — list every promoter who has an allocation row for
    this company along with their current balance and audit totals."""
    from app.modules.subscriptions.promoter_allocation_models import PromoterAllocation
    from app.modules.subscriptions.models import EnterpriseLicense
    from app.services.promoter_pool import (
        get_company_unallocated_balance, is_enterprise_licensed,
    )

    rows = (await db.execute(
        select(PromoterAllocation, User)
        .join(User, User.id == PromoterAllocation.promoter_user_id)
        .where(PromoterAllocation.client_id == client_id)
        .order_by(User.name)
    )).all()

    company_unallocated = await get_company_unallocated_balance(db, client_id)
    el_active = await is_enterprise_licensed(db, client_id)
    enterprise_to_date = None
    if el_active:
        lic = (await db.execute(
            select(EnterpriseLicense).where(
                EnterpriseLicense.client_id == client_id,
                EnterpriseLicense.status == "ACTIVE",
            )
        )).scalar_one_or_none()
        if lic is not None:
            enterprise_to_date = lic.to_date

    return {
        "client_id": client_id,
        "company_unallocated_balance": company_unallocated,
        "unlimited": el_active,
        "enterprise_to_date": enterprise_to_date,
        "promoters": [
            {
                "promoter_user_id": user.id,
                "promoter_name": user.name,
                "promoter_phone": user.phone,
                "units_balance": int(alloc.units_balance),
                "allocated_total": int(alloc.allocated_total),
                "reclaimed_total": int(alloc.reclaimed_total),
                "consumed_total": int(alloc.consumed_total),
            }
            for alloc, user in rows
        ],
    }


@router.post(
    "/client/{client_id}/promoter-allocations/{promoter_user_id}/allocate",
    status_code=201,
)
async def allocate_to_promoter_endpoint(
    client_id: str,
    promoter_user_id: str,
    request: PromoterAllocateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA action — give `units` to a specific promoter, drawn from the
    company's unallocated balance. Lazy-creates the allocation row on
    first call. Returns 422 if the company doesn't have enough
    unallocated units."""
    from app.services.promoter_pool import (
        allocate_to_promoter, get_promoter_balance,
    )
    try:
        row = await allocate_to_promoter(
            db, client_id=client_id,
            promoter_user_id=promoter_user_id,
            units=request.units,
        )
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "promoter_user_id": promoter_user_id,
        "units_balance": int(row.units_balance),
        "allocated_total": int(row.allocated_total),
    }


@router.post(
    "/client/{client_id}/promoter-allocations/{promoter_user_id}/reclaim",
    status_code=200,
)
async def reclaim_from_promoter_endpoint(
    client_id: str,
    promoter_user_id: str,
    request: PromoterAllocateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA action — pull `units` back from a promoter into the company
    unallocated balance. Cannot exceed the promoter's current balance
    (already-consumed units are not reclaimable)."""
    from app.services.promoter_pool import reclaim_from_promoter
    try:
        row = await reclaim_from_promoter(
            db, client_id=client_id,
            promoter_user_id=promoter_user_id,
            units=request.units,
        )
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "promoter_user_id": promoter_user_id,
        "units_balance": int(row.units_balance),
        "reclaimed_total": int(row.reclaimed_total),
    }


@router.get("/promoter/me/allocations")
async def my_promoter_allocations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Promoter-side — current_user sees their own allocation rows
    across all companies that have allocated to them.

    EL surfacing (2026-05-30): also includes clients where the
    current user has an ACTIVE Dealer/Facilitator binding AND the
    client has an active Enterprise Licence — even if no
    PromoterAllocation row exists. Such rows return
    `unlimited: true`, `enterprise_to_date: <date>`, and a sentinel
    units_balance (`ENTERPRISE_UNLIMITED_BALANCE`) so the existing
    Dealer picker's `units_balance > 0` filter naturally includes
    them.
    """
    from app.modules.subscriptions.promoter_allocation_models import PromoterAllocation
    from app.modules.subscriptions.models import EnterpriseLicense
    from app.modules.clients.models import Client, ClientPromoter
    from app.services.promoter_pool import ENTERPRISE_UNLIMITED_BALANCE
    from datetime import date as _date

    today = _date.today()

    alloc_rows = (await db.execute(
        select(PromoterAllocation, Client)
        .join(Client, Client.id == PromoterAllocation.client_id)
        .where(PromoterAllocation.promoter_user_id == current_user.id)
        .order_by(Client.display_name)
    )).all()

    # Also pull EL clients the user is bound to but has no allocation
    # row for. We union by client_id to avoid double-counting.
    seen_client_ids = {client.id for _, client in alloc_rows}
    el_rows = (await db.execute(
        select(EnterpriseLicense, Client)
        .join(Client, Client.id == EnterpriseLicense.client_id)
        .join(ClientPromoter, ClientPromoter.client_id == Client.id)
        .where(
            EnterpriseLicense.status == "ACTIVE",
            EnterpriseLicense.from_date <= today,
            EnterpriseLicense.to_date >= today,
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.status == "ACTIVE",
        )
        .order_by(Client.display_name)
    )).all()
    el_by_client = {client.id: lic for lic, client in el_rows}

    out = []
    for alloc, client in alloc_rows:
        lic = el_by_client.get(client.id)
        if lic is not None:
            out.append({
                "client_id": client.id,
                "client_name": client.display_name or client.full_name,
                "units_balance": ENTERPRISE_UNLIMITED_BALANCE,
                "allocated_total": int(alloc.allocated_total),
                "reclaimed_total": int(alloc.reclaimed_total),
                "consumed_total": int(alloc.consumed_total),
                "unlimited": True,
                "enterprise_to_date": lic.to_date,
            })
        else:
            out.append({
                "client_id": client.id,
                "client_name": client.display_name or client.full_name,
                "units_balance": int(alloc.units_balance),
                "allocated_total": int(alloc.allocated_total),
                "reclaimed_total": int(alloc.reclaimed_total),
                "consumed_total": int(alloc.consumed_total),
                "unlimited": False,
                "enterprise_to_date": None,
            })

    for lic, client in el_rows:
        if client.id in seen_client_ids:
            continue   # already merged above
        out.append({
            "client_id": client.id,
            "client_name": client.display_name or client.full_name,
            "units_balance": ENTERPRISE_UNLIMITED_BALANCE,
            "allocated_total": 0,
            "reclaimed_total": 0,
            "consumed_total": 0,
            "unlimited": True,
            "enterprise_to_date": lic.to_date,
        })

    out.sort(key=lambda r: (r["client_name"] or "").lower())
    return out


# ── F-P Assign-Package-to-Farmer: B1 read-side ────────────────────────────────
# 2026-05-29. Powers the PWA flow where a Facilitator-Promoter locked to one
# Client assigns a Package to a farmer from their kitty. Design lock in
# memory/project_rootstalk_fp_assign_package_design.md.

async def _resolve_promoter_locked_client(db: AsyncSession, user: User) -> Client:
    """Resolve the F-P's unique ACTIVE Facilitator-Promoter binding.

    A Facilitator with `is_promoter=True` is exclusive per spec §11.2 —
    one Client at a time. This helper enforces that invariant on every
    F-P-side read so the rest of the flow can derive `client_id`
    server-side instead of trusting the frontend.
    """
    from app.modules.clients.models import ClientPromoter, ClientStatus
    rows = (await db.execute(
        select(ClientPromoter, Client)
        .join(Client, Client.id == ClientPromoter.client_id)
        .where(
            ClientPromoter.user_id == user.id,
            ClientPromoter.promoter_type == "FACILITATOR",
            ClientPromoter.is_promoter.is_(True),
            ClientPromoter.status == "ACTIVE",
            Client.status == ClientStatus.ACTIVE,
        )
    )).all()
    if not rows:
        raise HTTPException(status_code=403, detail={
            "code": "not_a_promoter",
            "message": "You are not currently a Promoter for any company.",
        })
    if len(rows) > 1:
        raise HTTPException(status_code=500, detail={
            "code": "multiple_promoter_links",
            "message": "Data integrity: more than one ACTIVE Facilitator-Promoter link.",
        })
    return rows[0][1]


async def _resolve_promoter_at_client(
    db: AsyncSession, user: User, client_id: str,
) -> Client:
    """Dealer parity helper (2026-05-30). Verify the user has an ACTIVE
    ClientPromoter binding (any role) at the supplied client_id and
    the client itself is ACTIVE; return the Client.

    Dealer-Promoters are multi-client by design — they can carry kitties
    at several companies simultaneously, so the F-P §11.2 exclusivity
    check doesn't apply. F-Ps trying to use a client_id other than
    their locked binding naturally fail this check (no ACTIVE
    ClientPromoter at the wrong client), so the same endpoint can
    safely serve both roles when client_id is supplied.
    """
    from app.modules.clients.models import ClientPromoter, ClientStatus
    row = (await db.execute(
        select(ClientPromoter, Client)
        .join(Client, Client.id == ClientPromoter.client_id)
        .where(
            ClientPromoter.user_id == user.id,
            ClientPromoter.client_id == client_id,
            ClientPromoter.status == "ACTIVE",
            Client.status == ClientStatus.ACTIVE,
        )
    )).first()
    if row is None:
        raise HTTPException(status_code=403, detail={
            "code": "not_a_promoter_at_client",
            "message": "You don't have an active Promoter binding at this company.",
        })
    return row[1]


@router.get("/promoter/me/kitty")
async def my_kitty(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """F-P assignment-flow gate. Returns the F-P's locked Client +
    available `units_balance`. The PWA calls this on tap-into-flow and
    after every assignment; balance=0 is the signal to show the
    'no subscriptions available' empty state and block phone entry.

    EL flag (2026-05-30): when the locked Client has an active
    Enterprise Licence, `unlimited` is True and `enterprise_to_date`
    carries the closure date so the PWA can render "Unlimited ·
    closes 15 Jan" instead of a number."""
    from app.services.promoter_pool import (
        ENTERPRISE_UNLIMITED_BALANCE, get_promoter_balance, is_enterprise_licensed,
    )
    from app.modules.subscriptions.models import EnterpriseLicense
    client = await _resolve_promoter_locked_client(db, current_user)
    balance = await get_promoter_balance(db, client.id, current_user.id)
    unlimited = balance == ENTERPRISE_UNLIMITED_BALANCE and await is_enterprise_licensed(db, client.id)
    enterprise_to_date = None
    if unlimited:
        lic = (await db.execute(
            select(EnterpriseLicense).where(
                EnterpriseLicense.client_id == client.id,
                EnterpriseLicense.status == "ACTIVE",
            )
        )).scalar_one_or_none()
        if lic:
            enterprise_to_date = lic.to_date
    return {
        "client_id": client.id,
        "client_short_name": client.short_name,
        "client_display_name": client.display_name or client.full_name,
        "units_balance": balance,
        "unlimited": unlimited,
        "enterprise_to_date": enterprise_to_date,
    }


@router.get("/promoter/me/pending-assignments")
async def my_pending_assignments(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """F-P B4 (2026-05-29) — list this promoter's own PENDING sends.

    Powers the Pending-sent list in the PWA. Each row is decorated with
    farmer name + phone + crop + package name + assigned_at +
    hours_remaining_for_farmer (72h from assigned_at, per the B3
    auto-expire window). Cancel happens via
    DELETE /promoter/assignments/{id}; the PWA refreshes this list
    afterwards.

    Returns only PENDING_FARMER_APPROVAL rows for the current user —
    not gated on the F-P binding (a Dealer-Promoter or a former F-P
    can still see what they have outstanding)."""
    from app.tasks.assignment_expiry import EXPIRY_HOURS

    rows = (await db.execute(
        select(PromoterAssignment, Subscription, Package)
        .join(Subscription, Subscription.id == PromoterAssignment.subscription_id)
        .join(Package, Package.id == Subscription.package_id)
        .where(
            PromoterAssignment.promoter_user_id == current_user.id,
            PromoterAssignment.status == AssignmentStatus.PENDING_FARMER_APPROVAL,
        )
        .order_by(PromoterAssignment.assigned_at.desc())
    )).all()

    if not rows:
        return []

    farmer_ids = {sub.farmer_user_id for _, sub, _ in rows}
    farmers_by_id = {
        u.id: u for u in (await db.execute(
            select(User).where(User.id.in_(farmer_ids))
        )).scalars().all()
    }

    now = datetime.now(timezone.utc)
    out = []
    for assignment, sub, pkg in rows:
        farmer = farmers_by_id.get(sub.farmer_user_id)
        # `assigned_at` is timezone-aware on insert; defensive .replace
        # keeps the math safe if a legacy row lacks tzinfo.
        aa = assignment.assigned_at
        if aa.tzinfo is None:
            aa = aa.replace(tzinfo=timezone.utc)
        hours_elapsed = (now - aa).total_seconds() / 3600.0
        hours_remaining = max(0.0, EXPIRY_HOURS - hours_elapsed)
        out.append({
            "assignment_id": assignment.id,
            "subscription_id": sub.id,
            "client_id": sub.client_id,
            "package_id": pkg.id,
            "package_name": pkg.name,
            "crop_cosh_id": pkg.crop_cosh_id,
            "farmer_user_id": sub.farmer_user_id,
            "farmer_name": farmer.name if farmer else None,
            "farmer_phone": farmer.phone if farmer else None,
            "assigned_at": assignment.assigned_at,
            "hours_remaining": round(hours_remaining, 1),
        })
    return out


@router.get("/promoter/farmers/{phone}/locations")
async def promoter_farmer_locations(
    phone: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List a farmer's locations for an F-P about to assign a package.

    V1: each User has one primary location (state + district +
    sub_district on the User row); returned as a one-element list so
    the PWA can treat the response uniformly when real multi-location
    support lands. Refuses if the farmer hasn't registered."""
    await _resolve_promoter_locked_client(db, current_user)  # gate
    from app.modules.auth.service import get_user_by_phone
    farmer = await get_user_by_phone(db, phone)
    if not farmer:
        raise HTTPException(
            status_code=404,
            detail="Farmer not registered. Ask them to install the RootsTalk app first.",
        )
    return [{
        "label": "primary",
        "state_cosh_id": farmer.state_cosh_id,
        "district_cosh_id": farmer.district_cosh_id,
        "sub_district_cosh_id": farmer.sub_district_cosh_id,
    }]


@router.get("/promoter/crops")
async def promoter_crops(
    district_cosh_id: str,
    client_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crops served by the promoter's Client in the given District.

    F-P (no `client_id`): server-derived from the locked binding.
    Dealer (with `client_id`): caller chooses among the companies they
    have an ACTIVE ClientPromoter binding at. Either way, no
    `payment_model` filter — promoter assignment works for both
    FARMER_PAYS and COMPANY_PAYS clients (only farmer-side discovery
    hides COMPANY_PAYS).
    """
    from app.modules.advisory.models import PackageLocation, PackageStatus
    from app.modules.sync.models import CoshCoreItem
    from app.services.cosh_crop_view import get_measure_for_biological_name
    if client_id is None:
        client = await _resolve_promoter_locked_client(db, current_user)
    else:
        client = await _resolve_promoter_at_client(db, current_user, client_id)
    result = await db.execute(
        select(Package.crop_cosh_id)
        .join(PackageLocation, PackageLocation.package_id == Package.id)
        .where(
            Package.client_id == client.id,
            Package.status == PackageStatus.ACTIVE,
            PackageLocation.district_cosh_id == district_cosh_id,
        )
        .distinct()
    )
    crop_ids = list(result.scalars().all())
    if not crop_ids:
        return []
    lang = current_user.language_code or "en"
    name_rows = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
        .where(CoshCoreItem.cosh_id.in_(crop_ids))
    )).all()
    name_by_id: dict[str, str | None] = {}
    for cosh_id, translations in name_rows:
        if isinstance(translations, dict):
            name_by_id[cosh_id] = pick_translation(translations, lang, "")
        else:
            name_by_id[cosh_id] = None
    # 2026-05-30 — surface the per-crop AREA_WISE / PLANT_WISE measure
    # so the Promoter PWA can render the right input (acres vs plants
    # + planting year) automatically instead of asking the Promoter
    # to choose. The measure is intrinsic to the crop in Cosh.
    measure_by_id: dict[str, str | None] = {}
    for cid in crop_ids:
        measure_by_id[cid] = await get_measure_for_biological_name(db, cid)
    return [
        {
            "crop_cosh_id": c,
            "name": name_by_id.get(c),
            "measure": measure_by_id.get(c),
        }
        for c in crop_ids
    ]


@router.get("/promoter/packages/guided-step")
async def promoter_guided_step(
    crop_cosh_id: str,
    district_cosh_id: str,
    answers: str = "",
    client_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Promoter-side P-V resolver. Delegates to the same BL-01
    elimination engine as `/farmer/packages/guided-step`. F-P (no
    `client_id`): derived from the locked binding. Dealer (with
    `client_id`): caller supplies and the ACTIVE-binding gate
    enforces they're a promoter at that company."""
    if client_id is None:
        client = await _resolve_promoter_locked_client(db, current_user)
    else:
        client = await _resolve_promoter_at_client(db, current_user, client_id)
    return await guided_elimination_step(
        crop_cosh_id=crop_cosh_id,
        district_cosh_id=district_cosh_id,
        client_id=client.id,
        answers=answers,
        db=db,
        current_user=current_user,
    )


# ── Phase B: Razorpay-backed pool top-up ───────────────────────────────────────

class PoolPaymentCreateOrder(BaseModel):
    units: int


class PoolPaymentVerify(BaseModel):
    units: int
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.post("/client/{client_id}/subscription-pool/payment/create-order")
async def create_pool_payment_order(
    client_id: str,
    request: PoolPaymentCreateOrder,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Phase B.2 — start a Razorpay checkout for a pool top-up.

    Server computes the quote (never trust client-side amount), creates
    a Razorpay order at that amount, returns the bits the Razorpay JS
    SDK needs. NO SubscriptionPool row is written here — that happens
    only after `/payment/verify` confirms the signature.
    """
    from app.services.payment_service import create_pool_topup_order
    from app.services.subscription_pricing import quote_for
    try:
        q = quote_for(request.units)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Receipt: short, unique-ish, traceable. Razorpay caps at 40 chars.
    receipt = f"pool-{client_id[:8]}-{request.units}"[:40]
    return create_pool_topup_order(
        receipt=receipt,
        amount_paise=q.total_paise,
        units=q.units,
        client_id=client_id,
    )


@router.post("/client/{client_id}/subscription-pool/payment/verify")
async def verify_pool_payment(
    client_id: str,
    request: PoolPaymentVerify,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Phase B.2 — verify Razorpay signature and add units to the pool.

    Defence-in-depth checks:
      1. Razorpay signature matches the secret-keyed HMAC.
      2. Server re-computes the quote for `units` and asserts it equals
         the original Razorpay order amount (rejects tampering between
         create-order and verify).
      3. Idempotency: refuse to write a second pool row for the same
         razorpay_order_id (partial unique index also enforces this at
         the DB level).
    """
    from app.services.payment_service import (
        fetch_order_amount_paise, verify_payment_signature,
    )
    from app.services.subscription_pricing import quote_for

    if not verify_payment_signature(
        request.razorpay_order_id,
        request.razorpay_payment_id,
        request.razorpay_signature,
    ):
        raise HTTPException(
            status_code=400,
            detail="Payment verification failed — invalid signature",
        )

    try:
        q = quote_for(request.units)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Cross-check against the actual Razorpay order amount.
    razorpay_amount = fetch_order_amount_paise(request.razorpay_order_id)
    if razorpay_amount != q.total_paise:
        raise HTTPException(
            status_code=400,
            detail=(
                "Amount mismatch — Razorpay order amount does not match "
                "the recomputed quote. Refusing to credit pool."
            ),
        )

    # Idempotency — a second verify call for the same Razorpay order
    # (e.g. a duplicate webhook) must not double-credit the pool.
    existing = (await db.execute(
        select(SubscriptionPool).where(
            SubscriptionPool.razorpay_order_id == request.razorpay_order_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        balance = await _get_pool_balance(db, client_id)
        return {
            "detail": "Order already credited.",
            "balance": balance,
            "units_added": existing.units_purchased,
        }

    pool = SubscriptionPool(
        client_id=client_id,
        units_purchased=q.units,
        units_consumed=0,
        razorpay_order_id=request.razorpay_order_id,
        razorpay_payment_id=request.razorpay_payment_id,
        amount_paid_paise=q.total_paise,
        purchased_by_user_id=current_user.id,
    )
    db.add(pool)
    await db.commit()

    balance = await _get_pool_balance(db, client_id)
    return {
        "detail": f"{q.units} units added to pool.",
        "balance": balance,
        "units_added": q.units,
        "amount_paid_paise": q.total_paise,
    }


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

    Loads the candidate pool + parameter/variable metadata, then delegates
    to the pure-function service `run_elimination` (single source of truth
    for the algorithm). Returns the next parameter question with only valid
    variables, the final package if one remains, or a config-error marker.

    Reset semantics: pass `answers=''` (or omit) to start over from
    Parameter 1 — the algorithm naturally returns the first parameter
    question on an empty answer set. The PWA "decline confirmation"
    flow uses this.
    """
    from app.modules.advisory.models import PackageLocation, PackageStatus
    from app.services.bl01_guided_elimination import (
        run_elimination,
        PackageStub as ServicePackageStub,
        ParameterOption as ServiceParameterOption,
    )

    # ── Parse caller-supplied previous answers ───────────────────────────
    parsed_answers: dict[str, str] = {}
    if answers:
        for pair in answers.split(","):
            if ":" in pair:
                param_id, var_id = pair.split(":", 1)
                parsed_answers[param_id] = var_id

    # ── Load the candidate pool ───────────────────────────────────────────
    pkg_rows = (await db.execute(
        select(Package)
        .join(PackageLocation, PackageLocation.package_id == Package.id)
        .where(
            Package.client_id == client_id,
            Package.crop_cosh_id == crop_cosh_id,
            Package.status == PackageStatus.ACTIVE,
            PackageLocation.district_cosh_id == district_cosh_id,
        )
    )).scalars().all()

    # Note: do NOT early-return on empty pkg_rows — let the algorithm
    # produce the DATA_CONFIG_ERROR so the audit-row write at the bottom
    # runs uniformly for every error path (no candidates, or candidates
    # but ambiguous fingerprints).

    pkg_ids = [p.id for p in pkg_rows]

    # ── Load the variable map for every candidate package ────────────────
    pvs = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id.in_(pkg_ids))
    )).scalars().all()
    variable_map_by_pkg: dict[str, dict[str, str]] = {pid: {} for pid in pkg_ids}
    for pv in pvs:
        variable_map_by_pkg[pv.package_id][pv.parameter_id] = pv.variable_id

    pool: list[ServicePackageStub] = [
        ServicePackageStub(
            id=p.id, name=p.name, description=p.description,
            variable_map=variable_map_by_pkg.get(p.id, {}),
        )
        for p in pkg_rows
    ]

    # ── Load the parameters in display_order ─────────────────────────────
    param_ids_in_pool = {pid for vm in variable_map_by_pkg.values() for pid in vm}
    param_rows = (await db.execute(
        select(Parameter)
        .where(Parameter.id.in_(param_ids_in_pool))
        .order_by(Parameter.display_order.asc())
    )).scalars().all() if param_ids_in_pool else []

    # ── Load Kannada / Hindi / etc. parameter+variable names via Cosh ────
    # Parameter and Variable rows mirror Cosh entities; their Kannada
    # names live on `cosh_core_items.translations` keyed by `cosh_id`.
    # Custom (non-Cosh) rows fall through to the local `.name` column.
    from app.modules.sync.models import CoshCoreItem
    from app.services.i18n_cosh import pick_translation
    lang = current_user.language_code or "en"
    pv_cosh_ids = {pr.cosh_id for pr in param_rows if pr.cosh_id}
    cosh_translations: dict[str, dict] = {}
    if pv_cosh_ids:
        rows = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(pv_cosh_ids))
        )).all()
        for cid, tr in rows:
            cosh_translations[cid] = tr or {}

    def _localised_name(local_name: str, cosh_id: str | None) -> str:
        if cosh_id and cosh_id in cosh_translations:
            return pick_translation(cosh_translations[cosh_id], lang, local_name)
        return local_name

    parameters: list[ServiceParameterOption] = [
        ServiceParameterOption(
            id=pr.id, name=_localised_name(pr.name, pr.cosh_id),
            display_order=int(pr.display_order or 0),
        )
        for pr in param_rows
    ]

    # ── Load variable display names for the variables actually used ──────
    var_ids_in_pool = {
        vid for vm in variable_map_by_pkg.values() for vid in vm.values()
    }
    var_rows = (await db.execute(
        select(Variable).where(Variable.id.in_(var_ids_in_pool))
    )).scalars().all() if var_ids_in_pool else []
    var_cosh_ids = {v.cosh_id for v in var_rows if v.cosh_id}
    if var_cosh_ids:
        rows = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(var_cosh_ids))
        )).all()
        for cid, tr in rows:
            cosh_translations[cid] = tr or {}
    variable_names: dict[str, str] = {
        v.id: _localised_name(v.name, v.cosh_id) for v in var_rows
    }

    # ── Run the algorithm ────────────────────────────────────────────────
    step = run_elimination(pool, parameters, parsed_answers, variable_names)

    # ── Translate EliminationStep → JSON response ────────────────────────
    if step.error:
        # Spec: pool=0 (or pool>1 with no remaining parameters) is a
        # Content-Manager-alertable configuration error. We log AND
        # persist to data_config_errors so SA can review via
        # GET /admin/bl01/config-errors.
        import logging as _logging
        from app.modules.subscriptions.config_error_models import DataConfigError
        _logging.getLogger(__name__).error(
            "BL-01 DATA_CONFIG_ERROR client=%s crop=%s district=%s answers=%s",
            client_id, crop_cosh_id, district_cosh_id, answers,
        )
        try:
            db.add(DataConfigError(
                algorithm="BL-01",
                client_id=client_id,
                crop_cosh_id=crop_cosh_id,
                district_cosh_id=district_cosh_id,
                answers_state=answers or None,
                observed_by_user_id=current_user.id,
                details=step.error,
            ))
            await db.commit()
        except Exception as _audit_exc:  # noqa: BLE001 — never let logging fail the request
            _logging.getLogger(__name__).warning(
                "BL-01 audit row insert failed: %r", _audit_exc,
            )
            await db.rollback()
        return {"done": False, "error": step.error}

    if step.done and step.package is not None:
        return {
            "done": True,
            "package": {
                "id": step.package.id,
                "name": step.package.name,
                "description": step.package.description,
            },
            "summary": step.summary,        # plain-language variable names
            "auto_selected": step.auto_selected,
        }

    # Question step
    return {
        "done": False,
        "parameter": {"id": step.parameter.id, "name": step.parameter.name},
        "variables": [{"id": v.id, "name": v.name} for v in step.variables],
        "remaining_count": step.remaining_count,
        "auto_selected": step.auto_selected,
    }


# ── Discovery Endpoints ────────────────────────────────────────────────────────

@router.get("/farmer/discover/crops")
async def discover_crops(
    district_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All crops that have at least one ACTIVE package in the given
    district, scoped to FARMER_PAYS clients only. Returns the crop's
    English display name alongside the cosh_id so the PWA can render
    "Paddy" instead of a UUID (Cosh crop ids are real UUIDs, not the
    older `crop_paddy` slug form, so a frontend de-slug helper isn't
    enough).

    COMPANY_PAYS clients are excluded from direct-subscription
    discovery — their crops never appear here even if a package
    matches the district. They onboard farmers via promoters."""
    from app.modules.advisory.models import PackageLocation, PackageStatus
    from app.modules.clients.models import Client, PaymentModel
    from app.modules.sync.models import CoshCoreItem

    result = await db.execute(
        select(Package.crop_cosh_id)
        .join(PackageLocation, PackageLocation.package_id == Package.id)
        .join(Client, Client.id == Package.client_id)
        .where(
            Package.client_id != None,  # noqa
            Package.status == PackageStatus.ACTIVE,
            PackageLocation.district_cosh_id == district_cosh_id,
            Client.payment_model == PaymentModel.FARMER_PAYS,
        )
        .distinct()
    )
    crop_ids = list(result.scalars().all())
    if not crop_ids:
        return []
    # Resolve names from cosh_core_items (Cosh stores crop labels
    # under the `biological_names` core_type). Translations is a
    # JSON map keyed by language code; prefer the farmer's language,
    # fall back to English.
    lang = current_user.language_code or "en"
    name_rows = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
        .where(CoshCoreItem.cosh_id.in_(crop_ids))
    )).all()
    name_by_id: dict[str, str | None] = {}
    for cosh_id, translations in name_rows:
        if isinstance(translations, dict):
            name_by_id[cosh_id] = pick_translation(translations, lang, "")
        else:
            name_by_id[cosh_id] = None
    return [{"crop_cosh_id": c, "name": name_by_id.get(c)} for c in crop_ids]


@router.get("/farmer/discover/crops-and-companies")
async def discover_crops_and_companies(
    district_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Discovery view for the Crops & Companies page. One call returns:

      - every crop with ≥1 ACTIVE Package whose PackageLocation
        covers this district (with the client_ids that publish it)
      - every company with ≥1 such Package (with the crop_cosh_ids
        they cover)

    The mappings let the PWA cross-filter locally on tap — no extra
    round-trip per filter change. Also returns the district's
    friendly name so the page can show "Crops & Companies in
    Bengaluru Urban" without a second lookup.
    """
    from app.modules.advisory.models import PackageLocation, PackageStatus
    from app.modules.clients.models import Client, ClientStatus, PaymentModel
    from app.modules.sync.models import CoshCoreItem

    # One query gets every (crop, client) pair active in the district.
    # COMPANY_PAYS clients are excluded — they don't accept direct
    # farmer subscriptions, so neither their crops nor their tile
    # should ever appear on this discovery surface.
    pkg_rows = (await db.execute(
        select(Package.crop_cosh_id, Package.client_id)
        .join(PackageLocation, PackageLocation.package_id == Package.id)
        .join(Client, Client.id == Package.client_id)
        .where(
            Package.status == PackageStatus.ACTIVE,
            PackageLocation.district_cosh_id == district_cosh_id,
            Client.status == ClientStatus.ACTIVE,
            Client.payment_model == PaymentModel.FARMER_PAYS,
        )
        .distinct()
    )).all()

    crop_to_clients: dict[str, set[str]] = {}
    client_to_crops: dict[str, set[str]] = {}
    for crop_id, client_id in pkg_rows:
        if not crop_id or not client_id:
            continue
        crop_to_clients.setdefault(crop_id, set()).add(client_id)
        client_to_crops.setdefault(client_id, set()).add(crop_id)

    # Resolve crop names from Cosh, preferring the farmer's language.
    lang = current_user.language_code or "en"
    crop_ids = list(crop_to_clients.keys())
    crop_name_by_id: dict[str, str] = {}
    if crop_ids:
        name_rows = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(crop_ids))
        )).all()
        for cosh_id, translations in name_rows:
            if isinstance(translations, dict):
                crop_name_by_id[cosh_id] = pick_translation(translations, lang, cosh_id)

    # Resolve client details.
    client_ids = list(client_to_crops.keys())
    clients_by_id: dict[str, Client] = {}
    if client_ids:
        for c in (await db.execute(
            select(Client).where(Client.id.in_(client_ids))
        )).scalars().all():
            clients_by_id[c.id] = c

    # District display name — single core lookup, same locale preference.
    district_name: str | None = None
    dist_core = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id == district_cosh_id,
            CoshCoreItem.core_type == "district_list",
        )
    )).scalar_one_or_none()
    if dist_core and isinstance(dist_core.translations, dict):
        district_name = pick_translation(dist_core.translations, lang, "") or None

    crops = sorted(
        [
            {
                "crop_cosh_id": cid,
                "name": crop_name_by_id.get(cid) or cid,
                "client_ids": sorted(crop_to_clients[cid]),
            }
            for cid in crop_ids
        ],
        key=lambda x: x["name"].casefold(),
    )
    companies = sorted(
        [
            {
                "id": c.id,
                "display_name": c.display_name,
                "tagline": c.tagline,
                "logo_url": c.logo_url,
                "primary_colour": c.primary_colour,
                "crop_cosh_ids": sorted(client_to_crops.get(c.id, set())),
            }
            for c in clients_by_id.values()
        ],
        key=lambda x: (x["display_name"] or "").casefold(),
    )

    return {
        "district_cosh_id": district_cosh_id,
        "district_name": district_name,
        "crops": crops,
        "companies": companies,
    }


@router.get("/farmer/discover/companies")
async def discover_companies(
    crop_cosh_id: str,
    district_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All companies (clients) with at least one ACTIVE package for
    this crop+district, scoped to FARMER_PAYS clients only.

    COMPANY_PAYS clients are intentionally invisible to direct
    subscription flows — farmers reach those companies only through
    a promoter (dealer/facilitator) onboarding."""
    from app.modules.advisory.models import PackageLocation, PackageStatus
    from app.modules.clients.models import Client, ClientStatus, PaymentModel
    result = await db.execute(
        select(Package.client_id)
        .join(PackageLocation, PackageLocation.package_id == Package.id)
        .join(Client, Client.id == Package.client_id)
        .where(
            Package.client_id != None,  # noqa
            Package.crop_cosh_id == crop_cosh_id,
            Package.status == PackageStatus.ACTIVE,
            PackageLocation.district_cosh_id == district_cosh_id,
            Client.payment_model == PaymentModel.FARMER_PAYS,
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
    """Create a self-subscribed Subscription in WAITLISTED state.

    Phase C clarification (2026-05-04): self-subscribe is entirely
    independent of the company subscription pool. The farmer pays
    ₹199 directly to RootsTalk; nothing on any company's pool is
    touched here. Status moves to ACTIVE only after Razorpay
    `/payment/verify` confirms payment. The 3-day SubscriptionWaitlist
    expiry row that used to gate "company tops up" is no longer
    written — there's nothing to wait for.

    Per spec §11.1, only clients with payment_model=FARMER_PAYS allow
    farmer self-subscription. Clients in COMPANY_PAYS mode reject this
    endpoint with 422 — farmers must instead be assigned via Promoter.
    """
    from app.modules.clients.models import PaymentModel as _PaymentModel

    client = (await db.execute(
        select(Client).where(Client.id == request.client_id)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if client.payment_model == _PaymentModel.COMPANY_PAYS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "self_subscribe_not_allowed",
                "message": (
                    "This company is configured for Company Pays — only "
                    "company-designated promoters can assign packages to "
                    "farmers. Self-subscription is not available."
                ),
                "client_payment_model": client.payment_model.value,
            },
        )

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

    await db.commit()
    await db.refresh(sub)
    return {
        "id": sub.id,
        "status": sub.status,
        "reference_number": sub.reference_number,
        "message": "Subscription created — please complete payment to activate.",
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

    # Parse new_start_raw → datetime ONCE so we can write a real datetime
    # to the DB (asyncpg rejects bare strings on TIMESTAMP columns).
    from datetime import datetime
    if isinstance(new_start_raw, str):
        new_start_dt = datetime.fromisoformat(new_start_raw.replace("Z", "+00:00"))
    elif isinstance(new_start_raw, datetime):
        new_start_dt = new_start_raw
    else:
        raise HTTPException(
            status_code=422,
            detail="crop_start_date must be an ISO datetime string",
        )

    # First ever start date — just set it and stamp first_set_at
    # so the 15-day edit window starts ticking from now.
    if not sub.crop_start_date:
        from datetime import datetime as _dt, timezone as _tz
        sub.crop_start_date = new_start_dt
        sub.crop_start_date_first_set_at = _dt.now(_tz.utc)
        # Flip every SENT START_DATE alert for this sub to READ —
        # the farmer has done what those alerts were asking for, so
        # they should vanish from any recipient's incoming list
        # (Promoter / Facilitator / farmer-added LOCAL_PERSON).
        # Without this, the alerts stay SENT forever and the
        # Promoter keeps seeing them on /promoter/me/incoming-alerts.
        from app.modules.subscriptions.models import (
            Alert, AlertStatus, AlertType,
        )
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(Alert)
            .where(
                Alert.subscription_id == sub.id,
                Alert.alert_type == AlertType.START_DATE,
                Alert.status == AlertStatus.SENT,
            )
            .values(status=AlertStatus.READ)
        )
        await db.commit()
        return {"detail": "Start date set", "crop_start_date": sub.crop_start_date}

    # 15-day edit window. Farmer set this on day 0; can change up to
    # and including day 15; locks on day 16. Legacy rows with NULL
    # first_set_at are grandfathered as editable (no retro-locking).
    if sub.crop_start_date_first_set_at is not None:
        from datetime import timezone as _tz
        first_set_d = sub.crop_start_date_first_set_at.astimezone(_tz.utc).date()
        days_since_first_set = (dt_date.today() - first_set_d).days
        if days_since_first_set > 15:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Crop start date is locked. You can only change it "
                    "within 15 days of first setting it."
                ),
            )

    # Parse old and new dates for date-shift math
    old_start = sub.crop_start_date.date() if hasattr(sub.crop_start_date, 'date') else sub.crop_start_date
    new_start = new_start_dt.date()

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
            # BL-17: DBS to=0 closes day BEFORE sowing — clamp upper bound.
            from_d = old_start - timedelta(days=tl.from_value)
            to_d = old_start - timedelta(days=max(tl.to_value, 1))
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
    # Batch 39O (2026-05-16) unified the timelines table — every Timeline
    # row has a globally-unique id regardless of which parent FK is set.
    # Earlier code used synthetic prefixes like f"sp_{id}" / f"pg_{id}" /
    # f"qa_{id}" here, which made `detect_lock` unable to match
    # OrderItem.timeline_id (which stores the raw id). With the unified
    # table, the prefix is both unnecessary and harmful — use the raw id.
    for cha in cha_entries:
        triggered_d = cha.triggered_at.date() if hasattr(cha.triggered_at, 'date') else cha.triggered_at
        if cha.recommendation_type == "SP":
            sp_timelines = (await db.execute(
                select(Timeline).where(Timeline.sp_recommendation_id == cha.recommendation_id)
            )).scalars().all()
            for sp_tl in sp_timelines:
                from_d = triggered_d + timedelta(days=sp_tl.from_value)
                to_d = triggered_d + timedelta(days=sp_tl.to_value)
                tl_ranges.append(TimelineDateRange(
                    id=sp_tl.id, from_date=from_d, to_date=to_d, is_cha=True,
                ))
        elif cha.recommendation_type == "PG":
            pg_timelines = (await db.execute(
                select(Timeline).where(Timeline.pg_recommendation_id == cha.recommendation_id)
            )).scalars().all()
            for pg_tl in pg_timelines:
                from_d = triggered_d + timedelta(days=pg_tl.from_value)
                to_d = triggered_d + timedelta(days=pg_tl.to_value)
                tl_ranges.append(TimelineDateRange(
                    id=pg_tl.id, from_date=from_d, to_date=to_d, is_cha=True,
                ))
        elif cha.recommendation_type == "QA":
            # UCAT pipe-3: Q&A timelines live in pg_timelines too,
            # discriminated by standard_response_id. Anchor and lock
            # behaviour mirror PG/SP — they're CHA-flavoured for
            # date-shift purposes (don't move on crop_start change).
            qa_timelines = (await db.execute(
                select(Timeline).where(Timeline.standard_response_id == cha.recommendation_id)
            )).scalars().all()
            for qa_tl in qa_timelines:
                from_d = triggered_d + timedelta(days=qa_tl.from_value)
                to_d = triggered_d + timedelta(days=qa_tl.to_value)
                tl_ranges.append(TimelineDateRange(
                    id=qa_tl.id, from_date=from_d, to_date=to_d, is_cha=True,
                ))

    # Compute shifts
    shifts, delta_days = compute_date_shifts(tl_ranges, old_start, new_start, today, active_items)

    # Update start date (use the parsed datetime, not the raw string)
    sub.crop_start_date = new_start_dt

    # Shift active orders by delta (BL-05b step 6: order.date_from /
    # date_to shift universally so they remain consistent with the new
    # timeline windows).
    for order in orders:
        if hasattr(order.date_from, 'date'):
            order.date_from = order.date_from + timedelta(days=delta_days)
            order.date_to = order.date_to + timedelta(days=delta_days)

    # DBS V1 sync-close: when the new start_date is today or in the
    # past, BL-04a step 5 says all DBS practices are removed at
    # midnight UTC of crop_start_date. The hourly timeline-archive
    # sweep catches this naturally, but the farmer expects the order
    # to "end immediately" on a start-date advance — so we mirror
    # the archive synchronously here. See memory
    # `project_rootstalk_dbs_v1.md`.
    if new_start <= today:
        from app.services.order_events import record_event as _record_event
        dbs_tl_ids = {
            tl.id for tl in timelines
            if (tl.from_type.value if hasattr(tl.from_type, 'value') else str(tl.from_type)) == "DBS"
        }
        if dbs_tl_ids and orders:
            close_statuses = [
                OrderItemStatus.PENDING,
                OrderItemStatus.POSTPONED,
                OrderItemStatus.NOT_AVAILABLE,
            ]
            dbs_items = (await db.execute(
                select(OrderItem).where(
                    OrderItem.order_id.in_([o.id for o in orders]),
                    OrderItem.timeline_id.in_(dbs_tl_ids),
                    OrderItem.archived_at.is_(None),
                    OrderItem.status.in_(close_statuses),
                )
            )).scalars().all()
            from datetime import datetime as _dt2, timezone as _tz2
            now_ts = _dt2.now(_tz2.utc)
            for item in dbs_items:
                item.archived_at = now_ts
                prev = item.status.value if hasattr(item.status, "value") else item.status
                await _record_event(
                    db,
                    lineage_id=item.lineage_id,
                    event_type="TIMELINE_EXPIRED",
                    actor_role="SYSTEM",
                    order_id=item.order_id,
                    order_item_id=item.id,
                    prev_status=prev,
                    new_status=None,
                    metadata={"reason": "dbs_start_date_advanced"},
                )

    # BL-05b step 7: for dealer-postponed items whose timeline shifted,
    # the dealer's `postponed_until` must also shift by `delta_days`.
    # CHA / SP / PG / QA timelines are anchored to triggered_at and do
    # NOT shift on crop_start_date change — so only CCA timelines on
    # this package qualify. OrderItem.timeline_id points at the
    # package's `timelines` table directly; that's the right filter.
    if delta_days != 0:
        package_tl_ids = {tl.id for tl in timelines}
        if package_tl_ids:
            postponed_items = (await db.execute(
                select(OrderItem).where(
                    OrderItem.order_id.in_([o.id for o in orders]),
                    OrderItem.status == OrderItemStatus.POSTPONED,
                    OrderItem.postponed_until.isnot(None),
                    OrderItem.timeline_id.in_(package_tl_ids),
                )
            )).scalars().all()
            for item in postponed_items:
                item.postponed_until = item.postponed_until + timedelta(days=delta_days)

    # Defensive: also clear any lingering SENT START_DATE alerts on
    # the update path. Should be a no-op (first-set already flipped
    # them) but covers test scenarios + races where an old SENT row
    # exists from before the flip-on-set was added (2026-05-31).
    from app.modules.subscriptions.models import (
        Alert, AlertStatus, AlertType,
    )
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(Alert)
        .where(
            Alert.subscription_id == sub.id,
            Alert.alert_type == AlertType.START_DATE,
            Alert.status == AlertStatus.SENT,
        )
        .values(status=AlertStatus.READ)
    )

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
    promoter_type: str = "DEALER"
    # `client_id` is required for Dealer-Promoters (multi-client), and
    # optional-or-absent for Facilitator-Promoters where the server
    # derives it from the F-P's locked binding (spec §11.2). When sent
    # by an F-P, the value must match the locked binding — mismatch is
    # a 403, not silent overwrite.
    client_id: Optional[str] = None
    # F-P B2 (2026-05-29) — P-V answers captured on the F-P side and
    # persisted on the new Subscription so the volume calc has the
    # measure context immediately, not on a later farmer touch. Exactly
    # one branch must be supplied:
    #   AREA_WISE  → farm_area_acres
    #   PLANT_WISE → number_of_plants + planting_year
    farm_area_acres: Optional[float] = None
    number_of_plants: Optional[int] = None
    planting_year: Optional[int] = None


@router.post("/promoter/assignments/initiate", status_code=201)
async def initiate_assignment(
    request: PromoterAssignRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Promoter assigns advisory to farmer. Farmer must approve.

    Policy (Phase C, 2026-05-04): the gate is the **promoter's personal
    allocation balance** for this company, not the company-wide pool.
    A promoter who has exhausted their share is blocked from assigning
    further until the CA reallocates. The unit is consumed atomically
    at initiate time. Farmer self-subscribe is unchanged and remains
    independent of the company pool.

    F-P B2 (2026-05-29):
      • For `promoter_type=FACILITATOR`, `client_id` is derived
        server-side from the F-P's locked binding (spec §11.2 — one
        Client at a time). If the request also carries client_id, it
        must match — mismatch is a 403 to prevent silent overwrite.
      • P-V answers are persisted on the new Subscription: exactly one
        of {farm_area_acres} or {number_of_plants + planting_year}
        must be supplied. The corresponding *_confirmed_at column is
        stamped now since the F-P explicitly answered.
      • Rejection no longer leaves the unit consumed — the
        farmer-respond path calls `refund_to_promoter` (see B2 wiring).
    """
    from app.modules.auth.service import get_user_by_phone
    from app.services.promoter_pool import (
        consume_for_assignment, get_promoter_balance,
    )

    # ── Resolve client_id (server-derived for F-P, request-supplied
    #    for Dealer-Promoters). ─────────────────────────────────────
    if request.promoter_type == "FACILITATOR":
        locked = await _resolve_promoter_locked_client(db, current_user)
        if request.client_id and request.client_id != locked.id:
            raise HTTPException(status_code=403, detail={
                "code": "client_id_mismatch",
                "message": (
                    "Facilitator-Promoter cannot assign for a Client other "
                    "than their locked binding."
                ),
            })
        effective_client_id = locked.id
    else:
        if not request.client_id:
            raise HTTPException(status_code=422, detail={
                "code": "client_id_required",
                "message": "client_id is required for Dealer-Promoter assignments.",
            })
        effective_client_id = request.client_id

    # ── Validate optional measure inputs. ─────────────────────────
    # User direction 2026-05-30: the Promoter no longer enters the
    # farmer's farm area / plant count at assign time. Both are the
    # farmer's data — they set + confirm them on the Crop Dashboard
    # post-accept via `/farmer/subscriptions/{sid}/farm-area` (or
    # `/plant-count`). The PWA Promoter flow drops the measure stage
    # entirely. The backend still ACCEPTS the fields if supplied
    # (legacy / direct API callers) but no longer requires them.
    # XOR + partial-plant-wise validation stays — if someone DOES
    # supply, they have to supply consistently.
    area_given = request.farm_area_acres is not None
    plant_n_given = request.number_of_plants is not None
    plant_y_given = request.planting_year is not None
    plant_given = plant_n_given or plant_y_given
    if area_given and plant_given:
        raise HTTPException(status_code=422, detail={
            "code": "measure_conflict",
            "message": (
                "Supply at most one of {farm_area_acres} OR "
                "{number_of_plants + planting_year}, not both."
            ),
        })
    if plant_given and not (plant_n_given and plant_y_given):
        raise HTTPException(status_code=422, detail={
            "code": "plant_wise_incomplete",
            "message": "Plant-wise assignments need both number_of_plants and planting_year.",
        })

    farmer = await get_user_by_phone(db, request.farmer_phone)
    if not farmer:
        raise HTTPException(status_code=404, detail="Farmer not found. They must be registered in the PWA first.")

    promoter_balance = await get_promoter_balance(
        db, effective_client_id, current_user.id,
    )
    if promoter_balance <= 0:
        raise HTTPException(
            status_code=422,
            detail=(
                "You have no subscriptions allocated for this company. "
                "Ask the company admin to allocate units to you before assigning advisories to farmers."
            ),
        )

    # Atomically consume one unit from the promoter's allocation. The
    # SELECT FOR UPDATE inside `consume_for_assignment` serialises
    # concurrent initiate calls against the same promoter so we never
    # over-spend their balance.
    try:
        await consume_for_assignment(
            db, client_id=effective_client_id, promoter_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    now = datetime.now(timezone.utc)
    # 2026-05-30 — Promoter-assigned subs go straight to ACTIVE on
    # initiate. The kitty check above + `consume_for_assignment`
    # mean the unit is already paid for, so a separate WAITLISTED
    # state was misleading (it conflated "awaiting payment" with
    # "awaiting farmer's nod"). Per user direction: drop WAITLISTED
    # from the Promoter path; the "awaiting farmer approval" state
    # lives only on the PromoterAssignment row.
    sub = Subscription(
        farmer_user_id=farmer.id,
        client_id=effective_client_id,
        package_id=request.package_id,
        promoter_user_id=current_user.id,
        subscription_type=SubscriptionType.ASSIGNED,
        status=SubscriptionStatus.ACTIVE,
        subscription_date=now,
    )
    # Only persist + stamp _confirmed_at when the caller actually
    # provided the measure. Neither branch is the new default — the
    # farmer fills them in on the Crop Dashboard after accept.
    if area_given:
        sub.farm_area_acres = request.farm_area_acres
        sub.farm_area_confirmed_at = now
    elif plant_n_given and plant_y_given:
        sub.number_of_plants = request.number_of_plants
        sub.planting_year = request.planting_year
        sub.plant_count_confirmed_at = now
    db.add(sub)
    await db.flush()
    sub.reference_number = await _generate_reference_for_sub(db, sub.client_id)

    assignment = PromoterAssignment(
        subscription_id=sub.id,
        promoter_user_id=current_user.id,
        promoter_type=request.promoter_type,
        status=AssignmentStatus.PENDING_FARMER_APPROVAL,
    )
    db.add(assignment)
    await db.commit()

    # 2026-05-31 — notify the farmer that a Promoter has assigned a
    # package and is awaiting their approval. Without this, the
    # farmer only finds out by chance the next time they open the
    # PWA Home. Silently best-effort — a missing FCM token or
    # network blip mustn't fail the assignment itself.
    if farmer.fcm_token:
        try:
            from app.services.fcm_service import send_fcm
            promoter_label = (
                "Dealer" if request.promoter_type == "DEALER" else "Facilitator"
            )
            await send_fcm(
                token=farmer.fcm_token,
                title="Advisory assignment request",
                body=(
                    f"{promoter_label} {current_user.name or 'a promoter'} "
                    f"has sent you an advisory subscription. Open the app to "
                    f"approve or decline."
                ),
                data={
                    "type": "PROMOTER_ASSIGNMENT_RECEIVED",
                    "subscription_id": sub.id,
                    "assignment_id": assignment.id,
                },
            )
        except Exception:
            pass

    return {"subscription_id": sub.id, "assignment_id": assignment.id, "status": "Awaiting farmer approval"}


# Alerts E (2026-05-29) — F-P dashboard of subscriptions where they
# are the effective alert recipient. Covers both the explicit override
# (`extra_alert_user_id == current_user.id`) and the auto-promoter
# default for ASSIGNED subs (`promoter_user_id == current_user.id`
# AND no override AND not opted out).

@router.get("/promoter/me/alert-subscriptions")
async def my_alert_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Subscriptions where the current user is the effective extra
    alert recipient — covers both explicit overrides (the farmer typed
    your number) and auto-promoter defaults (ASSIGNED sub, you're the
    assigning promoter, farmer hasn't customised).

    Returns one row per subscription with the farmer + crop + package
    info the PWA needs to render the list, plus `source` so the F-P
    can see which path made them the recipient."""
    from app.modules.subscriptions.models import SubscriptionType

    explicit = (await db.execute(
        select(Subscription, Package, User)
        .join(Package, Package.id == Subscription.package_id)
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            Subscription.extra_alert_user_id == current_user.id,
            Subscription.alerts_extra_disabled.is_(False),
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )).all()

    auto = (await db.execute(
        select(Subscription, Package, User)
        .join(Package, Package.id == Subscription.package_id)
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            Subscription.promoter_user_id == current_user.id,
            Subscription.subscription_type == SubscriptionType.ASSIGNED,
            Subscription.extra_alert_user_id.is_(None),
            Subscription.alerts_extra_disabled.is_(False),
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )).all()

    seen: set[str] = set()
    out: list[dict] = []
    for sub, pkg, farmer in explicit:
        seen.add(sub.id)
        out.append({
            "subscription_id": sub.id,
            "client_id": sub.client_id,
            "package_id": pkg.id,
            "package_name": pkg.name,
            "crop_cosh_id": pkg.crop_cosh_id,
            "farmer_user_id": farmer.id,
            "farmer_name": farmer.name,
            "farmer_phone": farmer.phone,
            "reference_number": sub.reference_number,
            "source": "override",
        })
    for sub, pkg, farmer in auto:
        if sub.id in seen:
            continue   # explicit beats auto if data drift gave us both
        out.append({
            "subscription_id": sub.id,
            "client_id": sub.client_id,
            "package_id": pkg.id,
            "package_name": pkg.name,
            "crop_cosh_id": pkg.crop_cosh_id,
            "farmer_user_id": farmer.id,
            "farmer_name": farmer.name,
            "farmer_phone": farmer.phone,
            "reference_number": sub.reference_number,
            "source": "auto_promoter",
        })
    out.sort(key=lambda r: (r["source"] != "override", (r["farmer_name"] or "").lower()))
    return out


@router.get("/promoter/me/incoming-alerts")
async def my_incoming_alerts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Actual unread alerts addressed to this promoter.

    Returns one row per Alert with status=SENT (i.e. the farmer
    hasn't marked it read yet — once the farmer reads it, it
    disappears from the promoter's list, matching the spec rule
    "alerts vanish once the task is over from the farmer's end").

    Decorated with the farmer's name + phone for tap-to-call, the
    crop's English name from Cosh, and the alert type label
    (START_DATE / INPUT) so the PWA can render a typed chip.

    No package_name in the response — the promoter doesn't need it.
    """
    from app.modules.subscriptions.models import (
        Alert, AlertStatus, AlertType,
    )
    from app.modules.sync.models import CoshCoreItem

    rows = (await db.execute(
        select(Alert, Subscription, Package, User)
        .join(Subscription, Subscription.id == Alert.subscription_id)
        .join(Package, Package.id == Subscription.package_id)
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            Alert.recipient_user_id == current_user.id,
            Alert.status == AlertStatus.SENT,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
        .order_by(Alert.sent_at.desc())
    )).all()

    if not rows:
        return []

    # Resolve crop names in one batch, preferring the recipient's language.
    lang = current_user.language_code or "en"
    crop_ids = {pkg.crop_cosh_id for _, _, pkg, _ in rows if pkg.crop_cosh_id}
    crop_name_by_id: dict[str, str | None] = {}
    if crop_ids:
        for r in (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(crop_ids))
        )).all():
            tr = r.translations or {}
            if isinstance(tr, dict):
                crop_name_by_id[r.cosh_id] = pick_translation(tr, lang, "") or None
            else:
                crop_name_by_id[r.cosh_id] = None

    out = []
    for alert, sub, pkg, farmer in rows:
        out.append({
            "alert_id": alert.id,
            "alert_type": (
                alert.alert_type.value
                if hasattr(alert.alert_type, "value")
                else alert.alert_type
            ),
            "sent_at": alert.sent_at,
            "subscription_id": sub.id,
            "client_id": sub.client_id,
            "farmer_user_id": farmer.id,
            "farmer_name": farmer.name,
            "farmer_phone": farmer.phone,
            "crop_cosh_id": pkg.crop_cosh_id,
            "crop_name": crop_name_by_id.get(pkg.crop_cosh_id) if pkg.crop_cosh_id else None,
        })
    return out


# F-P View Packages (2026-05-29) — F-P-side read-only advisory view.

@router.get("/promoter/assignments/{subscription_id}/today")
async def promoter_assignment_today(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """F-P read-only view of a farmer's assigned advisory.

    Mirrors `/farmer/advisory/today` but scoped to ONE subscription
    that the F-P assigned and is still ACTIVE. Strips conditional-
    question fields (`has_pending_question`, `pending_conditional_question`,
    `blank_path_questions`) before returning — those are the farmer's
    interactive prompts and the F-P shouldn't see / answer them.

    Auth: PromoterAssignment.promoter_user_id == current_user.id AND
    status == ACTIVE. 404 if the assignment doesn't exist or isn't
    owned by this user; 409 if the assignment isn't ACTIVE yet.
    Returns a single AdvisoryDay dict (the farmer endpoint's list
    element shape), or 404 if the sub has no crop_start_date /
    is not yet renderable."""
    assignment = (await db.execute(
        select(PromoterAssignment).where(
            PromoterAssignment.subscription_id == subscription_id,
        )
    )).scalar_one_or_none()
    if assignment is None or assignment.promoter_user_id != current_user.id:
        # Conflate not-found and not-owner to avoid leaking
        # subscription_ids to a wrong-promoter probe.
        raise HTTPException(status_code=404, detail="Assignment not found.")
    if assignment.status != AssignmentStatus.ACTIVE:
        raise HTTPException(status_code=409, detail={
            "code": "assignment_not_active",
            "message": (
                f"Assignment is {assignment.status} — read-only advisory "
                "only renders for ACTIVE assignments."
            ),
        })

    sub = (await db.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found.")

    days = await _today_advisory_for_user(
        db,
        farmer_user_id=sub.farmer_user_id,
        only_subscription_id=subscription_id,
        lang=current_user.language_code or "en",
    )
    if not days:
        # Active assignment but no rendered window yet (e.g. farmer
        # hasn't set crop_start_date). Surface a 404 so the PWA can
        # render an "Awaiting farmer to set the start date" empty
        # state rather than a half-empty advisory.
        raise HTTPException(status_code=404, detail={
            "code": "no_advisory_yet",
            "message": (
                "The farmer hasn't started the crop yet — advisory will "
                "appear once they set the start date."
            ),
        })

    day = days[0]
    # Strip CQ fields per the F-P design: F-P shouldn't see the
    # interactive prompts the farmer is being asked.
    for tl in day.get("timelines", []):
        tl.pop("pending_conditional_question", None)
        tl.pop("blank_path_questions", None)
        if "has_pending_question" in tl:
            tl["has_pending_question"] = False
    return day


# F-P B3 (2026-05-29) — F-P self-cancel of a pending assignment.
WITHDRAW_FCM_TITLE = "Subscription offer withdrawn"
WITHDRAW_FCM_BODY = (
    "Your subscription offer was withdrawn before you responded. "
    "Reach out to the person who sent it if you'd like a new one."
)


@router.delete("/promoter/assignments/{assignment_id}", status_code=200)
async def promoter_cancel_assignment(
    assignment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """F-P withdraws a pending assignment before the farmer responds.

    Refunds 1 unit to the kitty, cancels the linked Subscription,
    sets the Assignment to CANCELLED_BY_PROMOTER, and notifies the
    farmer (FCM PROMOTER_ASSIGNMENT_WITHDRAWN).

    Auth: only the F-P who created the row can withdraw it.
    State: only PENDING_FARMER_APPROVAL — already-ACTIVE / rejected /
    expired / withdrawn rows refuse with 409 to keep the refund
    naturally idempotent."""
    from app.services.promoter_pool import refund_to_promoter

    assignment = (await db.execute(
        select(PromoterAssignment).where(PromoterAssignment.id == assignment_id)
    )).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    if assignment.promoter_user_id != current_user.id:
        raise HTTPException(status_code=403, detail={
            "code": "not_assignment_owner",
            "message": "Only the promoter who created this assignment can cancel it.",
        })
    if assignment.status != AssignmentStatus.PENDING_FARMER_APPROVAL:
        raise HTTPException(status_code=409, detail={
            "code": "assignment_not_pending",
            "message": f"Cannot cancel an assignment in status {assignment.status}.",
        })

    sub = (await db.execute(
        select(Subscription).where(Subscription.id == assignment.subscription_id)
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=500, detail={
            "code": "orphan_assignment",
            "message": "Assignment has no linked subscription.",
        })

    now = datetime.now(timezone.utc)
    assignment.status = AssignmentStatus.CANCELLED_BY_PROMOTER
    assignment.farmer_responded_at = now
    sub.status = SubscriptionStatus.CANCELLED
    try:
        await refund_to_promoter(
            db,
            client_id=sub.client_id,
            promoter_user_id=current_user.id,
        )
    except ValueError:
        # No allocation row — surface in logs but proceed; the
        # status flips are still correct.
        pass

    await db.commit()

    # FCM to the farmer.
    farmer = (await db.execute(
        select(User).where(User.id == sub.farmer_user_id)
    )).scalar_one_or_none()
    if farmer and farmer.fcm_token:
        from app.services.fcm_service import send_fcm
        try:
            await send_fcm(
                token=farmer.fcm_token,
                title=WITHDRAW_FCM_TITLE,
                body=WITHDRAW_FCM_BODY,
                data={
                    "type": "PROMOTER_ASSIGNMENT_WITHDRAWN",
                    "assignment_id": assignment.id,
                    "subscription_id": sub.id,
                },
            )
        except Exception:
            # Graceful degrade — DB state already correct.
            pass

    return {
        "assignment_id": assignment.id,
        "subscription_id": sub.id,
        "status": "Cancelled by promoter; unit refunded to your kitty.",
    }


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
    from app.modules.orders.router import _assert_active_dealer

    await _assert_active_dealer(db, current_user.id)
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
            # PromoterAssignment uses `assigned_at`, not `created_at`.
            # The previous line crashed the endpoint with
            # AttributeError → 500 → PWA load() swallowed it → farmer
            # never saw the pending-approval card.
            "created_at": assignment.assigned_at,
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

    # Resolve the crop's display name from Cosh — prefer the farmer's
    # language, fall back to English. Falls back to None if the crop
    # isn't in `cosh_core_items` yet.
    crop_name: str | None = None
    if package and package.crop_cosh_id:
        from app.modules.sync.models import CoshCoreItem
        lang = current_user.language_code or "en"
        row = (await db.execute(
            select(CoshCoreItem.translations).where(
                CoshCoreItem.cosh_id == package.crop_cosh_id,
            )
        )).scalar_one_or_none()
        if isinstance(row, dict):
            crop_name = pick_translation(row, lang, "") or None

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
        "crop_name": crop_name,
        "package_name": package.name if package else None,
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
    """Farmer approves or rejects Promoter assignment.

    Option A (2026-05-30): the Sub is created ACTIVE at initiate
    time — kitty unit already paid for, no WAITLISTED intermediate.
    Approve here is a no-op on the Sub (it's already ACTIVE);
    the work is on the PromoterAssignment row, which flips from
    PENDING_FARMER_APPROVAL → ACTIVE and unblocks advisory delivery
    (the daily-alerts task filters out subs whose Assignment is
    still PENDING). Reject transitions the Sub ACTIVE → CANCELLED
    (BL11_FARMER allows this) and refunds the unit to the promoter.

    Idempotency is anchored on assignment.status — second-call on
    an already-resolved assignment 404s, so neither
    `farmer_responded_at` nor the refund fires twice.
    """
    sub = await _get_subscription(db, subscription_id, current_user.id)
    approved = data.get("approved", False)

    assignment_result = await db.execute(
        select(PromoterAssignment).where(PromoterAssignment.subscription_id == subscription_id)
    )
    assignment = assignment_result.scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if assignment.status != AssignmentStatus.PENDING_FARMER_APPROVAL:
        # Already responded — second call is a no-op refusal.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "assignment_already_resolved",
                "message": (
                    f"This assignment is already "
                    f"{assignment.status.value if hasattr(assignment.status, 'value') else assignment.status}."
                ),
            },
        )

    now = datetime.now(timezone.utc)
    assignment.status = AssignmentStatus.ACTIVE if approved else AssignmentStatus.REJECTED_BY_FARMER
    assignment.farmer_responded_at = now

    if approved:
        # No Subscription transition — it's already ACTIVE since
        # initiate. Just unblocking advisory delivery via the
        # assignment flip above. subscription_date + reference_number
        # were stamped at initiate time, so nothing to do here.
        pass
    else:
        # Reject — transition Sub ACTIVE → CANCELLED via BL11. The
        # transition table allows this for the FARMER role.
        res = validate_sub_transition(
            sub.status, SubscriptionStatus.CANCELLED.value, BL11_FARMER,
        )
        if not res.allowed:
            _raise_sub_transition(res)
        sub.status = SubscriptionStatus.CANCELLED
        # F-P B2 — refund the unit back to the promoter's kitty.
        # The assignment-already-resolved gate above means this can
        # only run once per assignment, so refunds are naturally
        # idempotent.
        from app.services.promoter_pool import refund_to_promoter
        try:
            await refund_to_promoter(
                db,
                client_id=sub.client_id,
                promoter_user_id=assignment.promoter_user_id,
            )
        except ValueError:
            # No allocation row to refund into — leave consumed_total
            # and skip. CA can investigate via the audit totals.
            pass

    await db.commit()
    return {"status": sub.status, "reference_number": sub.reference_number}


# ── Payment Delegation ────────────────────────────────────────────────────────

class PaymentDelegateRequest(BaseModel):
    requested_from_user_id: Optional[str] = None
    delegate_phone: Optional[str] = None  # phone-based lookup (e.g. "+919876543210")
    role: Optional[str] = None  # DEALER or FACILITATOR (informational)


@router.get("/farmer/delegate-lookup")
async def delegate_lookup(
    phone: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Two-step delegate-payment helper (2026-05-30).

    Returns the resolved user's name, roles, and the companies that
    have onboarded them as a Facilitator / Dealer. The PWA calls this
    when the farmer hits "Check" on the phone-entry screen, then
    renders a confirmation card before letting them submit.

    Mirrors the validation rules in `delegate_payment`:
      - 404 `phone_not_registered` — no user with this phone
      - 422 `delegate_is_self`     — looked-up user is the caller
      - 422 `target_not_facilitator_or_dealer` — user exists but has
        no ACTIVE FACILITATOR/DEALER ClientPromoter row anywhere

    Side-effect-free; safe to call repeatedly as the farmer types.
    """
    from app.modules.auth.service import get_user_by_phone
    from app.modules.clients.models import Client, ClientPromoter

    target = await get_user_by_phone(db, phone)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "phone_not_registered",
                "message": "No user found with this phone number.",
            },
        )
    if target.id == current_user.id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "delegate_is_self",
                "message": "You cannot ask yourself to pay. Choose someone else.",
            },
        )

    # Join across ClientPromoter + Client so we can surface the
    # company names alongside the role badges. One row per
    # (client, promoter_type) so the farmer can see e.g. "Facilitator
    # at Acme; Dealer at Beta".
    rows = (await db.execute(
        select(ClientPromoter, Client)
        .join(Client, Client.id == ClientPromoter.client_id)
        .where(
            ClientPromoter.user_id == target.id,
            ClientPromoter.promoter_type.in_(("FACILITATOR", "DEALER")),
            ClientPromoter.status == "ACTIVE",
        )
    )).all()
    if not rows:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "target_not_facilitator_or_dealer",
                "message": (
                    "This person isn't registered as a Facilitator or "
                    "Dealer with any company. Ask them to register "
                    "via 'Become a Facilitator' or 'Become a Dealer' "
                    "in their app, then try again."
                ),
            },
        )

    affiliations = [
        {
            "role": cp.promoter_type,                          # FACILITATOR / DEALER
            "company_name": client.display_name or client.full_name,
            "client_id": client.id,
        }
        for cp, client in rows
    ]
    roles = sorted({a["role"] for a in affiliations})

    return {
        "user_id": target.id,
        "name": target.name,
        "phone": target.phone,
        "roles": roles,                                        # ["FACILITATOR"], ["DEALER"], or both
        "affiliations": affiliations,
    }


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
    if resolved_user_id == current_user.id:
        # Self-delegation shouldn't be possible from the PWA — frontend
        # blocks it before submit — but guard the backend too so a
        # direct API call can't create a self-targeted row.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "delegate_is_self",
                "message": "You cannot ask yourself to pay. Choose someone else.",
            },
        )

    # 2026-05-30 — refuse at create time when the target isn't an
    # ACTIVE Facilitator or Dealer at any company. Without this guard
    # the row gets created silently and the target user, when they
    # open their PWA Payments tab, hits the role gate on the GET
    # endpoint and never sees the request — the farmer thinks the
    # request was delivered. Per user 2026-05-30: a Facilitator
    # onboarding at any company is sufficient (Promoter designation
    # NOT required); same for Dealer.
    from app.modules.clients.models import ClientPromoter
    has_role = (await db.execute(
        select(ClientPromoter.id).where(
            ClientPromoter.user_id == resolved_user_id,
            ClientPromoter.promoter_type.in_(("FACILITATOR", "DEALER")),
            ClientPromoter.status == "ACTIVE",
        ).limit(1)
    )).scalar_one_or_none()
    if has_role is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "target_not_facilitator_or_dealer",
                "message": (
                    "This person isn't registered as a Facilitator or "
                    "Dealer with any company. Ask them to register "
                    "via 'Become a Facilitator' or 'Become a Dealer' "
                    "in their app, then try again."
                ),
            },
        )

    # Guard: only one PENDING request per subscription at a time. If
    # the farmer already has one outstanding, they must cancel it
    # first (per the 2026-05-29 cancel-and-route rule).
    existing_pending = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.subscription_id == subscription_id,
            SubscriptionPaymentRequest.status == "PENDING",
        )
    )).scalar_one_or_none()
    if existing_pending:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "payment_request_already_pending",
                "message": "A payment request is already pending for this subscription. Cancel it first to send a new one.",
            },
        )

    expires_at = datetime.now(timezone.utc) + timedelta(hours=PAYMENT_REQUEST_EXPIRY_HOURS)
    pr = SubscriptionPaymentRequest(
        subscription_id=subscription_id,
        farmer_user_id=current_user.id,
        requested_from_user_id=resolved_user_id,
        expires_at=expires_at,
    )
    db.add(pr)
    await db.commit()

    # Look up the delegate. Needed for the FCM body AND for the
    # success-screen copy on the farmer's PWA — surface the name and
    # phone so the "Payment request sent" screen can be specific
    # ("Sent to Ravi · +91 99004 00099") instead of a generic green
    # tick.
    from app.modules.platform.models import User as PlatformUser
    from app.services.fcm_service import send_fcm
    delegate = (await db.execute(
        select(PlatformUser).where(PlatformUser.id == resolved_user_id)
    )).scalar_one_or_none()
    if delegate and delegate.fcm_token:
        try:
            await send_fcm(
                token=delegate.fcm_token,
                title="Payment request received",
                body=f"{current_user.name or 'A farmer'} has asked you to pay ₹{int(pr.amount)} for their subscription. You have 24 hours to complete the payment.",
                data={
                    "type": "PAYMENT_REQUEST_RECEIVED",
                    "payment_request_id": pr.id,
                    "subscription_id": subscription_id,
                },
            )
        except Exception:
            pass

    return {
        "detail": "Payment request sent",
        "expires_at": expires_at,
        "requested_from_name": delegate.name if delegate else None,
        "requested_from_phone": delegate.phone if delegate else None,
    }


# ── V1.1 share-payment-link (2026-05-29) ─────────────────────────────────────

@router.get("/farmer/payment-requests/{request_id}")
async def get_my_payment_request(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch a payment request the caller owns (any state). Used by
    the PWA's `/share-link/[id]` page to render the QR + short URL.
    Auth gate: `farmer_user_id == current_user.id`.

    Returns subscription context (package/crop names) too so the
    share page can confirm "for your Tomato · Demo Pack subscription"
    without a second hop."""
    from app.modules.advisory.models import Package
    from app.modules.sync.models import CoshCoreItem

    pr = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.id == request_id,
            SubscriptionPaymentRequest.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Payment request not found")

    sub = (await db.execute(
        select(Subscription).where(Subscription.id == pr.subscription_id)
    )).scalar_one_or_none()
    pkg = (await db.execute(
        select(Package).where(Package.id == sub.package_id)
    )).scalar_one_or_none() if sub else None

    crop_name = None
    if pkg and pkg.crop_cosh_id:
        item = (await db.execute(
            select(CoshCoreItem).where(CoshCoreItem.cosh_id == pkg.crop_cosh_id)
        )).scalar_one_or_none()
        if item:
            tr = item.translations or {}
            lang = current_user.language_code or "en"
            crop_name = pick_translation(tr, lang, pkg.crop_cosh_id)

    now_utc = datetime.now(timezone.utc)
    hours_remaining = max(0, int((pr.expires_at - now_utc).total_seconds() // 3600))

    return {
        "id": pr.id,
        "subscription_id": pr.subscription_id,
        "method": pr.method,
        "status": pr.status,
        "amount": float(pr.amount),
        "short_url": pr.payment_link_short_url,
        "razorpay_payment_link_id": pr.razorpay_payment_link_id,
        "expires_at": pr.expires_at,
        "hours_remaining": hours_remaining,
        "paid_by_vpa": pr.paid_by_vpa,
        "package_name": pkg.name if pkg else None,
        "crop_name": crop_name,
    }


@router.post("/farmer/subscriptions/{subscription_id}/payment-link")
async def create_payment_share_link(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate a Razorpay Payment Link / QR the farmer can share
    with anyone (relatives, friends, etc.). The link is fixed at
    ₹199, single-payment, and `expire_by` is aligned with the
    SubscriptionPaymentRequest's `expires_at` so both sides time
    out together.

    Same single-PENDING guard as `delegate_payment`: the farmer
    must cancel an existing PENDING request before generating a
    new payment link.

    On success, returns the short URL the PWA renders as a QR.
    Recipient pays via any UPI app; Razorpay webhook reconciles
    via `notes.payment_request_id`.
    """
    from app.services.payment_service import create_subscription_payment_link

    sub = await _get_subscription(db, subscription_id, current_user.id)

    existing_pending = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.subscription_id == subscription_id,
            SubscriptionPaymentRequest.status == "PENDING",
        )
    )).scalar_one_or_none()
    if existing_pending:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "payment_request_already_pending",
                "message": "A payment request is already pending for this subscription. Cancel it first to generate a new link.",
            },
        )

    expires_at = datetime.now(timezone.utc) + timedelta(hours=PAYMENT_REQUEST_EXPIRY_HOURS)
    pr = SubscriptionPaymentRequest(
        subscription_id=subscription_id,
        farmer_user_id=current_user.id,
        requested_from_user_id=None,
        method="SHARE_LINK",
        expires_at=expires_at,
    )
    db.add(pr)
    await db.flush()   # need pr.id for the Razorpay notes

    try:
        link = create_subscription_payment_link(
            payment_request_id=pr.id,
            farmer_name=current_user.name,
            expire_in_seconds=PAYMENT_REQUEST_EXPIRY_HOURS * 3600,
        )
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=502,
            detail={
                "code": "payment_link_create_failed",
                "message": "Could not generate the payment link. Please try again.",
                "upstream": str(e),
            },
        )

    pr.razorpay_payment_link_id = link["razorpay_payment_link_id"]
    pr.payment_link_short_url = link["short_url"]
    await db.commit()
    await db.refresh(pr)

    return {
        "payment_request_id": pr.id,
        "short_url": pr.payment_link_short_url,
        "expires_at": pr.expires_at,
        "amount": float(pr.amount),
    }


@router.delete("/farmer/subscriptions/{subscription_id}/delegate-payment")
async def cancel_delegation(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer cancels a pending payment delegation. They can then
    choose someone else or pay themselves. The current delegate is
    notified via FCM so they don't continue waiting (or worse, try
    to pay after the request is cancelled)."""
    from app.modules.platform.models import User as PlatformUser
    from app.services.fcm_service import send_fcm

    from app.services.payment_service import cancel_payment_link

    sub = await _get_subscription(db, subscription_id, current_user.id)
    pending = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.subscription_id == sub.id,
            SubscriptionPaymentRequest.status == "PENDING",
        )
    )).scalars().all()
    notified_user_ids: set[str] = set()
    for pr in pending:
        pr.status = "CANCELLED"
        # SHARE_LINK rows: also revoke the Razorpay link so the URL
        # / QR stop accepting payment immediately. Best-effort —
        # network or already-cancelled errors don't fail our cancel.
        if pr.method == "SHARE_LINK" and pr.razorpay_payment_link_id:
            cancel_payment_link(pr.razorpay_payment_link_id)
        # SHARE_LINK rows have no requested_from_user_id, so skip
        # FCM-to-delegate for those.
        if pr.requested_from_user_id:
            notified_user_ids.add(pr.requested_from_user_id)
    await db.commit()

    if notified_user_ids:
        delegates = (await db.execute(
            select(PlatformUser).where(PlatformUser.id.in_(notified_user_ids))
        )).scalars().all()
        for d in delegates:
            if not d.fcm_token:
                continue
            try:
                await send_fcm(
                    token=d.fcm_token,
                    title="Payment request cancelled",
                    body=f"{current_user.name or 'The farmer'} cancelled their payment request. No action needed.",
                    data={
                        "type": "PAYMENT_REQUEST_CANCELLED_BY_FARMER",
                        "subscription_id": sub.id,
                    },
                )
            except Exception:
                pass

    return {"detail": f"{len(pending)} pending request(s) cancelled"}


@router.get("/dealer/payment-requests")
async def list_payment_requests(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List pending payment requests for this Dealer.

    Decorated 2026-05-30 to match `/facilitator/payment-requests`:
    farmer name + phone (tap-to-call), package + crop name, exact
    amount, and `hours_remaining` (computed from `expires_at`) so the
    UI can show a countdown. Only PENDING rows; PAID / DECLINED /
    CANCELLED rows are historical and drop off the active list.

    Active-dealer gate: must be onboarded as a Dealer at ≥1 client.
    """
    from app.modules.advisory.models import Package
    from app.modules.platform.models import User as PlatformUser
    from app.modules.sync.models import CoshCoreItem
    from app.modules.orders.router import _assert_active_dealer

    await _assert_active_dealer(db, current_user.id)
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

    lang = current_user.language_code or "en"
    crop_ids = {pkg.crop_cosh_id for _, _, pkg, _ in rows if pkg.crop_cosh_id}
    crop_name_by_id: dict[str, str] = {}
    if crop_ids:
        for r in (await db.execute(
            select(CoshCoreItem).where(CoshCoreItem.cosh_id.in_(crop_ids))
        )).scalars().all():
            tr = r.translations or {}
            crop_name_by_id[r.cosh_id] = pick_translation(tr, lang, r.cosh_id)

    now = datetime.now(timezone.utc)
    out = []
    for req, _sub, pkg, farmer in rows:
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


@router.put("/dealer/payment-requests/{request_id}/pay")
async def pay_subscription(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer/facilitator pays — becomes Promoter for this subscription.

    BL-11 audit (2026-05-06): added a transition guard up front so a
    duplicate hit (network retry, double-tap, replay) on an already-
    ACTIVE sub doesn't silently consume a second unit from the
    promoter's allocation and reset subscription_date. The state-
    machine table only allows WAITLISTED → ACTIVE; anything else
    raises NO_OP_TRANSITION or ILLEGAL_TRANSITION before we touch
    the allocation.

    Lifecycle audit follow-up (2026-05-30): added ownership +
    PENDING-only gate to the lookup, mirroring what the sibling
    endpoints (create-order, verify) already do. Pre-fix, any user
    with an allocation at the request's client could pay someone
    else's payment request — silent privilege misuse + the wrong
    promoter_user_id stamped on the resulting Subscription.
    """
    result = await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.id == request_id,
            SubscriptionPaymentRequest.requested_from_user_id == current_user.id,
            SubscriptionPaymentRequest.status == "PENDING",
        )
    )
    pr = result.scalar_one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Payment request not found or no longer pending")

    sub = (await db.execute(select(Subscription).where(Subscription.id == pr.subscription_id))).scalar_one()

    res = validate_sub_transition(
        sub.status, SubscriptionStatus.ACTIVE.value, BL11_DEALER,
    )
    if not res.allowed:
        _raise_sub_transition(res)

    # Phase C: the dealer/facilitator paying on the farmer's behalf
    # becomes the promoter for this subscription. They must have
    # allocation in this company's pool; if not, the payment cannot
    # complete (company hasn't given them units).
    from app.services.promoter_pool import (
        consume_for_assignment, get_promoter_balance,
    )
    promoter_balance = await get_promoter_balance(
        db, sub.client_id, current_user.id,
    )
    if promoter_balance <= 0:
        raise HTTPException(
            status_code=422,
            detail=(
                "You have no subscriptions allocated for this company. "
                "Ask the company admin to allocate units to you before paying for farmer subscriptions."
            ),
        )

    pr.status = "PAID"
    sub.promoter_user_id = current_user.id
    try:
        await consume_for_assignment(
            db, client_id=sub.client_id, promoter_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    sub.status = SubscriptionStatus.ACTIVE
    sub.subscription_date = datetime.now(timezone.utc)
    sub.reference_number = await _generate_reference_for_sub(db, sub.client_id)

    await db.commit()
    return {"status": sub.status, "reference_number": sub.reference_number}


@router.put("/dealer/payment-requests/{request_id}/decline")
async def decline_payment(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer declines a payment request at the outset.

    Brought to parity with `/facilitator/payment-requests/{id}/decline`
    on 2026-05-30. Three gates added:
      - active-dealer (onboarded by ≥1 client),
      - ownership (the request is addressed to *this* dealer),
      - PENDING-only (replay on a terminal row 404s instead of
        silently re-flipping status).
    Plus an FCM notify to the farmer so they know the request was
    declined without waiting out the 24-hour expiry.
    """
    from app.modules.platform.models import User as PlatformUser
    from app.modules.orders.router import _assert_active_dealer
    from app.services.fcm_service import send_fcm

    await _assert_active_dealer(db, current_user.id)
    pr = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.id == request_id,
            SubscriptionPaymentRequest.requested_from_user_id == current_user.id,
            SubscriptionPaymentRequest.status == "PENDING",
        )
    )).scalar_one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Payment request not found or no longer pending")
    pr.status = "DECLINED"
    await db.commit()

    farmer = (await db.execute(
        select(PlatformUser).where(PlatformUser.id == pr.farmer_user_id)
    )).scalar_one_or_none()
    if farmer and farmer.fcm_token:
        try:
            await send_fcm(
                token=farmer.fcm_token,
                title="Payment request declined",
                body=f"{current_user.name or 'Your contact'} declined to pay for your subscription. You can choose someone else or pay yourself.",
                data={
                    "type": "PAYMENT_REQUEST_DECLINED",
                    "subscription_id": pr.subscription_id,
                    "payment_request_id": pr.id,
                },
            )
        except Exception:
            pass   # FCM failure must not break the API response.

    return {"id": request_id, "status": "DECLINED"}


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


async def _generate_reference_for_sub(db: AsyncSession, client_id: str) -> str:
    """BL-15 V1 (Option B, audit 2026-05-06): generate a sequential
    reference number scoped to (client_code, year_two_digit).

    Format: `{client_code}-{YY}-{NNNNNN}` like `PA-26-000147`.
    `client_code` is the first 2 chars of `client.short_name` upper-
    cased, with `RT` as the fallback for too-short short_names.

    Sequential allocation: SELECT the lexicographically-highest
    existing reference matching the (client_code, year) prefix, parse
    its 6-digit suffix, return prefix + (suffix + 1). Lexicographic
    order matches numeric order at fixed 6-digit zero-padding, so this
    walks correctly without a separate counter table.

    Concurrency note: under concurrent activations on the same
    (client, year) bucket, two transactions may compute the same next
    number and one will fail with the unique constraint at commit
    time. The route returns 500 and the frontend retries — a rare
    failure mode given the per-(client, year) activation rate. V2
    will tighten this with a SELECT FOR UPDATE counter row (see
    project_rootstalk_v2_ideas.md). Pre-V1 the format used a 4-digit
    random suffix with ~50% birthday-collision rate at 118 references
    per (client, year), so V1 sequential is a strict improvement.

    Legacy references (V0 format `PADMASHALI26-3847` etc.) are
    invisible to the LIKE pattern `PA-26-%` and don't poison the
    max+1 query. They stay on the row as-is per the BL-15 spec
    rule "Never updated".
    """
    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    short_name = client.short_name if client else ""
    code = client_code_from_short_name(short_name)
    year = two_digit_year()
    prefix = reference_prefix(code, year)

    last = (await db.execute(
        select(Subscription.reference_number)
        .where(Subscription.reference_number.like(f"{prefix}%"))
        .order_by(Subscription.reference_number.desc())
        .limit(1)
    )).scalar_one_or_none()
    if last:
        prev_seq = parse_sequence(last)
        next_seq = prev_seq + 1 if prev_seq >= 0 else 1
    else:
        next_seq = 1
    return format_reference(code, year, next_seq)


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
    """Verify RazorPay signature and activate the subscription.

    BL-11 audit (2026-05-06): added a transition guard so a replayed
    verify payload (signatures stay valid until Razorpay invalidates
    the order) can't bounce a sub back to ACTIVE and reset
    subscription_date. Mirrors the existing dealer_verify_payment
    pattern. Returns NO_OP_TRANSITION on duplicate hits, which the
    PWA can treat as success-ish.
    """
    from app.services.payment_service import verify_payment_signature
    sub = await _get_subscription(db, subscription_id, current_user.id)
    res = validate_sub_transition(
        sub.status, SubscriptionStatus.ACTIVE.value, BL11_FARMER,
    )
    if not res.allowed:
        _raise_sub_transition(res)
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


# ── Staging-only: skip-Razorpay activation ────────────────────────────────────

@router.post("/farmer/subscriptions/{subscription_id}/payment/staging-bypass")
async def staging_bypass_activation(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Activate a subscription without going through Razorpay.

    Why: Razorpay TEST mode rejects real UPI handles ("Payment could
    not be processed") because it isn't on NPCI rails. Demos and
    end-to-end testing of the post-payment flow need a way to flip a
    WAITLISTED sub to ACTIVE without typing `success@razorpay` into
    the checkout sheet every time.

    Hard-gated to non-production environments — refuses with 403 if
    `settings.environment == 'production'`. Mirrors the activation
    side-effects of `/payment/verify` exactly (status, BL-11 guard,
    subscription_date, BL-15 reference number) so the resulting sub
    is indistinguishable from a real one.
    """
    from app.config import settings
    if settings.environment == "production":
        raise HTTPException(
            status_code=403,
            detail="Bypass disabled in production. Use /payment/verify.",
        )
    sub = await _get_subscription(db, subscription_id, current_user.id)
    res = validate_sub_transition(
        sub.status, SubscriptionStatus.ACTIVE.value, BL11_FARMER,
    )
    if not res.allowed:
        _raise_sub_transition(res)
    sub.status = SubscriptionStatus.ACTIVE
    sub.subscription_date = datetime.now(timezone.utc)
    if not sub.reference_number:
        sub.reference_number = await _generate_reference_for_sub(db, sub.client_id)
    await db.commit()
    return {
        "status": sub.status,
        "reference_number": sub.reference_number,
        "bypass": True,
    }


# ── Farmer: Alert preferences ─────────────────────────────────────────────────

@router.post("/farmer/subscriptions/{subscription_id}/alert-preferences")
async def set_alert_preferences(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save the farmer's alert recipient choice.

    Schema (Alerts A+B+C, 2026-05-29):
      { extra_phone: str | null, disabled: bool | null }

    Three intents:
      - `disabled=true` → opt out of every extra recipient (even the
        auto-promoter fallback). The farmer still gets all alerts on
        their own push channel. Returns 200 with the cleared state.
      - `extra_phone` provided → look the phone up in `users`; refuse
        with 422 if no User exists, or if the User isn't a registered
        Dealer / Facilitator. Otherwise store user_id + the resolved
        User row's denormalised phone/name.
      - `extra_phone` empty/None and `disabled` falsy → clear the
        override and the opt-out flag. Defaults take over (auto-
        promoter for ASSIGNED, no extra for SELF).

    The 'extra_name' field that earlier schemas accepted is ignored —
    the name is taken from the resolved User row so the chip always
    reflects the truth (no stale name lingering after the recipient
    changed theirs).
    """
    from app.modules.platform.models import UserRole, RoleType, StatusEnum

    sub = await _get_subscription(db, subscription_id, current_user.id)
    disabled = bool(data.get("disabled"))
    phone_raw = (data.get("extra_phone") or "").strip()

    if disabled:
        sub.alerts_extra_disabled = True
        sub.extra_alert_user_id = None
        sub.extra_alert_phone = None
        sub.extra_alert_name = None
        await db.commit()
        return {"detail": "Alert preferences updated"}

    sub.alerts_extra_disabled = False

    if not phone_raw:
        sub.extra_alert_user_id = None
        sub.extra_alert_phone = None
        sub.extra_alert_name = None
        await db.commit()
        return {"detail": "Alert preferences updated"}

    # Look up the typed phone. Refuse if it doesn't belong to a
    # registered Dealer / Facilitator User.
    target = (await db.execute(
        select(User).where(User.phone == phone_raw)
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=422, detail={
            "code": "user_not_found",
            "message": (
                "No RootsTalk user is registered with this number. "
                "Ask them to register first."
            ),
        })

    has_role = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == target.id,
            UserRole.role_type.in_((RoleType.DEALER, RoleType.FACILITATOR)),
            UserRole.status == StatusEnum.ACTIVE,
        )
    )).scalar_one_or_none() is not None
    if not has_role:
        raise HTTPException(status_code=422, detail={
            "code": "not_a_dealer_or_facilitator",
            "message": (
                "This person isn't registered as a Dealer or Facilitator "
                "on RootsTalk. Pick a number that belongs to one."
            ),
        })

    sub.extra_alert_user_id = target.id
    sub.extra_alert_phone = target.phone
    sub.extra_alert_name = target.name
    await db.commit()
    return {"detail": "Alert preferences updated"}


@router.get("/farmer/subscriptions/{subscription_id}/alert-preferences")
async def get_alert_preferences(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the farmer's effective extra alert recipient.

    Response: { extra_phone, extra_name, source, disabled }

    `source` values:
      - 'disabled'      — farmer explicitly opted out of any extra
                          recipient (alerts_extra_disabled is True).
      - 'override'      — farmer typed a number that resolved to a
                          Dealer / Facilitator User.
      - 'auto_promoter' — ASSIGNED sub, no override, no opt-out;
                          promoter is the default extra recipient.
      - 'none'          — SELF sub with no override, OR ASSIGNED with
                          no reachable promoter.
    """
    sub = await _get_subscription(db, subscription_id, current_user.id)

    if sub.alerts_extra_disabled:
        return {
            "extra_phone": None,
            "extra_name": None,
            "source": "disabled",
            "disabled": True,
        }

    if sub.extra_alert_phone:
        return {
            "extra_phone": sub.extra_alert_phone,
            "extra_name": sub.extra_alert_name,
            "source": "override",
            "disabled": False,
        }

    is_assigned = (
        sub.subscription_type.value
        if hasattr(sub.subscription_type, "value")
        else str(sub.subscription_type)
    ) == "ASSIGNED"
    if is_assigned and sub.promoter_user_id:
        promoter = (await db.execute(
            select(User).where(User.id == sub.promoter_user_id)
        )).scalar_one_or_none()
        if promoter and promoter.phone:
            return {
                "extra_phone": promoter.phone,
                "extra_name": promoter.name,
                "source": "auto_promoter",
                "disabled": False,
            }

    return {
        "extra_phone": None, "extra_name": None,
        "source": "none", "disabled": False,
    }


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

    # Activate the farmer's subscription. BL-11 audit (2026-05-06):
    # swapped the inline `if WAITLISTED` for validate_transition so a
    # replayed verify on an already-ACTIVE sub returns the standard
    # NO_OP_TRANSITION error_code — matches the farmer-side
    # verify_payment behaviour and the dealer-side pay_subscription.
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == pr.subscription_id)
    )).scalar_one_or_none()
    if sub:
        res = validate_sub_transition(
            sub.status, SubscriptionStatus.ACTIVE.value, BL11_DEALER,
        )
        if not res.allowed:
            _raise_sub_transition(res)
        sub.status = SubscriptionStatus.ACTIVE
        sub.subscription_date = datetime.now(timezone.utc)
        if not sub.reference_number:
            sub.reference_number = await _generate_reference_for_sub(db, sub.client_id)

    await db.commit()

    # Notify the farmer that their subscription is now active.
    from app.modules.platform.models import User as PlatformUser
    from app.services.fcm_service import send_fcm
    farmer = (await db.execute(
        select(PlatformUser).where(PlatformUser.id == pr.farmer_user_id)
    )).scalar_one_or_none()
    if farmer and farmer.fcm_token:
        try:
            await send_fcm(
                token=farmer.fcm_token,
                title="Subscription active",
                body=f"{current_user.name or 'Your contact'} paid for your subscription. Open the app to see your crop advisory.",
                data={
                    "type": "SUBSCRIPTION_ACTIVATED",
                    "subscription_id": pr.subscription_id,
                    "reference_number": sub.reference_number if sub else "",
                },
            )
        except Exception:
            pass

    return {
        "status": "PAID",
        "subscription_status": sub.status if sub else None,
        "reference_number": sub.reference_number if sub else None,
    }


# ── Razorpay webhook handler — V1.1 share-payment-link (2026-05-29) ──────────

@router.post("/payment/webhook")
async def razorpay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Razorpay server-to-server webhook for SHARE_LINK payments.

    Authentication: HMAC-SHA256 of the raw request body using the
    configured `razorpay_active_webhook_secret`, compared against
    the `X-Razorpay-Signature` header in constant time. No user
    session is involved — anyone can hit this URL but only Razorpay
    can produce a valid signature.

    Events handled (Razorpay event ID conventions):
      • `payment_link.paid`      — flip PENDING → PAID + activate
                                    subscription + FCM the farmer.
      • `payment_link.cancelled` — flip PENDING → CANCELLED.
      • `payment_link.expired`   — flip PENDING → CANCELLED.
      • anything else            — 200 ack with no state change
                                    (Razorpay's retry policy
                                    expects 2xx; a 4xx would
                                    cause it to keep firing).

    Reconciliation: each Payment Link carries our row id in
    `notes.payment_request_id`, so the lookup is one-shot. We also
    sanity-check the embedded amount matches `SUBSCRIPTION_AMOUNT_PAISE`
    and fail (with 400 to skip activation) on mismatch — defends
    against signed payloads that don't match our pricing.
    """
    from app.services.payment_service import verify_webhook_signature
    from app.services.payment_service import SUBSCRIPTION_AMOUNT_PAISE
    from app.modules.platform.models import User as PlatformUser
    from app.services.fcm_service import send_fcm

    body_bytes = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    if not verify_webhook_signature(body_bytes, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    import json
    try:
        payload = json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = payload.get("event")
    entity = (
        payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    )
    payment_request_id = (entity.get("notes") or {}).get("payment_request_id")
    if not payment_request_id:
        # Not one of ours; ack and ignore so Razorpay doesn't retry.
        return {"ok": True, "ignored": "no payment_request_id in notes"}

    pr = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.id == payment_request_id,
        )
    )).scalar_one_or_none()
    if not pr:
        return {"ok": True, "ignored": "payment_request_id not found"}

    # Idempotency: if we've already moved on from PENDING, ack and
    # bail. A duplicate payment_link.paid (Razorpay retries on 5xx)
    # mustn't double-activate or fire a second FCM.
    if pr.status != "PENDING":
        return {"ok": True, "ignored": f"already {pr.status}"}

    if event == "payment_link.paid":
        # Defence: amount sanity check.
        link_amount = int(entity.get("amount", 0))
        if link_amount != SUBSCRIPTION_AMOUNT_PAISE:
            raise HTTPException(
                status_code=400,
                detail=f"Amount mismatch: expected {SUBSCRIPTION_AMOUNT_PAISE}, got {link_amount}",
            )

        payment_entity = (
            payload.get("payload", {}).get("payment", {}).get("entity", {})
        )
        pr.status = "PAID"
        pr.razorpay_payment_id = payment_entity.get("id")
        # Razorpay's UPI payments carry the payer's VPA in `vpa`.
        # Cards / netbanking won't have it; leave NULL in that case.
        pr.paid_by_vpa = payment_entity.get("vpa")

        sub = (await db.execute(
            select(Subscription).where(Subscription.id == pr.subscription_id)
        )).scalar_one_or_none()
        if sub:
            res = validate_sub_transition(
                sub.status, SubscriptionStatus.ACTIVE.value, BL11_DEALER,
            )
            if res.allowed:
                sub.status = SubscriptionStatus.ACTIVE
                sub.subscription_date = datetime.now(timezone.utc)
                if not sub.reference_number:
                    sub.reference_number = await _generate_reference_for_sub(db, sub.client_id)
        await db.commit()

        # Notify farmer.
        farmer = (await db.execute(
            select(PlatformUser).where(PlatformUser.id == pr.farmer_user_id)
        )).scalar_one_or_none()
        if farmer and farmer.fcm_token:
            try:
                payer = pr.paid_by_vpa or "Your contact"
                await send_fcm(
                    token=farmer.fcm_token,
                    title="Subscription active",
                    body=f"{payer} paid for your subscription. Open the app to see your crop advisory.",
                    data={
                        "type": "SUBSCRIPTION_ACTIVATED",
                        "subscription_id": pr.subscription_id,
                        "reference_number": sub.reference_number if sub else "",
                    },
                )
            except Exception:
                pass

        return {"ok": True, "status": "PAID"}

    if event in ("payment_link.cancelled", "payment_link.expired"):
        pr.status = "CANCELLED"
        await db.commit()

        farmer = (await db.execute(
            select(PlatformUser).where(PlatformUser.id == pr.farmer_user_id)
        )).scalar_one_or_none()
        if farmer and farmer.fcm_token:
            try:
                await send_fcm(
                    token=farmer.fcm_token,
                    title="Payment link expired",
                    body="Your payment link has expired or been cancelled. Generate a new one or pay yourself.",
                    data={
                        "type": "PAYMENT_REQUEST_AUTO_EXPIRED",
                        "subscription_id": pr.subscription_id,
                        "payment_request_id": pr.id,
                    },
                )
            except Exception:
                pass

        return {"ok": True, "status": "CANCELLED", "reason": event}

    # Unknown event — ack so Razorpay doesn't retry.
    return {"ok": True, "ignored": f"unhandled event: {event}"}


# ── Farmer: My subscriptions alias (used by PWA home page) ────────────────────

@router.get("/farmer/my-subscriptions")
async def my_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer's subscriptions, decorated with:

    • package_name + crop_cosh_id + crop_name — so the PWA can
      render "Chilli · DEMO 2" labels on the Home pending-payment
      card without a second-hop lookup per row.
    • pending_payment_from — for WAITLISTED subs only, who owes
      the payment: null = the farmer himself (self-pay or
      abandoned), or {user_id, name, role, expires_at} when the
      farmer has delegated payment to a dealer/facilitator. Drives
      the Home card's CTA shape:
        - self-pending → "Complete payment" + "Cancel"
        - delegated → "Waiting for <Name> (<Role>) to pay" with
          "Pay myself instead" + "Cancel request"
    """
    from app.modules.advisory.models import Package
    from app.modules.sync.models import CoshCoreItem
    from app.modules.platform.models import User as UserModel, UserRole, RoleType
    from app.modules.clients.models import ClientPromoter
    from sqlalchemy import or_

    result = await db.execute(
        select(Subscription)
        .where(Subscription.farmer_user_id == current_user.id)
        .order_by(Subscription.created_at.desc())
    )
    subs = result.scalars().all()
    if not subs:
        return []

    # ── Package + crop name resolution (single round-trip each). ────
    pkg_ids = list({s.package_id for s in subs})
    pkg_rows = (await db.execute(
        select(Package.id, Package.name, Package.crop_cosh_id)
        .where(Package.id.in_(pkg_ids))
    )).all()
    pkg_by_id: dict[str, tuple[str, str]] = {
        pid: (name, crop) for pid, name, crop in pkg_rows
    }

    lang = current_user.language_code or "en"
    crop_ids = {crop for _, crop in pkg_by_id.values() if crop}
    crop_name_by_id: dict[str, str | None] = {}
    if crop_ids:
        name_rows = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(crop_ids))
        )).all()
        for cosh_id, translations in name_rows:
            if isinstance(translations, dict):
                crop_name_by_id[cosh_id] = pick_translation(translations, lang, "")
            else:
                crop_name_by_id[cosh_id] = None

    # ── Client identity per subscription. Surfaced so inside
    # screens (advisory, diagnose, ask-expert, …) can render a
    # consistent "you're in COMPANY · CROP" chip without each
    # page making a separate /client/{id}/info round-trip.
    from app.modules.clients.models import Client
    client_ids = list({s.client_id for s in subs})
    client_info_by_id: dict[str, dict] = {}
    if client_ids:
        client_rows = (await db.execute(
            select(
                Client.id, Client.display_name, Client.full_name,
                Client.logo_url, Client.primary_colour, Client.short_name,
            ).where(Client.id.in_(client_ids))
        )).all()
        for cid, display, full, logo, colour, short in client_rows:
            client_info_by_id[cid] = {
                "client_display_name": display or full,
                "client_logo_url": logo,
                "client_primary_colour": colour,
                "client_short_name": short,
            }

    # ── Pending-delegation resolution for WAITLISTED rows. ──────────
    waitlisted_ids = [s.id for s in subs if str(s.status) == "WAITLISTED"]
    pending_delegate_by_sub_id: dict[str, dict] = {}
    if waitlisted_ids:
        # Find the most recent pending payment request per
        # subscription. There should normally be only one PENDING
        # row per sub, but order-by-created_at picks the newest if
        # the farmer cycled through dealers/facilitators.
        pending_rows = (await db.execute(
            select(SubscriptionPaymentRequest)
            .where(
                SubscriptionPaymentRequest.subscription_id.in_(waitlisted_ids),
                SubscriptionPaymentRequest.status == "PENDING",
            )
            .order_by(SubscriptionPaymentRequest.created_at.desc())
        )).scalars().all()
        # Keep only the newest per sub.
        seen: set[str] = set()
        latest: list[SubscriptionPaymentRequest] = []
        for pr in pending_rows:
            if pr.subscription_id in seen:
                continue
            seen.add(pr.subscription_id)
            latest.append(pr)
        if latest:
            # SHARE_LINK rows have requested_from_user_id=None — drop
            # those before the IN-query so we don't ask Postgres for
            # a user with id=NULL.
            delegate_ids = list({
                pr.requested_from_user_id for pr in latest
                if pr.requested_from_user_id
            })
            name_by_user_id: dict[str, str | None] = {}
            phone_by_user_id: dict[str, str | None] = {}
            role_by_user_id: dict[str, str] = {}
            if delegate_ids:
                user_rows = (await db.execute(
                    select(UserModel.id, UserModel.name, UserModel.phone)
                    .where(UserModel.id.in_(delegate_ids))
                )).all()
                name_by_user_id = {uid: n for uid, n, _ in user_rows}
                phone_by_user_id = {uid: p for uid, _, p in user_rows}
                # Resolve role — prefer ClientPromoter (most specific
                # for delegation context); fall back to UserRole. If
                # both exist, ClientPromoter wins.
                promoter_rows = (await db.execute(
                    select(ClientPromoter.user_id, ClientPromoter.promoter_type)
                    .where(
                        ClientPromoter.user_id.in_(delegate_ids),
                        ClientPromoter.status == "ACTIVE",
                    )
                )).all()
                for uid, ptype in promoter_rows:
                    role_by_user_id[uid] = str(ptype).upper()
                # Fallback to UserRole for any not covered.
                missing = [uid for uid in delegate_ids if uid not in role_by_user_id]
                if missing:
                    ur_rows = (await db.execute(
                        select(UserRole.user_id, UserRole.role_type).where(
                            UserRole.user_id.in_(missing),
                            UserRole.role_type.in_([RoleType.DEALER, RoleType.FACILITATOR]),
                        )
                    )).all()
                    for uid, rt in ur_rows:
                        role_by_user_id.setdefault(
                            uid, rt.value if hasattr(rt, "value") else str(rt),
                        )
            now_utc = datetime.now(timezone.utc)
            for pr in latest:
                delta = pr.expires_at - now_utc
                hours_remaining = max(0, int(delta.total_seconds() // 3600))
                pending_delegate_by_sub_id[pr.subscription_id] = {
                    "payment_request_id": pr.id,
                    # 2026-05-29 share-link: 'DELEGATE' | 'SHARE_LINK'
                    # so the home card can render the right variant.
                    "method": pr.method,
                    "user_id": pr.requested_from_user_id,
                    "name": name_by_user_id.get(pr.requested_from_user_id),
                    "phone": phone_by_user_id.get(pr.requested_from_user_id),
                    "role": role_by_user_id.get(pr.requested_from_user_id, "OTHER"),
                    "short_url": pr.payment_link_short_url,
                    "expires_at": pr.expires_at,
                    "hours_remaining": hours_remaining,
                }

    # ── Compose response. ─────────────────────────────────────────
    out = []
    # Per-crop AREA_WISE / PLANT_WISE measure from Cosh. Drives the
    # Crop Dashboard's conditional fields. Untyped crops default to
    # AREA_WISE so legacy data renders correctly.
    from app.services.cosh_crop_view import get_measure_for_biological_name
    measure_by_crop: dict[str, str] = {}
    for cid in crop_ids:
        if cid:
            measure = await get_measure_for_biological_name(db, cid)
            measure_by_crop[cid] = measure or "AREA_WISE"

    # Per-client: does the client have at least one ACTIVE PRIMARY
    # FarmPundit? Drives the "Ask Expert" gate on the PWA. Without
    # a Primary, the routing chain (preference → Promoter-Pundit →
    # round-robin Primary) has nowhere to land and the query would
    # be orphaned. One batch query covers every client referenced
    # by the farmer's subscriptions.
    from app.modules.farmpundit.models import (
        ClientFarmPundit, PunditRole,
    )
    client_ids_for_pundit_check = list({s.client_id for s in subs})
    has_primary_by_client: dict[str, bool] = {cid: False for cid in client_ids_for_pundit_check}
    if client_ids_for_pundit_check:
        primary_rows = (await db.execute(
            select(ClientFarmPundit.client_id).where(
                ClientFarmPundit.client_id.in_(client_ids_for_pundit_check),
                ClientFarmPundit.role == PunditRole.PRIMARY,
                ClientFarmPundit.status == "ACTIVE",
            )
        )).scalars().all()
        for cid in primary_rows:
            has_primary_by_client[cid] = True

    # 2026-05-31 — Promoter-assigned subs awaiting the farmer's
    # explicit approval are OMITTED from /farmer/my-subscriptions
    # entirely. The farmer interacts with them only via the
    # pending-approval card from /farmer/assignments/pending. Once
    # they accept, the assignment flips to ACTIVE and the sub
    # appears in this list as a normal active subscription. Without
    # this filter, a Promoter-assigned sub was visible to the farmer
    # as an active crop tile from the moment the Promoter initiated —
    # effectively treating it as approved without the farmer's nod.
    pending_approval_sub_ids: set[str] = set()
    sub_ids_assigned = [s.id for s in subs if s.subscription_type == SubscriptionType.ASSIGNED]
    if sub_ids_assigned:
        rows = (await db.execute(
            select(PromoterAssignment.subscription_id).where(
                PromoterAssignment.subscription_id.in_(sub_ids_assigned),
                PromoterAssignment.status == AssignmentStatus.PENDING_FARMER_APPROVAL,
            )
        )).scalars().all()
        pending_approval_sub_ids = set(rows)
    subs = [s for s in subs if s.id not in pending_approval_sub_ids]
    if not subs:
        return []

    # 2026-06-06 — Resolve farmer's district name once via Cosh
    # core_items so every History card can show "Crop · Company ·
    # District" context without a per-card lookup.
    farmer_district_name: str | None = None
    if current_user.district_cosh_id:
        d_row = (await db.execute(
            select(CoshCoreItem.translations).where(
                CoshCoreItem.cosh_id == current_user.district_cosh_id
            )
        )).scalar_one_or_none()
        if isinstance(d_row, dict):
            farmer_district_name = pick_translation(d_row, lang, "") or None

    for s in subs:
        pkg_name, crop_cosh_id = pkg_by_id.get(s.package_id, (None, None))
        client = client_info_by_id.get(s.client_id, {})
        measure = measure_by_crop.get(crop_cosh_id, "AREA_WISE") if crop_cosh_id else "AREA_WISE"
        out.append({
            "id": s.id, "client_id": s.client_id, "package_id": s.package_id,
            "status": s.status, "crop_start_date": s.crop_start_date,
            "crop_start_date_first_set_at": s.crop_start_date_first_set_at,
            "reference_number": s.reference_number, "subscription_type": s.subscription_type,
            # Area-wise context
            "farm_area_acres": float(s.farm_area_acres) if s.farm_area_acres is not None else None,
            "area_unit": s.area_unit,
            "farm_area_confirmed_at": s.farm_area_confirmed_at,
            # Plant-wise context (2026-05-27)
            "number_of_plants": s.number_of_plants,
            "planting_year": s.planting_year,
            "plant_count_confirmed_at": s.plant_count_confirmed_at,
            # Crop typing — area-wise vs plant-wise. PWA renders the
            # right input set per this value.
            "crop_measure": measure,
            "crop_age": _compute_crop_age(s, measure),
            "package_name": pkg_name,
            "crop_cosh_id": crop_cosh_id,
            "crop_name": crop_name_by_id.get(crop_cosh_id) if crop_cosh_id else None,
            "client_display_name": client.get("client_display_name"),
            "client_logo_url": client.get("client_logo_url"),
            "client_primary_colour": client.get("client_primary_colour"),
            "client_short_name": client.get("client_short_name"),
            # Drives the Ask Expert button + Diagnose-IDK gateway gate
            # on the PWA. False → no PRIMARY pundit is available to
            # receive a query at this client.
            "client_has_primary_expert": has_primary_by_client.get(s.client_id, False),
            "pending_payment_from": pending_delegate_by_sub_id.get(s.id),
            # 2026-06-06 — Farmer's district for the History card
            # context line. Same value across all rows of one
            # response; surfaced per-row so the PWA doesn't need a
            # separate /profile fetch.
            "farmer_district_name": farmer_district_name,
        })
    return out


PLANTING_YEAR_FLOOR = 1970   # earliest year exposed in the dropdown


def _compute_crop_age(sub: Subscription, measure: str) -> dict | None:
    """Single-source crop-age calc for the Crop Dashboard.

    - AREA_WISE: days since crop_start_date.
    - PLANT_WISE: years since planting_year.
      * If planting_year < PLANTING_YEAR_FLOOR (i.e. the farmer
        selected "Beyond 1970" — stored as the sentinel 1969 — or
        any legacy value), the envelope returns the age as
        `current_year - PLANTING_YEAR_FLOOR` and sets
        `is_minimum: true`. PWA renders this as "> 56 years"
        (or whatever the current difference is).

    Returns `{value, unit, source, is_minimum}` or None when the
    source field is missing.
    """
    from datetime import date as _date
    if measure == "PLANT_WISE":
        if sub.planting_year is None:
            return None
        current_year = _date.today().year
        py = int(sub.planting_year)
        if py < PLANTING_YEAR_FLOOR:
            return {
                "value": current_year - PLANTING_YEAR_FLOOR,
                "unit": "years",
                "source": "PLANTING_YEAR",
                "is_minimum": True,
            }
        age_years = current_year - py
        if age_years < 0:
            return None
        return {
            "value": age_years, "unit": "years",
            "source": "PLANTING_YEAR", "is_minimum": False,
        }
    # AREA_WISE (default)
    if sub.crop_start_date is None:
        return None
    start = sub.crop_start_date.date() if hasattr(sub.crop_start_date, "date") else sub.crop_start_date
    age_days = (_date.today() - start).days
    if age_days < 0:
        return None
    return {
        "value": age_days, "unit": "days",
        "source": "START_DATE", "is_minimum": False,
    }


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
    """BL-02: Store farmer's YES/NO/BLANK answer to a conditional question
    for today. Idempotent: replaces today's answer if one already exists.

    Subscription ownership is verified — a farmer can only submit answers
    against their own subscriptions. Returns 404 (subscription not found
    OR not owned by caller) instead of leaking the distinction.
    """
    from datetime import date
    if request.answer not in ("YES", "NO", "BLANK"):
        raise HTTPException(status_code=422, detail="answer must be YES, NO, or BLANK")

    # Ownership gate — `_get_subscription` already does the join + 404 path.
    await _get_subscription(db, request.subscription_id, current_user.id)

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

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_uuid(s: str | None) -> bool:
    return bool(s and _UUID_RE.match(s))


@router.get("/farmer/advisory/today")
async def get_today_advisory(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return today's active practices for all the farmer's ACTIVE subscriptions.
    Applies BL-04 (DAS/DBS window) + BL-02 (conditional filtering) +
    BL-03 (deduplication across CCA + triggered CHA timelines).
    """
    return await _today_advisory_for_user(
        db, farmer_user_id=current_user.id, only_subscription_id=None,
        lang=current_user.language_code or "en",
    )


# ── Practice acknowledgement: "I've done this" tick ───────────────────────────
# Three actions, all upserts on the same composite key.
#   mark   — green tick. Counts off the badge. Reveals "Delete" button.
#   unmark — grey tick again. Re-counts toward badge. Delete button vanishes.
#   hide   — only allowed after mark. Practice disappears from the farmer's
#            UI; the row stays so re-publishes / re-renders don't resurrect
#            it. No undo from the PWA.
class _PracticeAckBody(BaseModel):
    subscription_id: str
    timeline_lineage_id: str
    practice_id: str
    occurrence_date: date


async def _upsert_practice_ack(
    db: AsyncSession,
    farmer_user_id: str,
    body: _PracticeAckBody,
    action: str,
):
    from app.modules.advisory.models import PracticeAcknowledgement
    from app.modules.subscriptions.models import Subscription

    # Auth: the subscription must belong to this farmer.
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == body.subscription_id,
            Subscription.farmer_user_id == farmer_user_id,
        )
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    ack = (await db.execute(
        select(PracticeAcknowledgement).where(
            PracticeAcknowledgement.subscription_id == body.subscription_id,
            PracticeAcknowledgement.timeline_lineage_id == body.timeline_lineage_id,
            PracticeAcknowledgement.practice_id == body.practice_id,
            PracticeAcknowledgement.occurrence_date == body.occurrence_date,
        )
    )).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if ack is None:
        ack = PracticeAcknowledgement(
            farmer_user_id=farmer_user_id,
            subscription_id=body.subscription_id,
            timeline_lineage_id=body.timeline_lineage_id,
            practice_id=body.practice_id,
            occurrence_date=body.occurrence_date,
        )
        db.add(ack)

    if action == "mark":
        ack.marked_at = now
        ack.hidden_at = None
    elif action == "unmark":
        ack.marked_at = None
        ack.hidden_at = None
    elif action == "hide":
        # User decision (2026-06-19): hide is only allowed after mark.
        # If the farmer somehow hits this without marking first (stale
        # PWA), reject so the state machine stays clean.
        if ack.marked_at is None:
            raise HTTPException(
                status_code=400,
                detail="Cannot hide a practice that hasn't been marked done",
            )
        ack.hidden_at = now
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    await db.commit()
    return {
        "subscription_id": ack.subscription_id,
        "timeline_lineage_id": ack.timeline_lineage_id,
        "practice_id": ack.practice_id,
        "occurrence_date": ack.occurrence_date.isoformat(),
        "ack_status": (
            "HIDDEN" if ack.hidden_at else "MARKED" if ack.marked_at else "ACTIVE"
        ),
    }


@router.post("/farmer/practice-ack/mark")
async def mark_practice(
    body: _PracticeAckBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _upsert_practice_ack(db, current_user.id, body, "mark")


@router.post("/farmer/practice-ack/unmark")
async def unmark_practice(
    body: _PracticeAckBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _upsert_practice_ack(db, current_user.id, body, "unmark")


@router.post("/farmer/practice-ack/hide")
async def hide_practice(
    body: _PracticeAckBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _upsert_practice_ack(db, current_user.id, body, "hide")


# ── Dashboard attention counts ────────────────────────────────────────────────
@router.get("/farmer/dashboard/attention")
async def get_dashboard_attention(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Aggregate "things needing the farmer's action" across every
    active subscription. Feeds three dashboards: Crop (single-sub
    drill), Company (per-client_id rollup), Farmer Main (grand total).

    Counts by bucket:
      - advisory_unmarked: practices visible today that aren't marked
        and aren't hidden (the unified advisory signal — replaces
        new/viewed/acted-on tracking via the ack table).
      - orders_awaiting_approval: OrderItem.status = SENT_FOR_APPROVAL.
      - orders_returned: OrderItem.status = NOT_AVAILABLE (farmer
        must reroute) — only on direct-to-dealer orders (facilitator-
        held NOT_AVAILABLE belongs to the facilitator's queue).
      - orders_pickup_ready: PackingList.shared, not yet received.
      - seeds_*: equivalents on SeedOrderFull (SENT_FOR_APPROVAL,
        NOT_AVAILABLE, READY_FOR_PICKUP).
      - queries_responded: Query.status = RESPONDED, unread.
      - payments_pending: PaymentRequest.status = PENDING.

    Hero-strip + per-tile badge sources. PWA computes its own total
    (sum of buckets) so we don't impose a tile model from here.
    """
    from app.modules.advisory.models import Practice
    from app.modules.orders.models import Order, OrderItem, OrderItemStatus, PackingList
    from app.modules.seed_mgmt.models import SeedOrderFull, SeedOrderStatus
    from app.modules.subscriptions.models import Subscription, SubscriptionStatus

    subs = (await db.execute(
        select(Subscription).where(
            Subscription.farmer_user_id == current_user.id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )).scalars().all()

    by_sub: dict[str, dict] = {}
    by_company: dict[str, dict] = {}

    # Pre-compute advisory_unmarked by running the today-advisory and
    # counting ACTIVE-state practices. Re-using the existing kernel
    # keeps the badge math identical to what the farmer sees.
    advisory = await _today_advisory_for_user(
        db, farmer_user_id=current_user.id, only_subscription_id=None,
        lang=current_user.language_code or "en",
    )
    advisory_by_sub: dict[str, int] = {}
    for day in advisory:
        sub_id = day.get("subscription_id")
        if not sub_id:
            continue
        count = 0
        for tl in day.get("timelines") or []:
            for p in tl.get("practices") or []:
                if p.get("ack_status") == "ACTIVE":
                    # Per user direction (2026-06-19): for INPUT
                    # practices the tick only appears post-purchase.
                    # An INPUT that's pre-purchase shouldn't count
                    # toward "unmarked" since the farmer can't act
                    # on it as a practice yet.
                    if p.get("l0_type") == "INPUT" and not p.get("is_purchased"):
                        continue
                    count += 1
        advisory_by_sub[sub_id] = count

    for sub in subs:
        bucket = {
            "subscription_id": sub.id,
            "client_id": sub.client_id,
            "advisory_unmarked": advisory_by_sub.get(sub.id, 0),
            "orders_awaiting_approval": 0,
            "orders_returned": 0,
            "orders_pickup_ready": 0,
            "seeds_awaiting_approval": 0,
            "seeds_returned": 0,
            "seeds_pickup_ready": 0,
            "queries_responded": 0,
            "payments_pending": 0,
        }

        # Regular orders' attention items.
        order_rows = (await db.execute(
            select(Order).where(
                Order.subscription_id == sub.id,
                Order.farmer_user_id == current_user.id,
            )
        )).scalars().all()
        for o in order_rows:
            items = (await db.execute(
                select(OrderItem).where(
                    OrderItem.order_id == o.id,
                    OrderItem.archived_at.is_(None),
                )
            )).scalars().all()
            bucket["orders_awaiting_approval"] += sum(
                1 for i in items if i.status == OrderItemStatus.SENT_FOR_APPROVAL
            )
            # Returned items only belong to the farmer when the order
            # is direct-to-dealer; facilitator-held NOT_AVAILABLE is
            # the facilitator's queue.
            if not o.facilitator_user_id:
                bucket["orders_returned"] += sum(
                    1 for i in items if i.status == OrderItemStatus.NOT_AVAILABLE
                )
            # Pickup ready = packing list shared + nothing received yet
            # + at least one APPROVED item present.
            pl = (await db.execute(
                select(PackingList).where(PackingList.order_id == o.id)
            )).scalar_one_or_none()
            if (
                pl is not None
                and pl.shared_at is not None
                and pl.farmer_received_at is None
                and any(i.status == OrderItemStatus.APPROVED for i in items)
            ):
                bucket["orders_pickup_ready"] += 1

        # Seed orders' attention items.
        seed_rows = (await db.execute(
            select(SeedOrderFull).where(
                SeedOrderFull.subscription_id == sub.id,
                SeedOrderFull.farmer_user_id == current_user.id,
            )
        )).scalars().all()
        for so in seed_rows:
            if so.status == SeedOrderStatus.SENT_FOR_APPROVAL.value:
                bucket["seeds_awaiting_approval"] += 1
            elif so.status == SeedOrderStatus.NOT_AVAILABLE.value:
                bucket["seeds_returned"] += 1
            elif so.status == SeedOrderStatus.READY_FOR_PICKUP.value:
                bucket["seeds_pickup_ready"] += 1

        bucket["total"] = (
            bucket["advisory_unmarked"]
            + bucket["orders_awaiting_approval"]
            + bucket["orders_returned"]
            + bucket["orders_pickup_ready"]
            + bucket["seeds_awaiting_approval"]
            + bucket["seeds_returned"]
            + bucket["seeds_pickup_ready"]
            + bucket["queries_responded"]
            + bucket["payments_pending"]
        )
        by_sub[sub.id] = bucket
        cid = sub.client_id
        co = by_company.setdefault(cid, {
            "client_id": cid, "total": 0, "subscription_ids": [],
        })
        co["total"] += bucket["total"]
        co["subscription_ids"].append(sub.id)

    grand_total = sum(b["total"] for b in by_sub.values())
    return {
        "by_subscription": by_sub,
        "by_company": list(by_company.values()),
        "grand_total": grand_total,
    }


async def _l2_name_loc_map(db: AsyncSession, lang: str) -> dict[str, str]:
    """Returns `{L2_enum: localised_name}` for every L2 we have a Cosh
    UUID mapping for. Reads `cosh_core_items.translations` once per
    request and picks the user's locale via `pick_translation`.
    Missing translations fall through silently so the PWA can humanize
    the enum as a last resort.
    """
    from app.modules.sync.models import CoshCoreItem
    from app.services.cosh_constants import PYTHON_L2_TO_COSH_UUID
    cosh_ids = list(PYTHON_L2_TO_COSH_UUID.values())
    if not cosh_ids:
        return {}
    rows = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
        .where(CoshCoreItem.cosh_id.in_(cosh_ids))
    )).all()
    rev = {v: k for k, v in PYTHON_L2_TO_COSH_UUID.items()}
    out: dict[str, str] = {}
    for cid, tr in rows:
        if not isinstance(tr, dict):
            continue
        enum = rev.get(cid)
        if enum is None:
            continue
        name = pick_translation(tr, lang, "")
        if name:
            out[enum] = name
    return out


async def _today_advisory_for_user(
    db: AsyncSession,
    *,
    farmer_user_id: str,
    only_subscription_id: Optional[str] = None,
    lang: str = "en",
):
    """Shared kernel for the today-advisory view.

    Extracted (F-P View Packages, 2026-05-29) so the F-P read-only
    viewer can render the same BL-02/03/04 + snapshot machinery for
    one assigned subscription without duplicating the 400-line body.
    When `only_subscription_id` is set, the query narrows to that
    single sub (still gated on farmer_user_id + ACTIVE + crop_start)
    and the result is the one-element list — caller is expected to
    pick out [0] and handle the empty case.
    """
    l2_name_loc = await _l2_name_loc_map(db, lang)
    today = date.today()

    # All ACTIVE subscriptions with a crop_start_date — narrowed if
    # the caller asked for one specific assignment.
    q = select(Subscription).where(
        Subscription.farmer_user_id == farmer_user_id,
        Subscription.status == SubscriptionStatus.ACTIVE,
        Subscription.crop_start_date != None,  # noqa: E711
    )
    if only_subscription_id is not None:
        q = q.where(Subscription.id == only_subscription_id)
    subs_result = await db.execute(q)
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

        # Perennial gates (rules confirmed 2026-06-18):
        #   (1) 365-day window from crop_start drives the package
        #       duration. Perennial packages have duration_days forced
        #       to 365 by the CCA author flow; this gate makes the
        #       advisory honor the same lifespan at read time.
        #   (2) No advisory pre-start. CALENDAR timelines never fire
        #       before crop_start_date even when today's day-of-year
        #       happens to fall inside one of their authored windows.
        # Annual packages keep their existing DBS/DAS behaviour (DBS
        # practices DO show pre-start by design — seed treatment etc.).
        from datetime import timedelta as _td
        pkg_type_str = pkg.package_type.value if hasattr(pkg.package_type, "value") else str(pkg.package_type)
        perennial_out_of_window = False
        if pkg_type_str == "PERENNIAL":
            perennial_end = crop_start + _td(days=365)
            if today < crop_start or today > perennial_end:
                perennial_out_of_window = True

        # Load all timelines for this package
        if perennial_out_of_window:
            timelines: list = []
        else:
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

        # 2026-06-19 — Per-occurrence practice acks for this sub.
        # Key shape: (timeline_lineage_id, practice_id, occurrence_date).
        # Filter out hidden practices later; surface marked state on the
        # rest so the PWA can render the green tick.
        from app.modules.advisory.models import PracticeAcknowledgement
        ack_rows = (await db.execute(
            select(PracticeAcknowledgement).where(
                PracticeAcknowledgement.subscription_id == sub.id,
            )
        )).scalars().all()
        ack_by_key: dict[tuple[str, str, date], PracticeAcknowledgement] = {
            (a.timeline_lineage_id, a.practice_id, a.occurrence_date): a
            for a in ack_rows
        }
        # 2026-06-19 — Key by lineage_id, not timeline_id. After a
        # publish, the sub's package has new Timeline rows with new
        # ids but the SAME lineage_id as their pre-publish ancestors.
        # Looking up by lineage_id finds the snapshot stored from the
        # ancestor view (BL-13 step 4: locked timelines stay frozen
        # across publishes).
        cca_snap_by_lineage: dict = {s.lineage_id: s for s in existing_cca_snaps}

        active_timelines = []
        for tl in timelines:
            existing_snap = cca_snap_by_lineage.get(tl.lineage_id)
            meta = (
                metadata_from_content(existing_snap.content)
                if existing_snap is not None
                else metadata_from_master_cca(tl)
            )
            # 2026-06-03 — pass today_date so CALENDAR (Perennial)
            # timelines can compute day-of-year. Without it CALENDAR
            # silently returned False and perennial farmers saw
            # nothing on /farmer/advisory/today regardless of which
            # day-of-year their authored window covers.
            if cca_window_active(meta, day_offset, today_date=today):
                active_timelines.append((tl, day_offset, meta))

        from app.services.bl03_deduplication import (
            deduplicate_advisory, TimelineWindow as TLWindow,
            PracticeStub as PStub, PracticeElement as PEl,
        )
        from app.modules.orders.models import Order, OrderItem
        from datetime import timedelta

        # NOTE: do NOT early-return when active_timelines is empty — a farmer
        # may still have an ACTIVE TriggeredCHAEntry (diagnosis-driven
        # advisory) whose window includes today even when no CCA timeline is
        # in window right now. The downstream loops below all handle empty
        # CCA correctly (no-op iteration), and the CHA branches will still
        # populate tl_windows.

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

            from_d, to_d = cca_calendar_dates(meta, crop_start, today)

            tl_window = TLWindow(
                id=tl.id,
                name=(content.get("timeline") or {}).get("name") or tl.name,
                from_date=from_d, to_date=to_d,
                created_at=tl.created_at.date() if hasattr(tl.created_at, 'date') else today,
                practices=rendered.practice_stubs, source="CCA",
                lineage_id=tl.lineage_id,
            )
            tl_windows.append(tl_window)
            tl_date_map[tl.id] = (from_d, to_d, day_num)

        # ── Load triggered CHA timelines (from diagnosis or FarmPundit queries) ─
        # Perennial out-of-window subs surface NO advisory at all —
        # CHA recommendations are gated on the same window as CCA.
        if perennial_out_of_window:
            cha_entries: list = []
        else:
            cha_entries = (await db.execute(
                select(TriggeredCHAEntry).where(
                    TriggeredCHAEntry.subscription_id == sub.id,
                    TriggeredCHAEntry.status == "ACTIVE",
                )
            )).scalars().all()

        # Localise CHA problem labels on the read path. The stored
        # `cha.problem_name` is a snapshot — diagnosis used to write
        # the English string (cha_hierarchy.py:81 hardcoded `.get("en")`
        # before the 2026-06-18 fix), and existing rows still carry
        # the English snapshot. Resolve each `problem_cosh_id` against
        # the current farmer's locale via `pick_translation`, falling
        # back to the snapshot so QA rows (which store the farmer's
        # own question text, not a cosh_id) keep their label.
        cha_problem_loc: dict[str, str] = await resolve_names_by_cosh_id(
            db,
            {c.problem_cosh_id for c in cha_entries if c.problem_cosh_id},
            lang,
        ) if cha_entries else {}

        # Per-timeline CHA/QA metadata for the response composer.
        # Keyed by the synthetic `cha_tl_id` we build below so it
        # survives BL-03's dedup pass (which keeps the same `tl.id`).
        cha_meta_by_tl_id: dict[str, dict] = {}

        # 2026-06-19 — Raw CHA timeline IDs the active loop adds to
        # tl_windows (under synthetic ids `cha-{src}-{raw}`). The
        # BL-03 context-only pass excludes these so the same CHA
        # timeline isn't also added as a context-only entry — that
        # would (a) double-include the timeline, and (b) make the
        # context-only path wrongly anchor CHA offsets to
        # crop_start_date instead of triggered_at. See the same-day
        # fix in services/order_bundle.py for the order-side mirror.
        active_cha_raw_tl_ids: set[str] = set()

        for cha in cha_entries:
            if cha.recommendation_type == "SP":
                sp_timelines = (await db.execute(
                    select(Timeline).where(Timeline.sp_recommendation_id == cha.recommendation_id)
                )).scalars().all()
                for sp_tl in sp_timelines:
                    # CHA window check uses snapshot's frozen offsets if a
                    # snapshot exists, else master. Lookup by
                    # lineage_id so snapshots survive publish clones
                    # (2026-06-19).
                    sp_snap = (await db.execute(
                        select(LockedTimelineSnapshot).where(
                            LockedTimelineSnapshot.subscription_id == sub.id,
                            LockedTimelineSnapshot.lineage_id == sp_tl.lineage_id,
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
                    problem_label = (
                        cha_problem_loc.get(cha.problem_cosh_id)
                        or cha.problem_name
                        or cha.problem_cosh_id
                    )
                    tl_windows.append(TLWindow(
                        id=cha_tl_id, name=f"CHA — {problem_label}: {sp_tl.name}",
                        from_date=from_d, to_date=to_d,
                        created_at=cha.triggered_at.date() if hasattr(cha.triggered_at, 'date') else today,
                        practices=stubs, source="CHA",
                        lineage_id=f"cha-sp-{sp_tl.lineage_id}",
                    ))
                    tl_date_map[cha_tl_id] = (from_d, to_d, 0)
                    cha_meta_by_tl_id[cha_tl_id] = {
                        "problem_name": problem_label,
                        "triggered_at": cha.triggered_at.isoformat()
                            if hasattr(cha.triggered_at, "isoformat") else None,
                    }
                    active_cha_raw_tl_ids.add(sp_tl.id)
            elif cha.recommendation_type == "PG":
                pg_timelines = (await db.execute(
                    select(Timeline).where(Timeline.pg_recommendation_id == cha.recommendation_id)
                )).scalars().all()
                for pg_tl in pg_timelines:
                    pg_snap = (await db.execute(
                        select(LockedTimelineSnapshot).where(
                            LockedTimelineSnapshot.subscription_id == sub.id,
                            LockedTimelineSnapshot.lineage_id == pg_tl.lineage_id,
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
                    problem_label = (
                        cha_problem_loc.get(cha.problem_cosh_id)
                        or cha.problem_name
                        or cha.problem_cosh_id
                    )
                    tl_windows.append(TLWindow(
                        id=cha_tl_id, name=f"CHA — {problem_label}: {pg_tl.name}",
                        from_date=from_d, to_date=to_d,
                        created_at=cha.triggered_at.date() if hasattr(cha.triggered_at, 'date') else today,
                        practices=stubs, source="CHA",
                        lineage_id=f"cha-pg-{pg_tl.lineage_id}",
                    ))
                    tl_date_map[cha_tl_id] = (from_d, to_d, 0)
                    cha_meta_by_tl_id[cha_tl_id] = {
                        "problem_name": problem_label,
                        "triggered_at": cha.triggered_at.isoformat()
                            if hasattr(cha.triggered_at, "isoformat") else None,
                    }
                    active_cha_raw_tl_ids.add(pg_tl.id)
            elif cha.recommendation_type == "QA":
                # UCAT pipe-3: Q&A timelines live in pg_timelines via
                # standard_response_id. Mirror PG branch but keyed
                # off the polymorphic FK and labelled with the
                # question text (cha.problem_name set by
                # _trigger_qa_for_query). source="QA" so the PWA
                # can render the Pundit-origin icon.
                qa_timelines = (await db.execute(
                    select(Timeline).where(Timeline.standard_response_id == cha.recommendation_id)
                )).scalars().all()
                for qa_tl in qa_timelines:
                    qa_snap = (await db.execute(
                        select(LockedTimelineSnapshot).where(
                            LockedTimelineSnapshot.subscription_id == sub.id,
                            LockedTimelineSnapshot.lineage_id == qa_tl.lineage_id,
                            LockedTimelineSnapshot.source == "QA",
                        )
                    )).scalar_one_or_none()
                    if qa_snap is not None:
                        meta = metadata_from_content(qa_snap.content)
                    else:
                        meta = metadata_from_content({"timeline": {
                            "from_type": "DAS",
                            "from_value": int(qa_tl.from_value),
                            "to_value": int(qa_tl.to_value),
                        }})
                    from_d, to_d = cha_calendar_dates(meta, cha.triggered_at.date())
                    if not (from_d <= today <= to_d):
                        continue
                    content, _locked = await resolve_cha_content(db, sub.id, qa_tl.id, "QA")
                    stubs = render_cha_from_content(content)
                    cha_tl_id = f"cha-qa-{qa_tl.id}"
                    question_label = cha.problem_name or "Pundit response"
                    tl_windows.append(TLWindow(
                        id=cha_tl_id, name=f"Q&A — {question_label}: {qa_tl.name}",
                        from_date=from_d, to_date=to_d,
                        created_at=cha.triggered_at.date() if hasattr(cha.triggered_at, 'date') else today,
                        practices=stubs, source="QA",
                        lineage_id=f"cha-qa-{qa_tl.lineage_id}",
                    ))
                    tl_date_map[cha_tl_id] = (from_d, to_d, 0)
                    cha_meta_by_tl_id[cha_tl_id] = {
                        "problem_name": question_label,
                        "triggered_at": cha.triggered_at.isoformat()
                            if hasattr(cha.triggered_at, "isoformat") else None,
                    }
                    active_cha_raw_tl_ids.add(qa_tl.id)

        # ── BL-03 deduplication across CCA + CHA timelines ───────────────────
        # Includes a "context-only" pass: timelines referenced by APPROVED
        # order items that are NOT currently in window. The purchased rule
        # (BL-03) requires the closed governing timeline to be present in
        # dedup input — otherwise a farmer who bought Mancozeb in week 1
        # gets told to buy it again in week 4 when a later timeline also
        # recommends it.
        approved_items_q = await db.execute(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.subscription_id == sub.id,
                OrderItem.status == "APPROVED",
            )
        )
        approved_items = approved_items_q.scalars().all()
        approved_ids: set[str] = {it.practice_id for it in approved_items}

        # ── Orders V2 Batch 11: tappable per-practice fulfilment ──
        # For each practice the farmer sees on the advisory, surface
        # the latest live OrderItem status so the card can render a
        # status badge ("Returned — tap to send elsewhere",
        # "Postponed 3 days", "Ready for approval", etc.). We pick
        # the most recently-touched non-terminal item per practice;
        # REROUTED / REMOVED / archived rows are ignored — they're
        # off the active surface by design.
        # 2026-06-06 — Outer-join PackingList so the fulfilment payload
        # can surface farmer_received_at and the order's packing_code.
        # The advisory then nudges the farmer to confirm pickup at the
        # exact moment they're reading dosage instructions for that
        # item (highest-intent moment for confirmation).
        from app.modules.orders.models import PackingList, BrandLookupCache
        active_items_q = await db.execute(
            select(OrderItem, Order, PackingList)
            .join(Order, Order.id == OrderItem.order_id)
            .outerjoin(PackingList, PackingList.order_id == Order.id)
            .where(
                Order.subscription_id == sub.id,
                Order.status.notin_(["CANCELLED", "EXPIRED"]),
                OrderItem.status.notin_(["REROUTED", "REMOVED"]),
                OrderItem.archived_at.is_(None),
            )
            .order_by(OrderItem.updated_at.desc())
        )
        active_items_rows = active_items_q.all()
        # 2026-06-06 — Manufacturer lookup batched for the advisory
        # render so APPROVED practice cards can display "Brand · by
        # Manufacturer" without per-practice round-trips. Source =
        # BrandLookupCache.trade_name_cosh_id → manufacturer_name
        # (same path the order review + Packing card already use).
        approved_brand_ids = {
            it.brand_cosh_id for it, _, _ in active_items_rows
            if it.brand_cosh_id and (
                it.status.value if hasattr(it.status, "value") else it.status
            ) == "APPROVED"
        }
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
        # Take the first (most recent) row per practice_id.
        fulfilment_by_practice: dict[str, dict] = {}
        for it, ord_row, pl in active_items_rows:
            if it.practice_id in fulfilment_by_practice:
                continue
            status_str = it.status.value if hasattr(it.status, "value") else it.status
            # Brand/volume/price stay hidden until APPROVED — same
            # rule as the farmer order detail; surfacing them
            # earlier would break the dealer-mediation invariant.
            show_money = status_str == "APPROVED"
            days_remaining = None
            if it.postponed_until:
                from datetime import datetime as _dt, timezone as _tz
                delta = it.postponed_until - _dt.now(_tz.utc)
                days_remaining = max(0, delta.days)
            fulfilment_by_practice[it.practice_id] = {
                "status": status_str,
                "order_id": ord_row.id,
                "order_item_id": it.id,
                "order_status": ord_row.status.value if hasattr(ord_row.status, "value") else ord_row.status,
                "dealer_user_id": ord_row.dealer_user_id,
                "facilitator_user_id": ord_row.facilitator_user_id,
                "brand_name": (
                    brand_loc.get(it.brand_cosh_id) or it.brand_name
                    if show_money else None
                ),
                "manufacturer_name": (
                    manufacturer_by_brand.get(it.brand_cosh_id)
                    if (show_money and it.brand_cosh_id) else None
                ),
                "given_volume": float(it.given_volume) if (show_money and it.given_volume) else None,
                "volume_unit": it.volume_unit if show_money else None,
                "price": float(it.price) if (show_money and it.price) else None,
                "postponed_until": it.postponed_until.isoformat() if it.postponed_until else None,
                "postpone_days_remaining": days_remaining,
                # 2026-06-06 — Packing receipt state. The PWA reads
                # farmer_received_at to decide whether to show the
                # "📦 Tap to confirm pickup" hint on this practice row.
                "packing_code": pl.packing_code if pl else None,
                "farmer_received_at": (
                    pl.farmer_received_at.isoformat()
                    if pl and pl.farmer_received_at else None
                ),
            }

        active_tl_ids = {tl.id for tl, _, _ in active_timelines}
        # Combine active CCA + active CHA raw IDs so neither flavour gets
        # re-added by the context-only pass below.
        all_active_raw_tl_ids = active_tl_ids | active_cha_raw_tl_ids
        context_tl_ids: set[str] = {
            it.timeline_id for it in approved_items
            if it.timeline_id and it.timeline_id not in all_active_raw_tl_ids
        }
        context_render_ids: set[str] = set()  # mark for response-build skip

        if context_tl_ids:
            context_tl_rows = (await db.execute(
                select(Timeline).where(Timeline.id.in_(context_tl_ids))
            )).scalars().all()

            # 2026-06-19 — discriminate CCA vs CHA in the context-only
            # pass. Pre-fix this loop assumed every row was CCA and
            # computed dates against crop_start_date — wrong for any
            # CHA timeline whose offsets anchor to triggered_at. The
            # bug made BL-03 step 12 (purchased rule) misfire for
            # approved CHA-recommended inputs. Pre-load triggered_at
            # for the CHA-flavoured rows; the most recent
            # TriggeredCHAEntry on each recommendation wins (matches
            # how the active loop iterates entries with most-recent
            # last).
            cha_recs_needed = {
                rec_id for ctx_tl in context_tl_rows
                for rec_id in (
                    ctx_tl.sp_recommendation_id,
                    ctx_tl.pg_recommendation_id,
                    ctx_tl.standard_response_id,
                )
                if rec_id
            }
            cha_triggered_at_by_rec: dict = {}
            if cha_recs_needed:
                ctx_cha_entries = (await db.execute(
                    select(TriggeredCHAEntry).where(
                        TriggeredCHAEntry.subscription_id == sub.id,
                        TriggeredCHAEntry.recommendation_id.in_(cha_recs_needed),
                    ).order_by(TriggeredCHAEntry.triggered_at.desc())
                )).scalars().all()
                for cce in ctx_cha_entries:
                    if cce.recommendation_id not in cha_triggered_at_by_rec:
                        cha_triggered_at_by_rec[cce.recommendation_id] = (
                            cce.triggered_at.date()
                            if hasattr(cce.triggered_at, "date")
                            else cce.triggered_at
                        )

            for ctx_tl in context_tl_rows:
                if ctx_tl.package_id:
                    # CCA path — existing behaviour.
                    ctx_meta = metadata_from_master_cca(ctx_tl)
                    # Context-only path: timeline is out of window but
                    # referenced by APPROVED items, so the snapshot capture
                    # is PO-driven, not VIEWED. The label is
                    # observability-only; render behaviour is identical.
                    ctx_content, _ = await resolve_cca_content(
                        db, sub.id, ctx_tl.id, lock_trigger="PURCHASE_ORDER",
                    )
                    ctx_rendered = render_cca_from_content(ctx_content, today_answers)
                    ctx_from_d, ctx_to_d = cca_calendar_dates(ctx_meta, crop_start, today)
                    tl_windows.append(TLWindow(
                        id=ctx_tl.id,
                        name=(ctx_content.get("timeline") or {}).get("name") or ctx_tl.name,
                        from_date=ctx_from_d, to_date=ctx_to_d,
                        created_at=(
                            ctx_tl.created_at.date()
                            if hasattr(ctx_tl.created_at, "date") else today
                        ),
                        practices=ctx_rendered.practice_stubs, source="CCA",
                        lineage_id=ctx_tl.lineage_id,
                    ))
                    context_render_ids.add(ctx_tl.id)
                else:
                    # CHA path — anchor to triggered_at, use synthetic ID
                    # so the entry can't collide with any active CHA
                    # already in tl_windows (excluded above) or a
                    # context CCA.
                    rec_id = (
                        ctx_tl.sp_recommendation_id
                        or ctx_tl.pg_recommendation_id
                        or ctx_tl.standard_response_id
                    )
                    triggered_d = cha_triggered_at_by_rec.get(rec_id)
                    if triggered_d is None:
                        # No matching TriggeredCHAEntry — can't compute
                        # window. Skip; purchased-rule won't fire for this
                        # orphan, but the data isn't present to fire on
                        # either.
                        continue
                    if ctx_tl.from_value is None or ctx_tl.to_value is None:
                        continue
                    if ctx_tl.sp_recommendation_id:
                        cha_source = "SP"
                        cha_tl_id_synth = f"cha-sp-{ctx_tl.id}"
                    elif ctx_tl.pg_recommendation_id:
                        cha_source = "PG"
                        cha_tl_id_synth = f"cha-pg-{ctx_tl.id}"
                    else:
                        cha_source = "QA"
                        cha_tl_id_synth = f"cha-qa-{ctx_tl.id}"
                    ctx_content, _ = await resolve_cha_content(
                        db, sub.id, ctx_tl.id, cha_source,
                        lock_trigger="PURCHASE_ORDER",
                    )
                    ctx_stubs = render_cha_from_content(ctx_content)
                    ctx_meta_inner = metadata_from_content({"timeline": {
                        "from_type": "DAS",
                        "from_value": int(ctx_tl.from_value),
                        "to_value": int(ctx_tl.to_value),
                    }})
                    ctx_from_d, ctx_to_d = cha_calendar_dates(
                        ctx_meta_inner, triggered_d,
                    )
                    tl_windows.append(TLWindow(
                        id=cha_tl_id_synth,
                        name=ctx_tl.name,
                        from_date=ctx_from_d, to_date=ctx_to_d,
                        created_at=triggered_d,
                        practices=ctx_stubs,
                        source="CHA" if cha_source != "QA" else "QA",
                        lineage_id=(
                            f"cha-sp-{ctx_tl.lineage_id}" if ctx_tl.sp_recommendation_id
                            else f"cha-pg-{ctx_tl.lineage_id}" if ctx_tl.pg_recommendation_id
                            else f"cha-qa-{ctx_tl.lineage_id}"
                        ),
                    ))
                    context_render_ids.add(cha_tl_id_synth)

        deduped = deduplicate_advisory(tl_windows, approved_practice_ids=approved_ids)

        # ── Resolve cosh_ref / unit_cosh_id UUIDs → friendly names. ──────────
        # Elements that point at a Cosh Core (COMMON_NAME, APPLICATION_METHOD,
        # ITK_NAME, *_UNIT, …) store the selection as a Cosh UUID in either
        # `cosh_ref` or `unit_cosh_id`. The PWA can't display UUIDs, so we
        # batch-resolve them here against `cosh_core_items.translations.en`.
        # One-shot lookup across every dedup-surviving practice keeps the
        # query cost flat regardless of advisory size.
        ref_ids: set[str] = set()
        for dedup_tl in deduped:
            for p in dedup_tl.visible_practices:
                for el in p.elements:
                    if el.cosh_ref and _is_uuid(el.cosh_ref):
                        ref_ids.add(el.cosh_ref)
                    if el.unit_cosh_id and _is_uuid(el.unit_cosh_id):
                        ref_ids.add(el.unit_cosh_id)
        name_by_cosh_id: dict[str, str] = {}
        if ref_ids:
            from app.modules.sync.models import CoshCoreItem
            for cosh_id, translations in (await db.execute(
                select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
                .where(CoshCoreItem.cosh_id.in_(ref_ids))
            )).all():
                if isinstance(translations, dict):
                    label = pick_translation(translations, lang, "")
                    if label:
                        name_by_cosh_id[cosh_id] = label

        def _resolve(uuid_or_label: str | None) -> str | None:
            if not uuid_or_label:
                return uuid_or_label
            return name_by_cosh_id.get(uuid_or_label, uuid_or_label)

        # ── Build response ────────────────────────────────────────────────────
        timeline_data = []
        for dedup_tl in deduped:
            tl = dedup_tl.timeline
            # Context-only timelines exist purely to inform BL-03's
            # purchased rule — they are NOT in tl_date_map and must not
            # render to the farmer.
            if tl.id in context_render_ids:
                continue
            from_d, to_d, day_num = tl_date_map[tl.id]
            # Frequency filter: hide frequency-based practices that aren't due today.
            # Non-frequency practices (frequency_days NULL) are always shown if in window.
            freq_filtered_practices = [
                p for p in dedup_tl.visible_practices
                if _is_frequency_due_today(p.frequency_days, from_d, today)
            ]
            # 2026-06-19 — Per-practice ack lookup. occurrence_date is
            # the timeline's from_d for non-frequency (sticky across the
            # whole window) or today for frequency (each due day is its
            # own ack). Hidden practices drop out entirely here.
            tl_practices_out: list[dict] = []
            for p in freq_filtered_practices:
                occ_date = today if (p.frequency_days and p.frequency_days >= 2) else from_d
                ack = ack_by_key.get((tl.lineage_id, p.id, occ_date))
                if ack and ack.hidden_at is not None:
                    continue  # farmer "deleted" — invisible to them
                ack_status = "MARKED" if (ack and ack.marked_at is not None) else "ACTIVE"
                tl_practices_out.append({
                    "id": p.id, "l0_type": p.l0_type,
                    "l1_type": p.l1_type, "l2_type": p.l2_type,
                    "l2_name_loc": l2_name_loc.get(p.l2_type or "") or None,
                    "display_order": p.display_order, "is_special_input": p.is_special_input,
                    "relation_id": p.relation_id,
                    "relation_role": p.relation_role,
                    "relation_type": p.relation_type,
                    "frequency_days": p.frequency_days,
                    "is_frequency_due_today": True,
                    "is_purchased": p.id in approved_ids,
                    "fulfilment": fulfilment_by_practice.get(p.id),
                    "elements": [{
                        "element_type": el.element_type,
                        "cosh_ref": _resolve(el.cosh_ref),
                        "value": el.value,
                        "unit_cosh_id": _resolve(el.unit_cosh_id),
                    } for el in p.elements],
                    # Practice ack state + the occurrence_date the PWA
                    # must echo back on mark/unmark/hide calls.
                    "ack_status": ack_status,
                    "occurrence_date": occ_date.isoformat(),
                })
            tl_entry: dict = {
                "id": tl.id,
                # 2026-06-19 — Stable lineage id for the ack key.
                "lineage_id": tl.lineage_id,
                "name": tl.name,
                "source": tl.source,  # CCA | CHA
                "from_date": from_d.isoformat(),
                "to_date": to_d.isoformat(),
                "day_number": day_num,
                "suppressed_count": len(dedup_tl.suppressed),
                "practices": tl_practices_out,
            }
            # Include BL-02 pending question for this timeline (if any)
            if tl.id in pending_questions_by_tl:
                tl_entry["pending_conditional_question"] = pending_questions_by_tl[tl.id]
                tl_entry["has_pending_question"] = True
            # Per spec §6.4: blank-path questions for this timeline (named, with farmer's answer)
            if tl.id in blank_paths_by_tl:
                tl_entry["blank_path_questions"] = blank_paths_by_tl[tl.id]
            # CHA / QA metadata so the PWA can show "FruitFly" alongside
            # the date band and sort fresh diagnoses to the top of the
            # list. Only set on CHA/QA-source timelines; CCA gets nothing.
            cha_meta = cha_meta_by_tl_id.get(tl.id)
            if cha_meta:
                tl_entry["problem_name"] = cha_meta["problem_name"]
                tl_entry["triggered_at"] = cha_meta["triggered_at"]
            timeline_data.append(tl_entry)

        pkg_type_val = (
            pkg.package_type.value if hasattr(pkg.package_type, "value")
            else (str(pkg.package_type) if pkg.package_type else None)
        )
        out.append({
            "subscription_id": sub.id,
            "client_id": sub.client_id,
            "package_id": sub.package_id,
            "package_name": pkg.name,
            "package_type": pkg_type_val,
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
    """Self-subscribed: cancel freely (ACTIVE or pending-payment
    WAITLISTED). Company-assigned: returns 400 (request required).

    2026-05-20: widened to include WAITLISTED so a farmer can
    cancel a self-initiated payment-pending subscription from the
    Home pending-payment card. The status filter on the lookup
    accepts both ACTIVE and WAITLISTED; the SELF-vs-ASSIGNED
    policy in is_self_unsubscribable still rejects assigned rows.
    """
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
            Subscription.status.in_([
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.WAITLISTED,
            ]),
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found or already cancelled")

    sub_type = sub.subscription_type.value if hasattr(sub.subscription_type, "value") else str(sub.subscription_type)
    if not is_self_unsubscribable(sub_type, sub.status):
        raise HTTPException(
            status_code=400,
            detail="Company-assigned subscriptions cannot be cancelled by the farmer. Please contact your company.",
        )

    # Cancel-and-route rule (2026-05-29): if a payment request is
    # currently PENDING with someone else, the farmer must cancel
    # that first. Prevents the awkward state where the subscription
    # is cancelled while a delegate is mid-Razorpay flow.
    pending_pay = (await db.execute(
        select(SubscriptionPaymentRequest).where(
            SubscriptionPaymentRequest.subscription_id == sub.id,
            SubscriptionPaymentRequest.status == "PENDING",
        )
    )).scalar_one_or_none()
    if pending_pay:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pending_payment_blocks_unsubscribe",
                "message": "A payment request is currently pending. Cancel that first, then cancel the subscription.",
            },
        )

    was_waitlisted = sub.status == SubscriptionStatus.WAITLISTED
    sub.status = SubscriptionStatus.CANCELLED
    await db.commit()
    return {
        "detail": ("Pending payment cancelled" if was_waitlisted else "Unsubscribed successfully"),
        "status": sub.status,
    }


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
    variety_id: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns up to 5 nearest dealers + Promoter pinned first.
    order_type: PESTICIDE | FERTILISER | SEED — filters by sell_categories.

    `variety_id` is the seed-order brand-lock hook (Point 3a, 2026-06-18).
    When supplied, the picker drops any dealer not onboarded by the
    variety's owning client. Seed varieties are always brand-locked, so
    the PWA's seed-order picker must always pass this. Pesticide /
    fertiliser callers leave it null and use the existing brand-lock
    machinery on `/eligible-recipients-for-new-order`.
    """
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # Resolve the variety's client_id for the brand-lock filter. We
    # narrow on `variety.client_id`, not `sub.client_id`, because the
    # subscription's package can carry varieties from multiple
    # seed-company clients via VarietyPoP.
    brand_lock_client_id: Optional[str] = None
    if variety_id:
        from app.modules.seed_mgmt.models import SeedVariety as _Var
        var_row = (await db.execute(
            select(_Var.client_id).where(_Var.id == variety_id)
        )).scalar_one_or_none()
        if var_row is None:
            raise HTTPException(status_code=404, detail="Variety not found")
        brand_lock_client_id = var_row

    farmer_lat = lat or (float(current_user.gps_lat) if current_user.gps_lat else 0.0)
    farmer_lng = lng or (float(current_user.gps_lng) if current_user.gps_lng else 0.0)

    promoter_user_id = await _get_promoter(db, subscription_id, PromoterType.DEALER)

    # Accept both spellings (FERTILISER, FERTILIZER) — the rest of the
    # platform is mixed and the Orders V2 redesign standardises on
    # FERTILIZER, but legacy callers still send FERTILISER.
    category_map = {
        "PESTICIDE": "PESTICIDES",
        "FERTILISER": "FERTILISERS",
        "FERTILIZER": "FERTILISERS",
        "SEED": "SEEDS",
    }
    required_cat = category_map.get((order_type or "").upper()) if order_type else None

    # Pre-compute the onboarded-dealer allow-list for the brand-lock
    # case. Cheaper than calling `_is_dealer_onboarded_by_client` per
    # dealer in the loop below (which would issue one query per row).
    onboarded_dealer_ids: Optional[set[str]] = None
    if brand_lock_client_id:
        from app.modules.clients.models import ClientPromoter
        onboarded_rows = (await db.execute(
            select(ClientPromoter.user_id).where(
                ClientPromoter.client_id == brand_lock_client_id,
                ClientPromoter.promoter_type == "DEALER",
                ClientPromoter.status == "ACTIVE",
            )
        )).scalars().all()
        onboarded_dealer_ids = set(onboarded_rows)

    profiles = (await db.execute(select(DealerProfile))).scalars().all()
    results = []
    for profile in profiles:
        if required_cat and required_cat not in (profile.sell_categories or []):
            continue
        if onboarded_dealer_ids is not None and profile.user_id not in onboarded_dealer_ids:
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
    lang = current_user.language_code or "en"
    l2_name_loc = await _l2_name_loc_map(db, lang)

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
                        "l2_name_loc": l2_name_loc.get(p.l2_type or "") or None,
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
    lang = current_user.language_code or "en"
    l2_name_loc = await _l2_name_loc_map(db, lang)

    missed = []
    for tl in timelines:
        is_missed = False
        window_end = None
        if tl.from_type.value == "DAS":
            if day_offset > tl.to_value:
                is_missed = True
                window_end = crop_start + timedelta(days=tl.to_value)
        elif tl.from_type.value == "DBS":
            # BL-17: DBS to=0 closes day BEFORE sowing — clamp upper bound.
            _tl_to = max(tl.to_value, 1)
            if day_offset > -_tl_to:
                is_missed = True
                window_end = crop_start - timedelta(days=_tl_to)

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
                            "l2_name_loc": l2_name_loc.get(p.l2_type or "") or None,
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
    # PP V1 (2026-05-30): silent stale-override hiding. If the saved
    # preference no longer resolves to an ACTIVE Promoter-Pundit at
    # this Client (CA revoked PP, or F-P stepped down, or invitation
    # was withdrawn), the slot appears empty to the farmer and the
    # subscription quietly falls through to the pool. The farmer can
    # always type a new number. Re-typing the now-stale number returns
    # the 422 not_a_promoter_pundit message from set_pundit_preference.
    pref = (await db.execute(
        select(FarmPunditPreference).where(FarmPunditPreference.subscription_id == subscription_id)
    )).scalar_one_or_none()

    preferred_pundit = None
    if pref:
        # Eligibility gate — same shape as set_pundit_preference.
        from app.modules.farmpundit.models import ClientFarmPundit as _CFP
        from app.modules.clients.models import ClientPromoter as _CP
        eligible = (await db.execute(
            select(_CFP)
            .join(FarmPunditProfile, FarmPunditProfile.id == _CFP.pundit_id)
            .where(
                _CFP.pundit_id == pref.pundit_id,
                _CFP.client_id == sub.client_id,
                _CFP.is_promoter_pundit == True,  # noqa: E712
                _CFP.status == "ACTIVE",
            )
        )).scalar_one_or_none()
        if eligible is not None:
            row = (await db.execute(
                select(FarmPunditProfile, User)
                .join(User, User.id == FarmPunditProfile.user_id)
                .where(FarmPunditProfile.id == pref.pundit_id)
            )).first()
            if row:
                profile, user_obj = row
                # Also gate on the upstream ClientPromoter binding —
                # if the F-P stepped down / was deactivated, the
                # ClientFarmPundit row may still exist but the
                # eligibility is gone.
                fp_still_active = (await db.execute(
                    select(_CP).where(
                        _CP.client_id == sub.client_id,
                        _CP.user_id == user_obj.id,
                        _CP.promoter_type == "FACILITATOR",
                        _CP.is_promoter == True,  # noqa: E712
                        _CP.status == "ACTIVE",
                    )
                )).scalar_one_or_none() is not None
                if fp_still_active:
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
    # PP V1 (2026-05-30): phantom-pundit rows (searchable=False) are
    # excluded so the farmer never sees a Promoter-Pundit in the
    # picker. The only way to reach a P-P is by typing the phone
    # directly into the expert field (validated in set_pundit_preference).
    company_experts = []
    try:
        rows = (await db.execute(
            select(ClientFarmPundit, FarmPunditProfile, User)
            .join(FarmPunditProfile, FarmPunditProfile.id == ClientFarmPundit.pundit_id)
            .join(User, User.id == FarmPunditProfile.user_id)
            .where(
                ClientFarmPundit.client_id == sub.client_id,
                ClientFarmPundit.status == "ACTIVE",
                ClientFarmPundit.searchable.is_(True),
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

async def _assert_area_wise_or_untyped(db: AsyncSession, sub: Subscription) -> None:
    """Refuse the call when the subscription's crop is PLANT_WISE.

    Lenient direction (2026-05-27): we don't auto-null any
    farm_area_acres value that may already exist on a plant-wise
    subscription (legacy / Cosh-reclassified), but a fresh write to
    the area-wise endpoints is refused so we don't compound the
    drift. Untyped crops fall back to area-wise so the write goes
    through.
    """
    from app.services.cosh_crop_view import get_measure_for_biological_name
    from app.modules.advisory.models import Package as _Pkg
    pkg = (await db.execute(
        select(_Pkg).where(_Pkg.id == sub.package_id)
    )).scalar_one_or_none()
    if pkg is None or not pkg.crop_cosh_id:
        return  # No crop to look up; permit the write.
    measure = await get_measure_for_biological_name(db, pkg.crop_cosh_id)
    if measure == "PLANT_WISE":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "wrong_measure_for_area_endpoint",
                "message": (
                    "This crop is plant-wise. Use the plant-count "
                    "endpoints instead of the farm-area endpoints."
                ),
            },
        )


async def _assert_plant_wise_or_untyped(db: AsyncSession, sub: Subscription) -> None:
    """Refuse the call when the subscription's crop is AREA_WISE.
    Mirror of `_assert_area_wise_or_untyped` for the plant-count
    endpoints. Untyped crops are tolerated (default area-wise) — a
    plant-count write on an untyped crop is permitted but unusual."""
    from app.services.cosh_crop_view import get_measure_for_biological_name
    from app.modules.advisory.models import Package as _Pkg
    pkg = (await db.execute(
        select(_Pkg).where(_Pkg.id == sub.package_id)
    )).scalar_one_or_none()
    if pkg is None or not pkg.crop_cosh_id:
        return
    measure = await get_measure_for_biological_name(db, pkg.crop_cosh_id)
    if measure == "AREA_WISE":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "wrong_measure_for_plant_endpoint",
                "message": (
                    "This crop is area-wise. Use the farm-area "
                    "endpoints instead of the plant-count endpoints."
                ),
            },
        )


@router.put("/farmer/subscriptions/{subscription_id}/farm-area")
async def update_farm_area(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update tentative or soft-confirmed farm area. Rejected if hard-locked."""
    sub = await _get_subscription(db, subscription_id, current_user.id)
    await _assert_area_wise_or_untyped(db, sub)
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


# ── Farmer: Plant count + planting year (plant-wise crops) ───────────────────

@router.put("/farmer/subscriptions/{subscription_id}/plant-count")
async def update_plant_count(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update tentative or soft-confirmed plant count + planting year.

    Plant-wise mirror of /farm-area. Refuses 400 if locked
    (`plant_count_confirmed_at`). At least one of (number_of_plants,
    planting_year) must be present in the payload.
    """
    sub = await _get_subscription(db, subscription_id, current_user.id)
    await _assert_plant_wise_or_untyped(db, sub)
    if sub.plant_count_confirmed_at:
        raise HTTPException(status_code=400, detail="Plant count is locked and cannot be changed")
    n = data.get("number_of_plants")
    y = data.get("planting_year")
    if n is None and y is None:
        raise HTTPException(
            status_code=422,
            detail="At least one of number_of_plants / planting_year required",
        )
    if n is not None:
        if not isinstance(n, int) or n <= 0:
            raise HTTPException(status_code=422, detail="number_of_plants must be a positive integer")
        sub.number_of_plants = n
    if y is not None:
        if not isinstance(y, int) or y < 1900 or y > 2100:
            raise HTTPException(status_code=422, detail="planting_year must be a year between 1900 and 2100")
        sub.planting_year = y
    await db.commit()
    return {
        "number_of_plants": sub.number_of_plants,
        "planting_year": sub.planting_year,
        "plant_count_confirmed_at": sub.plant_count_confirmed_at,
    }


@router.post("/farmer/subscriptions/{subscription_id}/plant-count/confirm")
async def confirm_plant_count(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hard-lock the plant count + planting year. Mirror of
    /farm-area/confirm. Both fields must be set before confirmation."""
    sub = await _get_subscription(db, subscription_id, current_user.id)
    await _assert_plant_wise_or_untyped(db, sub)
    if data.get("number_of_plants") is not None:
        sub.number_of_plants = data["number_of_plants"]
    if data.get("planting_year") is not None:
        sub.planting_year = data["planting_year"]
    if not sub.number_of_plants or not sub.planting_year:
        raise HTTPException(
            status_code=422,
            detail="Both number_of_plants and planting_year are required to confirm",
        )
    sub.plant_count_confirmed_at = datetime.now(timezone.utc)
    await db.commit()
    return {
        "number_of_plants": sub.number_of_plants,
        "planting_year": sub.planting_year,
        "confirmed_at": sub.plant_count_confirmed_at,
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

    # 2026-06-19 — User reported the DBS path was creating SENT
    # orders with both dealer_user_id and facilitator_user_id NULL
    # (the PWA's order sheet never collected a recipient before
    # POST). The order went "into thin air". Plus reference_number
    # was never generated, so the Manage tab fell back to the raw
    # order UUID.
    #
    # Fix: land as DRAFT with a generated reference_number. The
    # farmer is then routed to `/orders/[id]` where the existing
    # DRAFT picker (brand-lock-aware via
    # `/eligible-recipients`) handles dealer / facilitator
    # selection.
    from app.modules.orders.router import _generate_order_reference
    reference_number = await _generate_order_reference(db)
    order = Order(
        subscription_id=subscription_id,
        farmer_user_id=current_user.id,
        client_id=sub.client_id,
        # No recipient set at create — farmer picks via the DRAFT
        # picker on the next screen.
        dealer_user_id=None,
        facilitator_user_id=None,
        date_from=date_from,
        date_to=date_to,
        category=category,
        status=OrderStatus.DRAFT,
        reference_number=reference_number,
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


@router.get("/admin/bl01/config-errors")
async def admin_list_bl01_config_errors(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA debug — most recent BL-01 (PoP guided elimination) config errors.

    Each row is one farmer hit on a configuration that doesn't resolve to
    any package OR resolves to multiple packages with no remaining
    parameter to ask. Investigate by checking the (client_id,
    crop_cosh_id, district_cosh_id) combination — usually means the SE
    is missing a PackageLocation, a PackageVariable, or has duplicate
    package fingerprints.
    """
    _require_sa_for_snapshots(current_user)

    from app.modules.subscriptions.config_error_models import DataConfigError
    rows = (await db.execute(
        select(DataConfigError)
        .where(DataConfigError.algorithm == "BL-01")
        .order_by(DataConfigError.occurred_at.desc())
        .limit(max(1, min(limit, 500)))
    )).scalars().all()

    return [
        {
            "id": r.id,
            "client_id": r.client_id,
            "crop_cosh_id": r.crop_cosh_id,
            "district_cosh_id": r.district_cosh_id,
            "answers_state": r.answers_state,
            "details": r.details,
            "observed_by_user_id": r.observed_by_user_id,
            "occurred_at": r.occurred_at,
        }
        for r in rows
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
