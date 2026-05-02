from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.seed_mgmt.models import SeedVariety, VarietyPoP, SeedOrderFull, SeedOrderStatus
from app.modules.subscriptions.models import Subscription

router = APIRouter(tags=["Seed Management"])


# ── SDM / Client Portal: Variety Catalog ─────────────────────────────────────

@router.get("/client/{client_id}/varieties")
async def list_varieties(
    client_id: str,
    crop_cosh_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(SeedVariety).where(
        SeedVariety.client_id == client_id,
        SeedVariety.status == "ACTIVE",
    ).order_by(SeedVariety.name)
    if crop_cosh_id:
        q = q.where(SeedVariety.crop_cosh_id == crop_cosh_id)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [_variety_out(v) for v in rows]


@router.post("/client/{client_id}/varieties", status_code=201)
async def create_variety(
    client_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    variety = SeedVariety(
        client_id=client_id,
        crop_cosh_id=data["crop_cosh_id"],
        name=data["name"],
        variety_type=data.get("variety_type", "SEED"),
        description_points=data.get("description_points", []),
        dus_characters=data.get("dus_characters"),
        photos=data.get("photos", []),
        created_by_user_id=current_user.id,
    )
    db.add(variety)
    await db.commit()
    await db.refresh(variety)
    return _variety_out(variety)


@router.put("/client/{client_id}/varieties/{variety_id}")
async def update_variety(
    client_id: str, variety_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    variety = await _get_variety(db, variety_id, client_id)
    for field in ["name", "variety_type", "description_points", "dus_characters", "photos", "status"]:
        if field in data:
            setattr(variety, field, data[field])
    await db.commit()
    return _variety_out(variety)


@router.delete("/client/{client_id}/varieties/{variety_id}")
async def deactivate_variety(
    client_id: str, variety_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    variety = await _get_variety(db, variety_id, client_id)
    variety.status = "INACTIVE"
    await db.commit()
    return {"detail": "Variety deactivated"}


# ── PoP assignments ────────────────────────────────────────────────────────────

@router.post("/client/{client_id}/varieties/{variety_id}/pop-assignments", status_code=201)
async def assign_to_pop(
    client_id: str, variety_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_variety(db, variety_id, client_id)
    existing = (await db.execute(
        select(VarietyPoP).where(
            VarietyPoP.variety_id == variety_id,
            VarietyPoP.package_id == data["package_id"],
        )
    )).scalar_one_or_none()
    if existing:
        if existing.status == "INACTIVE":
            existing.status = "ACTIVE"
            await db.commit()
        return {"detail": "Assigned"}
    assignment = VarietyPoP(variety_id=variety_id, package_id=data["package_id"])
    db.add(assignment)
    await db.commit()
    return {"detail": "Assigned"}


@router.delete("/client/{client_id}/varieties/{variety_id}/pop-assignments/{package_id}")
async def remove_from_pop(
    client_id: str, variety_id: str, package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    assignment = (await db.execute(
        select(VarietyPoP).where(
            VarietyPoP.variety_id == variety_id,
            VarietyPoP.package_id == package_id,
        )
    )).scalar_one_or_none()
    if assignment:
        assignment.status = "INACTIVE"
        await db.commit()
    return {"detail": "Removed"}


# ── Farmer: Browse varieties for their subscription's PoP ─────────────────────

@router.get("/farmer/subscriptions/{sub_id}/seed-varieties")
async def browse_seed_varieties(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer browses varieties recommended for their subscription's PoP."""
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == sub_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    result = await db.execute(
        select(SeedVariety)
        .join(VarietyPoP, VarietyPoP.variety_id == SeedVariety.id)
        .where(
            VarietyPoP.package_id == sub.package_id,
            VarietyPoP.status == "ACTIVE",
            SeedVariety.status == "ACTIVE",
        )
        .order_by(SeedVariety.name)
    )
    varieties = result.scalars().all()
    return [_variety_out(v) for v in varieties]


# ── Farmer: Place seed order ───────────────────────────────────────────────────

@router.post("/farmer/seed-orders", status_code=201)
async def place_seed_order(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == data["subscription_id"],
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    variety = (await db.execute(
        select(SeedVariety).where(SeedVariety.id == data["variety_id"])
    )).scalar_one_or_none()
    if not variety:
        raise HTTPException(status_code=404, detail="Variety not found")

    order = SeedOrderFull(
        subscription_id=data["subscription_id"],
        farmer_user_id=current_user.id,
        variety_id=data["variety_id"],
        client_id=variety.client_id,
        dealer_user_id=data.get("dealer_user_id"),
        facilitator_user_id=data.get("facilitator_user_id"),
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return {"id": order.id, "status": order.status, "variety_id": order.variety_id}


@router.get("/farmer/seed-orders")
async def list_farmer_seed_orders(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(SeedOrderFull).where(
            SeedOrderFull.farmer_user_id == current_user.id
        ).order_by(SeedOrderFull.created_at.desc())
    )
    orders = result.scalars().all()
    out = []
    for o in orders:
        variety = (await db.execute(select(SeedVariety).where(SeedVariety.id == o.variety_id))).scalar_one_or_none()
        out.append({
            "id": o.id, "status": o.status,
            "variety_name": variety.name if variety else None,
            "unit": o.unit, "quantity": float(o.quantity) if o.quantity else None,
            "total_price": float(o.total_price) if o.total_price else None,
            "created_at": o.created_at,
        })
    return out


@router.put("/farmer/seed-orders/{order_id}/approve")
async def approve_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await _get_seed_order(db, order_id, current_user.id, farmer=True)
    if order.status != SeedOrderStatus.SENT_FOR_APPROVAL:
        raise HTTPException(status_code=400, detail="Order is not awaiting approval")
    order.status = SeedOrderStatus.PURCHASED
    await db.commit()
    return {"id": order_id, "status": order.status}


@router.put("/farmer/seed-orders/{order_id}/reject")
async def reject_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await _get_seed_order(db, order_id, current_user.id, farmer=True)
    order.status = SeedOrderStatus.REJECTED
    await db.commit()
    return {"id": order_id, "status": order.status}


@router.put("/farmer/seed-orders/{order_id}/cancel")
async def cancel_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await _get_seed_order(db, order_id, current_user.id, farmer=True)
    if order.status not in [SeedOrderStatus.SENT, SeedOrderStatus.ACCEPTED]:
        raise HTTPException(status_code=400, detail="Cannot cancel order in current status")
    order.status = SeedOrderStatus.CANCELLED
    await db.commit()
    return {"id": order_id, "status": order.status}


# ── Dealer: Seed orders ────────────────────────────────────────────────────────

@router.get("/dealer/seed-orders")
async def list_dealer_seed_orders(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(SeedOrderFull).where(
            SeedOrderFull.dealer_user_id == current_user.id,
            SeedOrderFull.status.notin_([SeedOrderStatus.CANCELLED]),
        ).order_by(SeedOrderFull.created_at.desc())
    )
    orders = result.scalars().all()
    out = []
    for o in orders:
        variety = (await db.execute(select(SeedVariety).where(SeedVariety.id == o.variety_id))).scalar_one_or_none()
        farmer = (await db.execute(select(User).where(User.id == o.farmer_user_id))).scalar_one_or_none()
        sub = (await db.execute(select(Subscription).where(Subscription.id == o.subscription_id))).scalar_one_or_none()
        out.append({
            "id": o.id, "status": o.status,
            "variety_name": variety.name if variety else None,
            "crop_cosh_id": variety.crop_cosh_id if variety else None,
            "farmer_name": farmer.name if farmer else None,
            "farm_area_acres": float(sub.farm_area_acres) if sub and sub.farm_area_acres else None,
            "unit": o.unit,
            "quantity": float(o.quantity) if o.quantity else None,
            "total_price": float(o.total_price) if o.total_price else None,
            "created_at": o.created_at,
        })
    return out


@router.put("/dealer/seed-orders/{order_id}/accept")
async def accept_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await _get_seed_order(db, order_id, current_user.id, farmer=False)
    if order.status != SeedOrderStatus.SENT:
        raise HTTPException(status_code=400, detail="Order can only be accepted from SENT status")
    order.status = SeedOrderStatus.ACCEPTED
    await db.commit()
    return {"id": order_id, "status": order.status}


@router.put("/dealer/seed-orders/{order_id}/submit-for-approval")
async def seed_submit_for_approval(
    order_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer enters unit, quantity, and total price → sends to farmer."""
    order = await _get_seed_order(db, order_id, current_user.id, farmer=False)
    if order.status not in [SeedOrderStatus.SENT, SeedOrderStatus.ACCEPTED]:
        raise HTTPException(status_code=400, detail="Cannot submit in current status")
    if not data.get("unit") or not data.get("quantity"):
        raise HTTPException(status_code=422, detail="unit and quantity required")
    order.unit = data["unit"]
    order.quantity = data["quantity"]
    order.total_price = data.get("total_price")
    order.status = SeedOrderStatus.SENT_FOR_APPROVAL
    await db.commit()
    return {"id": order_id, "status": order.status}


@router.put("/dealer/seed-orders/{order_id}/abort")
async def abort_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await _get_seed_order(db, order_id, current_user.id, farmer=False)
    order.status = SeedOrderStatus.SENT
    order.unit = None
    order.quantity = None
    order.total_price = None
    await db.commit()
    return {"id": order_id, "status": order.status}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _variety_out(v: SeedVariety) -> dict:
    return {
        "id": v.id,
        "client_id": v.client_id,
        "crop_cosh_id": v.crop_cosh_id,
        "name": v.name,
        "variety_type": v.variety_type,
        "description_points": v.description_points or [],
        "dus_characters": v.dus_characters,
        "photos": v.photos or [],
        "status": v.status,
        "pop_assignments": [{"package_id": a.package_id, "status": a.status}
                            for a in (v.pop_assignments or [])],
    }


async def _get_variety(db: AsyncSession, variety_id: str, client_id: str) -> SeedVariety:
    v = (await db.execute(
        select(SeedVariety).where(SeedVariety.id == variety_id, SeedVariety.client_id == client_id)
    )).scalar_one_or_none()
    if not v:
        raise HTTPException(status_code=404, detail="Variety not found")
    return v


async def _get_seed_order(db: AsyncSession, order_id: str, user_id: str, farmer: bool) -> SeedOrderFull:
    if farmer:
        condition = SeedOrderFull.farmer_user_id == user_id
    else:
        condition = SeedOrderFull.dealer_user_id == user_id
    order = (await db.execute(
        select(SeedOrderFull).where(SeedOrderFull.id == order_id, condition)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Seed order not found")
    return order
