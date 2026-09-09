"""Farmer Ledger — dealer-facing farmer roster + purchase history.

All endpoints require the caller to hold an ACTIVE `DEALER` role.
Every query is scoped to `dealer_user_id = current_user.id` — a dealer
sees only his own farmers, his own sales, and his own notes. Anonymised
"other-shop" rows on the per-farmer detail expose brand + manufacturer +
qty only; never dealer identity or price.

See project_rootstalk_dealer_farmer_ledger for the design.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_roles
from app.modules.advisory.models import Package, Practice
from app.modules.clients.models import Client
from app.modules.coaching.service import get_coaching_student_for_user
from app.modules.ledger.models import DealerFarmerNote, DealerManualSale, new_uuid
from app.modules.ledger.schemas import (
    FarmerDetail, FarmerInfoUpdateRequest, LedgerEntry, ManualSaleCreateRequest,
    ManualSaleResponse, ManualSaleUpdateRequest, NewFarmerFields, NoteUpdateRequest,
    PhoneLookupRequest, PhoneLookupResponse, RosterFarmer, RosterResponse,
)
from app.modules.orders.models import Order, OrderItem, PackingList, SeedOrder
from app.modules.platform.models import RoleType, StatusEnum, User, UserRole
from app.modules.subscriptions.models import Subscription, SubscriptionStatus
from app.modules.sync.models import CoshCoreItem


router = APIRouter(prefix="/dealer/ledger", tags=["Farmer Ledger"])

require_dealer = require_roles(RoleType.DEALER)


# ── Sub-status filter mapping ─────────────────────────────────────

_ACTIVE_STATUSES = {
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.WAITLISTED,
    SubscriptionStatus.SUSPENDED,
}
_COMPLETED_STATUSES = {
    SubscriptionStatus.LAPSED,
    SubscriptionStatus.UNSUBSCRIBED,
    SubscriptionStatus.CANCELLED,
}


def _sub_statuses_for_filter(filter_: str) -> Optional[set[SubscriptionStatus]]:
    if filter_ == "active":
        return _ACTIVE_STATUSES
    if filter_ == "completed":
        return _COMPLETED_STATUSES
    return None  # "all" — no restriction


# ── Phone normalisation ───────────────────────────────────────────

def _normalise_phone(raw: str) -> str:
    """Match `coaching.service.normalise_phone` — strip non-digits,
    take the last 10, prefix +91."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not digits:
        raise HTTPException(status_code=422, detail={"code": "invalid_phone", "message": "Enter a valid phone number."})
    last10 = digits[-10:]
    if len(last10) != 10:
        raise HTTPException(status_code=422, detail={"code": "invalid_phone", "message": "Enter a 10-digit phone number."})
    return f"+91{last10}"


# ── Cosh name lookup ──────────────────────────────────────────────

async def _resolve_cosh_names(db: AsyncSession, cosh_ids: set[str], lang: str = "en") -> dict[str, str]:
    """Return {cosh_id: english_name} for the given ids. Falls back to
    the cosh_id itself if the translation is missing."""
    clean = {c for c in cosh_ids if c}
    if not clean:
        return {}
    rows = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
        .where(CoshCoreItem.cosh_id.in_(clean))
    )).all()
    out: dict[str, str] = {}
    for cosh_id, translations in rows:
        if isinstance(translations, dict):
            out[cosh_id] = translations.get(lang) or translations.get("en") or cosh_id
        else:
            out[cosh_id] = cosh_id
    for cid in clean:
        out.setdefault(cid, cid)
    return out


def _l1_to_category(l1_type: Optional[str]) -> str:
    """Bucket Practice.l1_type into the ledger's 3-way category.
    Anything unrecognised passes through so the frontend can display
    a fallback label."""
    if not l1_type:
        return "OTHER"
    up = l1_type.upper()
    if "SEED" in up:
        return "SEED"
    if "PEST" in up or "INSECT" in up or "FUNG" in up or "HERB" in up or "WEED" in up:
        return "PESTICIDE"
    if "FERT" in up or "NUTR" in up or "MICRO" in up:
        return "FERTILIZER"
    return up


# ── Helper: verify dealer has some relationship with a farmer ─────

async def _dealer_has_farmer_in_roster(db: AsyncSession, dealer_id: str, farmer_id: str) -> bool:
    """True if the dealer has either a picked-up PackingList or a
    manual sale for this farmer. Used to gate farmer-info edits +
    per-farmer detail reads."""
    q1 = select(func.count()).select_from(Order).join(
        PackingList, PackingList.order_id == Order.id,
    ).where(
        Order.dealer_user_id == dealer_id,
        Order.farmer_user_id == farmer_id,
        PackingList.picked_up_at.is_not(None),
    )
    if (await db.scalar(q1)) or 0:
        return True
    q2 = select(func.count()).select_from(DealerManualSale).where(
        DealerManualSale.dealer_user_id == dealer_id,
        DealerManualSale.farmer_user_id == farmer_id,
    )
    return bool((await db.scalar(q2)) or 0)


# ── GET /dealer/ledger/farmers — roster ───────────────────────────

@router.get("/farmers", response_model=RosterResponse)
async def get_roster(
    sort: Literal["recency", "name", "phone"] = "recency",
    q: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    dealer_id = current_user.id
    today = date.today()
    three_months_ago = today - timedelta(days=90)
    twelve_months_ago = today - timedelta(days=365)

    # Own picked-up sales — last-purchase per farmer.
    own_last_rows = (await db.execute(
        select(
            Order.farmer_user_id.label("farmer_id"),
            func.max(PackingList.picked_up_at).label("last_ts"),
        ).select_from(Order).join(
            PackingList, PackingList.order_id == Order.id,
        ).where(
            Order.dealer_user_id == dealer_id,
            PackingList.picked_up_at.is_not(None),
        ).group_by(Order.farmer_user_id)
    )).all()

    # Recent-3-month counts per farmer.
    own_recent_rows = (await db.execute(
        select(Order.farmer_user_id, func.count())
        .select_from(Order).join(PackingList, PackingList.order_id == Order.id)
        .where(
            Order.dealer_user_id == dealer_id,
            PackingList.picked_up_at.is_not(None),
            PackingList.picked_up_at >= datetime.combine(three_months_ago, datetime.min.time(), tzinfo=timezone.utc),
        )
        .group_by(Order.farmer_user_id)
    )).all()
    own_recent_map = {fid: cnt for fid, cnt in own_recent_rows}

    own_last_map: dict[str, datetime] = {}
    for row in own_last_rows:
        if row.last_ts:
            own_last_map[row.farmer_id] = row.last_ts

    # Manual sales grouped by farmer.
    manual_rows = (await db.execute(
        select(
            DealerManualSale.farmer_user_id,
            func.max(DealerManualSale.sale_date),
        ).where(
            DealerManualSale.dealer_user_id == dealer_id,
        ).group_by(DealerManualSale.farmer_user_id)
    )).all()
    manual_last_map = {fid: d for fid, d in manual_rows if d}

    manual_recent_rows = (await db.execute(
        select(DealerManualSale.farmer_user_id, func.count())
        .where(
            DealerManualSale.dealer_user_id == dealer_id,
            DealerManualSale.sale_date >= three_months_ago,
        ).group_by(DealerManualSale.farmer_user_id)
    )).all()
    manual_recent_map = {fid: cnt for fid, cnt in manual_recent_rows}

    farmer_ids = set(own_last_map.keys()) | set(manual_last_map.keys())
    if not farmer_ids:
        return RosterResponse(farmers=[], total_count=0)

    # Coaching-sandbox isolation: strip any coaching student out of
    # the roster before hydrating User rows. Guards against the case
    # where a dealer somehow accumulated a picked-up PL / manual
    # entry against a coaching-student user_id (shouldn't happen in
    # normal flow, but defensive here so the roster never leaks a
    # coaching identity into a real dealer's world).
    from app.modules.coaching.models import CoachingStudent  # local to avoid cycles
    coaching_ids = set((await db.execute(
        select(CoachingStudent.user_id).where(CoachingStudent.user_id.in_(farmer_ids))
    )).scalars().all())
    farmer_ids -= coaching_ids
    if not farmer_ids:
        return RosterResponse(farmers=[], total_count=0)

    # Hydrate user rows.
    users = (await db.execute(
        select(User).where(User.id.in_(farmer_ids))
    )).scalars().all()

    # Resolve state + district names.
    cosh_ids = set()
    for u in users:
        if u.state_cosh_id:
            cosh_ids.add(u.state_cosh_id)
        if u.district_cosh_id:
            cosh_ids.add(u.district_cosh_id)
    names = await _resolve_cosh_names(db, cosh_ids, current_user.language_code or "en")

    out: list[RosterFarmer] = []
    for u in users:
        own_last = own_last_map.get(u.id)
        manual_last = manual_last_map.get(u.id)
        last_date: Optional[date] = None
        if own_last and manual_last:
            last_date = max(own_last.date(), manual_last)
        elif own_last:
            last_date = own_last.date()
        elif manual_last:
            last_date = manual_last

        recent = (own_recent_map.get(u.id) or 0) + (manual_recent_map.get(u.id) or 0)
        no_purchase_12m = bool(last_date and last_date < twelve_months_ago)

        out.append(RosterFarmer(
            user_id=u.id,
            name=u.name,
            phone=u.phone,
            photo_url=u.photo_url,
            state_cosh_id=u.state_cosh_id,
            district_cosh_id=u.district_cosh_id,
            sub_district=u.sub_district_cosh_id,
            state_name=names.get(u.state_cosh_id) if u.state_cosh_id else None,
            district_name=names.get(u.district_cosh_id) if u.district_cosh_id else None,
            last_purchase_date=last_date,
            recent_purchase_count=recent,
            no_purchases_in_12_months=no_purchase_12m,
        ))

    # Optional server-side search (name / phone contains).
    if q:
        needle = q.strip().lower()
        out = [
            f for f in out
            if (f.name and needle in f.name.lower())
            or (f.phone and needle in f.phone.lower())
        ]

    # Sort — recency prefers rows with a last_purchase_date, then desc.
    if sort == "name":
        out.sort(key=lambda f: (f.name or "").lower())
    elif sort == "phone":
        out.sort(key=lambda f: f.phone or "")
    else:
        out.sort(key=lambda f: (f.last_purchase_date or date.min), reverse=True)

    return RosterResponse(farmers=out, total_count=len(out))


# ── GET /dealer/ledger/farmers/{user_id} — per-farmer detail ──────

@router.get("/farmers/{user_id}", response_model=FarmerDetail)
async def get_farmer_detail(
    user_id: str,
    filter: Literal["active", "completed", "all"] = "active",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    dealer_id = current_user.id

    if not await _dealer_has_farmer_in_roster(db, dealer_id, user_id):
        raise HTTPException(status_code=404, detail={"code": "farmer_not_in_roster", "message": "This farmer is not in your ledger."})

    # Coaching-sandbox isolation — never expose a coaching student's
    # profile / history via the real dealer ledger.
    if await get_coaching_student_for_user(db, user_id) is not None:
        raise HTTPException(status_code=404, detail={"code": "farmer_not_in_roster"})

    farmer = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not farmer:
        raise HTTPException(status_code=404, detail={"code": "farmer_not_found"})

    # Resolve cosh names for state + district.
    cosh_ids = {c for c in (farmer.state_cosh_id, farmer.district_cosh_id) if c}
    names = await _resolve_cosh_names(db, cosh_ids, current_user.language_code or "en")

    # Personal note (dealer-private).
    note_row = (await db.execute(
        select(DealerFarmerNote).where(
            DealerFarmerNote.dealer_user_id == dealer_id,
            DealerFarmerNote.farmer_user_id == user_id,
        )
    )).scalar_one_or_none()

    # 12-month cutoff for the ledger detail.
    today = date.today()
    twelve_months_ago = today - timedelta(days=365)
    cutoff_ts = datetime.combine(twelve_months_ago, datetime.min.time(), tzinfo=timezone.utc)

    # Which subs are in-scope for the filter?
    sub_status_filter = _sub_statuses_for_filter(filter)

    # Grab all farmer's subs; we'll filter per row after fetching context.
    subs_q = select(Subscription).where(Subscription.farmer_user_id == user_id)
    if sub_status_filter is not None:
        subs_q = subs_q.where(Subscription.status.in_(sub_status_filter))
    subs = (await db.execute(subs_q)).scalars().all()
    sub_by_id = {s.id: s for s in subs}
    in_scope_sub_ids = set(sub_by_id.keys())

    # Package lookup for crop + advising company.
    pkg_ids = {s.package_id for s in subs if s.package_id}
    pkgs = (await db.execute(select(Package).where(Package.id.in_(pkg_ids)))).scalars().all() if pkg_ids else []
    pkg_by_id = {p.id: p for p in pkgs}

    client_ids = {p.client_id for p in pkgs if p.client_id}
    clients = (await db.execute(select(Client).where(Client.id.in_(client_ids)))).scalars().all() if client_ids else []
    client_by_id = {c.id: c for c in clients}

    # Crop name via cosh translations.
    crop_cosh_ids = {p.crop_cosh_id for p in pkgs if p.crop_cosh_id}
    crop_names = await _resolve_cosh_names(db, crop_cosh_ids, current_user.language_code or "en")

    def _sub_context(sub_id: Optional[str]) -> dict:
        if not sub_id:
            return {}
        sub = sub_by_id.get(sub_id)
        if not sub:
            return {}
        pkg = pkg_by_id.get(sub.package_id) if sub.package_id else None
        client = client_by_id.get(pkg.client_id) if pkg and pkg.client_id else None
        crop_name = crop_names.get(pkg.crop_cosh_id) if pkg and pkg.crop_cosh_id else None
        start_date = None
        if sub.crop_start_date:
            start_date = sub.crop_start_date.date() if isinstance(sub.crop_start_date, datetime) else sub.crop_start_date
        return {
            "subscription_id": sub.id,
            "package_id": pkg.id if pkg else None,
            "crop_name": crop_name,
            "crop_start_date": start_date,
            "advising_company": (client.display_name or client.full_name) if client else None,
        }

    entries: list[LedgerEntry] = []

    # ── Own-shop RT-mediated sales ─────────────────────────────
    own_rows = (await db.execute(
        select(
            PackingList.picked_up_at,
            Order.subscription_id,
            OrderItem.brand_name,
            OrderItem.given_volume,
            OrderItem.volume_unit,
            OrderItem.price,
            OrderItem.practice_id,
        ).select_from(PackingList).join(
            Order, Order.id == PackingList.order_id,
        ).join(
            OrderItem, OrderItem.order_id == Order.id,
        ).where(
            Order.dealer_user_id == dealer_id,
            Order.farmer_user_id == user_id,
            PackingList.picked_up_at.is_not(None),
            PackingList.picked_up_at >= cutoff_ts,
        )
    )).all()

    # Resolve practice → l1_type for category derivation.
    practice_ids = {r.practice_id for r in own_rows if r.practice_id}
    if practice_ids:
        prow = (await db.execute(select(Practice.id, Practice.l1_type).where(Practice.id.in_(practice_ids)))).all()
        l1_by_practice = {pid: l1 for pid, l1 in prow}
    else:
        l1_by_practice = {}

    for r in own_rows:
        if r.subscription_id and r.subscription_id not in in_scope_sub_ids:
            continue
        entries.append(LedgerEntry(
            source="own",
            date=r.picked_up_at.date(),
            category=_l1_to_category(l1_by_practice.get(r.practice_id)),
            product_name=r.brand_name,
            brand=r.brand_name,
            manufacturer=None,
            qty=r.given_volume,
            unit=r.volume_unit,
            price=r.price,
            **_sub_context(r.subscription_id),
        ))

    # Own-shop SeedOrder rows.
    own_seed = (await db.execute(
        select(
            PackingList.picked_up_at,
            SeedOrder.subscription_id,
            SeedOrder.quantity,
            SeedOrder.unit,
            SeedOrder.total_price,
        ).select_from(PackingList).join(
            SeedOrder, SeedOrder.id == PackingList.seed_order_id,
        ).where(
            SeedOrder.dealer_user_id == dealer_id,
            SeedOrder.farmer_user_id == user_id,
            PackingList.picked_up_at.is_not(None),
            PackingList.picked_up_at >= cutoff_ts,
        )
    )).all()
    for r in own_seed:
        if r.subscription_id and r.subscription_id not in in_scope_sub_ids:
            continue
        entries.append(LedgerEntry(
            source="own",
            date=r.picked_up_at.date(),
            category="SEED",
            product_name=None,
            brand=None,
            manufacturer=None,
            qty=r.quantity,
            unit=r.unit,
            price=r.total_price,
            **_sub_context(r.subscription_id),
        ))

    # ── Own manual entries ─────────────────────────────────────
    # Manual entries have NO sub tie; only appear under "all" filter.
    if filter == "all":
        manual = (await db.execute(
            select(DealerManualSale).where(
                DealerManualSale.dealer_user_id == dealer_id,
                DealerManualSale.farmer_user_id == user_id,
                DealerManualSale.sale_date >= twelve_months_ago,
            )
        )).scalars().all()
        for m in manual:
            entries.append(LedgerEntry(
                source="own_manual",
                date=m.sale_date,
                category=m.category,
                product_name=m.product_name,
                brand=m.brand,
                manufacturer=m.manufacturer,
                qty=m.qty,
                unit=m.unit,
                price=m.price,
                manual_sale_id=m.id,
            ))

    # ── Anonymised other-shop rows ─────────────────────────────
    # RT-mediated purchases by this farmer at OTHER dealers, tied to a sub.
    other_rows = (await db.execute(
        select(
            PackingList.picked_up_at,
            Order.subscription_id,
            OrderItem.brand_name,
            OrderItem.given_volume,
            OrderItem.volume_unit,
            OrderItem.practice_id,
        ).select_from(PackingList).join(
            Order, Order.id == PackingList.order_id,
        ).join(
            OrderItem, OrderItem.order_id == Order.id,
        ).where(
            Order.dealer_user_id != dealer_id,
            Order.farmer_user_id == user_id,
            PackingList.picked_up_at.is_not(None),
            PackingList.picked_up_at >= cutoff_ts,
        )
    )).all()
    other_practice_ids = {r.practice_id for r in other_rows if r.practice_id} - practice_ids
    if other_practice_ids:
        prow2 = (await db.execute(select(Practice.id, Practice.l1_type).where(Practice.id.in_(other_practice_ids)))).all()
        for pid, l1 in prow2:
            l1_by_practice[pid] = l1

    for r in other_rows:
        if not r.subscription_id or r.subscription_id not in in_scope_sub_ids:
            continue
        entries.append(LedgerEntry(
            source="other_shop",
            date=r.picked_up_at.date(),
            category=_l1_to_category(l1_by_practice.get(r.practice_id)),
            product_name=r.brand_name,  # brand-level common identity
            brand=r.brand_name,
            manufacturer=None,
            qty=r.given_volume,
            unit=r.volume_unit,
            price=None,  # deliberately hidden
            **_sub_context(r.subscription_id),
        ))

    # Other-shop seed orders too.
    other_seed = (await db.execute(
        select(
            PackingList.picked_up_at,
            SeedOrder.subscription_id,
            SeedOrder.quantity,
            SeedOrder.unit,
        ).select_from(PackingList).join(
            SeedOrder, SeedOrder.id == PackingList.seed_order_id,
        ).where(
            SeedOrder.dealer_user_id != dealer_id,
            SeedOrder.farmer_user_id == user_id,
            PackingList.picked_up_at.is_not(None),
            PackingList.picked_up_at >= cutoff_ts,
        )
    )).all()
    for r in other_seed:
        if not r.subscription_id or r.subscription_id not in in_scope_sub_ids:
            continue
        entries.append(LedgerEntry(
            source="other_shop",
            date=r.picked_up_at.date(),
            category="SEED",
            product_name=None,
            brand=None,
            manufacturer=None,
            qty=r.quantity,
            unit=r.unit,
            price=None,
            **_sub_context(r.subscription_id),
        ))

    entries.sort(key=lambda e: e.date, reverse=True)

    return FarmerDetail(
        user_id=farmer.id,
        name=farmer.name,
        phone=farmer.phone,
        photo_url=farmer.photo_url,
        state_cosh_id=farmer.state_cosh_id,
        district_cosh_id=farmer.district_cosh_id,
        sub_district=farmer.sub_district_cosh_id,
        state_name=names.get(farmer.state_cosh_id) if farmer.state_cosh_id else None,
        district_name=names.get(farmer.district_cosh_id) if farmer.district_cosh_id else None,
        note=note_row.note if note_row else None,
        is_claimed=farmer.self_registered_at is not None,
        entries=entries,
    )


# ── POST /dealer/ledger/lookup-phone ──────────────────────────────

@router.post("/lookup-phone", response_model=PhoneLookupResponse)
async def lookup_phone(
    body: PhoneLookupRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    normalised = _normalise_phone(body.phone)
    user = (await db.execute(select(User).where(User.phone == normalised))).scalar_one_or_none()
    if not user:
        return PhoneLookupResponse(found=False)
    # Coaching-sandbox isolation: pretend coaching students don't
    # exist in real dealer surfaces. Prevents identity + address
    # leak of an isolated coaching workspace user into a real
    # dealer's ledger.
    if await get_coaching_student_for_user(db, user.id) is not None:
        return PhoneLookupResponse(found=False)
    cosh_ids = {c for c in (user.state_cosh_id, user.district_cosh_id) if c}
    names = await _resolve_cosh_names(db, cosh_ids, current_user.language_code or "en")
    return PhoneLookupResponse(
        found=True,
        user_id=user.id,
        name=user.name,
        phone=user.phone,
        photo_url=user.photo_url,
        state_cosh_id=user.state_cosh_id,
        district_cosh_id=user.district_cosh_id,
        sub_district=user.sub_district_cosh_id,
        state_name=names.get(user.state_cosh_id) if user.state_cosh_id else None,
        district_name=names.get(user.district_cosh_id) if user.district_cosh_id else None,
    )


# ── POST /dealer/ledger/manual-sale — create ──────────────────────

async def _create_farmer_user(db: AsyncSession, new: NewFarmerFields) -> User:
    normalised = _normalise_phone(new.phone)
    existing = (await db.execute(select(User).where(User.phone == normalised))).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "phone_already_exists",
                "message": "A farmer with this phone already exists. Use lookup to fetch and reuse.",
                "user_id": existing.id,
            },
        )
    user = User(
        id=new_uuid(),
        phone=normalised,
        name=new.name.strip(),
        state_cosh_id=new.state_cosh_id,
        district_cosh_id=new.district_cosh_id,
        sub_district_cosh_id=(new.sub_district or None),
    )
    db.add(user)
    await db.flush()
    # Assign FARMER role by default — every PWA user is a Farmer.
    db.add(UserRole(id=new_uuid(), user_id=user.id, role_type=RoleType.FARMER, status=StatusEnum.ACTIVE))
    return user


@router.post("/manual-sale", response_model=ManualSaleResponse)
async def create_manual_sale(
    body: ManualSaleCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    dealer_id = current_user.id
    if bool(body.farmer_user_id) == bool(body.new_farmer):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_farmer_ref", "message": "Provide exactly one of farmer_user_id or new_farmer."},
        )

    if body.new_farmer:
        farmer = await _create_farmer_user(db, body.new_farmer)
        farmer_id = farmer.id
    else:
        farmer_id = body.farmer_user_id  # type: ignore[assignment]
        exists = (await db.execute(select(User.id).where(User.id == farmer_id))).scalar_one_or_none()
        if not exists:
            raise HTTPException(status_code=404, detail={"code": "farmer_not_found"})

    sale = DealerManualSale(
        id=new_uuid(),
        dealer_user_id=dealer_id,
        farmer_user_id=farmer_id,
        category=body.sale.category,
        product_name=body.sale.product_name.strip(),
        brand=(body.sale.brand or None),
        manufacturer=(body.sale.manufacturer or None),
        qty=body.sale.qty,
        unit=body.sale.unit.strip(),
        price=body.sale.price,
        sale_date=body.sale.sale_date,
        notes=(body.sale.notes or None),
    )
    db.add(sale)
    await db.commit()
    return ManualSaleResponse(id=sale.id, farmer_user_id=farmer_id)


# ── PATCH /dealer/ledger/manual-sale/{id} ─────────────────────────

@router.patch("/manual-sale/{sale_id}", response_model=ManualSaleResponse)
async def update_manual_sale(
    sale_id: str,
    body: ManualSaleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    sale = (await db.execute(select(DealerManualSale).where(DealerManualSale.id == sale_id))).scalar_one_or_none()
    if not sale or sale.dealer_user_id != current_user.id:
        raise HTTPException(status_code=404, detail={"code": "sale_not_found"})

    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(sale, field, value)
    await db.commit()
    return ManualSaleResponse(id=sale.id, farmer_user_id=sale.farmer_user_id)


# ── DELETE /dealer/ledger/manual-sale/{id} ────────────────────────

@router.delete("/manual-sale/{sale_id}")
async def delete_manual_sale(
    sale_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    sale = (await db.execute(select(DealerManualSale).where(DealerManualSale.id == sale_id))).scalar_one_or_none()
    if not sale or sale.dealer_user_id != current_user.id:
        raise HTTPException(status_code=404, detail={"code": "sale_not_found"})
    await db.delete(sale)
    await db.commit()
    return {"detail": "Deleted"}


# ── PUT /dealer/ledger/farmers/{user_id}/note ─────────────────────

@router.put("/farmers/{user_id}/note")
async def upsert_note(
    user_id: str,
    body: NoteUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    dealer_id = current_user.id
    if not await _dealer_has_farmer_in_roster(db, dealer_id, user_id):
        raise HTTPException(status_code=404, detail={"code": "farmer_not_in_roster"})
    row = (await db.execute(
        select(DealerFarmerNote).where(
            DealerFarmerNote.dealer_user_id == dealer_id,
            DealerFarmerNote.farmer_user_id == user_id,
        )
    )).scalar_one_or_none()
    text = (body.note or "").strip()
    if not text:
        if row:
            await db.delete(row)
            await db.commit()
        return {"detail": "Note cleared"}
    if row:
        row.note = text
    else:
        db.add(DealerFarmerNote(
            id=new_uuid(),
            dealer_user_id=dealer_id,
            farmer_user_id=user_id,
            note=text,
        ))
    await db.commit()
    return {"detail": "Note saved"}


# ── PATCH /dealer/ledger/farmers/{user_id} — canonical name/address edit ──

@router.patch("/farmers/{user_id}")
async def update_farmer_info(
    user_id: str,
    body: FarmerInfoUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer),
):
    dealer_id = current_user.id
    if not await _dealer_has_farmer_in_roster(db, dealer_id, user_id):
        raise HTTPException(status_code=404, detail={"code": "farmer_not_in_roster"})
    farmer = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not farmer:
        raise HTTPException(status_code=404, detail={"code": "farmer_not_found"})

    # Farmer owns their profile once they've self-registered on the
    # PWA. Dealer edits become an override the farmer never asked for.
    if farmer.self_registered_at is not None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "farmer_is_claimed",
                "message": "This farmer is registered on RootsTalk and manages their own profile. Only they can update these details.",
            },
        )

    updates = body.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"]:
        farmer.name = updates["name"].strip()
    if "state_cosh_id" in updates:
        farmer.state_cosh_id = updates["state_cosh_id"]
    if "district_cosh_id" in updates:
        farmer.district_cosh_id = updates["district_cosh_id"]
    if "sub_district" in updates:
        farmer.sub_district_cosh_id = (updates["sub_district"] or None)
    await db.commit()
    return {"detail": "Farmer updated"}
