"""
Phase 10: Support System — Client RM and Neytiri RM APIs.
Case logs, farmer search, alert aggregation.
"""
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.subscriptions.models import Subscription, Alert
from app.modules.orders.models import Order

router = APIRouter(tags=["Support & RM"])


class CaseLog(BaseModel):
    farmer_user_id: str
    client_id: str
    notes: str


# ── Client RM ──────────────────────────────────────────────────────────────────

@router.get("/client-rm/farmer-search")
async def farmer_search(
    name: Optional[str] = None,
    phone: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Client RM searches farmers by name or phone."""
    q = select(User)
    if phone:
        q = q.where(User.phone == phone)
    if name:
        q = q.where(User.name.ilike(f"%{name}%"))
    result = await db.execute(q.limit(20))
    users = result.scalars().all()
    return [{"id": u.id, "name": u.name, "phone": u.phone, "district_cosh_id": u.district_cosh_id} for u in users]


@router.get("/client-rm/farmer/{farmer_id}/context")
async def farmer_context(
    farmer_id: str,
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full farmer context: subscriptions, active orders, pending queries."""
    subscriptions = (await db.execute(
        select(Subscription).where(
            Subscription.farmer_user_id == farmer_id,
            Subscription.client_id == client_id,
        )
    )).scalars().all()

    orders = (await db.execute(
        select(Order).where(
            Order.farmer_user_id == farmer_id,
            Order.client_id == client_id,
        ).order_by(Order.created_at.desc()).limit(10)
    )).scalars().all()

    return {
        "farmer_id": farmer_id,
        "subscriptions": [{"id": s.id, "status": s.status, "package_id": s.package_id,
                           "crop_start_date": s.crop_start_date} for s in subscriptions],
        "orders": [{"id": o.id, "status": o.status, "date_from": o.date_from, "date_to": o.date_to} for o in orders],
    }


@router.get("/client-rm/alerts")
async def client_rm_alerts(
    client_id: str,
    alert_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All alerts for a client's farmers."""
    q = (select(Alert)
         .join(Subscription, Subscription.id == Alert.subscription_id)
         .where(Subscription.client_id == client_id)
         .order_by(Alert.sent_at.desc())
         .limit(100))
    if alert_type:
        q = q.where(Alert.alert_type == alert_type)
    result = await db.execute(q)
    return result.scalars().all()


# ── Neytiri RM (cross-client) ──────────────────────────────────────────────────

@router.get("/neytiri-rm/user-search")
async def cross_client_user_search(
    phone: Optional[str] = None,
    name: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Neytiri RM searches across all clients by phone or name."""
    q = select(User)
    if phone:
        q = q.where(User.phone == phone)
    if name:
        q = q.where(User.name.ilike(f"%{name}%"))
    result = await db.execute(q.limit(20))
    users = result.scalars().all()
    return [{"id": u.id, "name": u.name, "phone": u.phone, "email": u.email} for u in users]


@router.put("/neytiri-rm/users/{user_id}/reset-password")
async def neytiri_reset_password(
    user_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Neytiri RM IT support: override any user's password."""
    from app.modules.auth.service import hash_password
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not data.get("new_password"):
        raise HTTPException(status_code=422, detail="new_password required")
    user.password_hash = hash_password(data["new_password"])
    await db.commit()
    return {"detail": "Password reset successfully"}


# ── My Subscriptions (Farmer) ──────────────────────────────────────────────────

@router.get("/farmer/my-subscriptions")
async def my_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Subscription)
        .where(Subscription.farmer_user_id == current_user.id, Subscription.status == "ACTIVE")
        .order_by(Subscription.subscription_date.desc())
    )
    subs = result.scalars().all()
    return [{"id": s.id, "client_id": s.client_id, "package_id": s.package_id,
             "reference_number": s.reference_number, "crop_start_date": s.crop_start_date} for s in subs]
