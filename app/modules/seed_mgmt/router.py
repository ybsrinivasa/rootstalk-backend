from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.seed_mgmt.models import SeedVariety, VarietyPoP, SeedOrderFull, SeedOrderStatus
from app.modules.subscriptions.models import Subscription

router = APIRouter(tags=["Seed Management"])


# ── Authorisation gate (Batch O, 2026-05-18) ────────────────────────────────
#
# Per user 2026-05-18: Seed Varieties is restricted three ways.
#   (1) Feature available only to clients onboarded as Seed Companies
#       (Client.org_type_cosh_ids contains 'org_type_seed_companies').
#   (2) Within such a client, only CA and SEED_DATA_MANAGER can manage
#       the catalogue. FIELD_MANAGER / REPORT_USER / etc. are refused
#       even when they're members of the seed company.
# These checks ride on top of the tenant-isolation guard in
# get_current_user (Batch I) — so cross-tenant access is already
# refused at the request boundary; this helper only fires on
# same-client access.

SEED_COMPANY_COSH_ID = "org_type_seed_companies"


async def _assert_can_manage_seed_varieties(
    db: AsyncSession, user_id: str, client_id: str,
) -> None:
    """Both org-type AND role gate. Raises 403 with stable codes:
      - `not_a_seed_company` — client.org_type_cosh_ids doesn't
        include the Seed Company tag.
      - `seed_data_or_ca_only` — user doesn't have CA role, nor
        the Seed Data privilege (after the org-type check passes).

    Accepted callers (after Batch X, 2026-05-19):
      • ACTIVE ClientUser with role CA, OR
      • ACTIVE SUBJECT_EXPERT holding the SEED_DATA privilege via
        ClientUserPrivilege, OR
      • CM-EDIT assignee (CMs retain full CA-equivalent access when
        impersonating via the SA-Portal login-as flow).

    The legacy SEED_DATA_MANAGER role is gone — the Batch X
    migration backfilled existing rows into SE + SEED_DATA
    privilege. The role enum value is kept for SAEnum compat but
    no longer surfaces in the role picker or auth path."""
    from app.modules.clients.models import (
        CMClientAssignment, CMRights, Client, ClientOrganisationType,
        ClientUser, ClientUserPrivilege, ClientUserPrivilegeModel,
        ClientUserRole,
    )
    from app.modules.platform.models import StatusEnum

    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    org_types = (await db.execute(
        select(ClientOrganisationType).where(
            ClientOrganisationType.client_id == client_id,
        )
    )).scalars().all()
    if not any(
        ot.org_type_cosh_id == SEED_COMPANY_COSH_ID for ot in org_types
    ):
        raise HTTPException(status_code=403, detail={
            "code": "not_a_seed_company",
            "message": (
                "Seed Varieties is available only to clients onboarded "
                "as a Seed Company. Contact RootsTalk support to add "
                "the Seed Company organisation type."
            ),
        })

    cus = (await db.execute(
        select(ClientUser).where(
            ClientUser.user_id == user_id,
            ClientUser.client_id == client_id,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalars().all()
    # CA always passes.
    if any(cu.role == ClientUserRole.CA for cu in cus):
        return
    # SE who holds the SEED_DATA privilege passes.
    is_se = any(cu.role == ClientUserRole.SUBJECT_EXPERT for cu in cus)
    if is_se:
        holds = (await db.execute(
            select(ClientUserPrivilegeModel.id).where(
                ClientUserPrivilegeModel.client_id == client_id,
                ClientUserPrivilegeModel.user_id == user_id,
                ClientUserPrivilegeModel.privilege == ClientUserPrivilege.SEED_DATA,
            ).limit(1)
        )).scalar_one_or_none()
        if holds is not None:
            return

    # CM-EDIT path (2026-05-18). CMs have full CA-equivalent access
    # when impersonating via the SA-Portal login-as flow.
    cm_assignment = (await db.execute(
        select(CMClientAssignment.id).where(
            CMClientAssignment.cm_user_id == user_id,
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
            CMClientAssignment.rights == CMRights.EDIT,
        ).limit(1)
    )).scalar_one_or_none()
    if cm_assignment is not None:
        return

    raise HTTPException(status_code=403, detail={
        "code": "seed_data_or_ca_only",
        "message": (
            "Only the CA, or the Subject Expert holding the Seed Data "
            "privilege, can manage Seed Varieties for this company."
        ),
    })


# ── DUS options lookup (Batch W, 2026-05-19) ────────────────────────────────

@router.get("/client/{client_id}/seed/dus-options")
async def list_dus_options(
    client_id: str,
    crop_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns the crop-scoped DUS taxonomy as a nested tree for
    cascading Part → Sub-Part → Character → Descriptor dropdowns on
    the SE's variety edit form.

    Source: Cosh `dus_characters_descriptors` Connect (1,562 rows on
    first sync 2026-05-19). One round-trip per crop; cache client-side
    for the lifetime of the form mount.

    Empty array when Cosh hasn't characterised the crop yet — SE sees
    "No DUS taxonomy for this crop in Cosh" on the form.
    """
    await _assert_can_manage_seed_varieties(db, current_user.id, client_id)
    from app.services.cosh_dus_view import list_dus_options_for_crop
    return await list_dus_options_for_crop(db, crop_cosh_id=crop_cosh_id)


# ── SDM / Client Portal: Variety Catalog ─────────────────────────────────────

@router.get("/client/{client_id}/varieties")
async def list_varieties(
    client_id: str,
    crop_cosh_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_manage_seed_varieties(db, current_user.id, client_id)
    # Batch W-3 (2026-05-19) — eager-load pop_assignments so the
    # serializer doesn't trigger a lazy-load (async SQLAlchemy raises
    # MissingGreenlet during JSON encoding otherwise). The list 500'd
    # the moment a client had at least one variety; tests didn't
    # catch it because they hit the function directly within the
    # greenlet context.
    q = select(SeedVariety).options(
        selectinload(SeedVariety.pop_assignments),
    ).where(
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
    await _assert_can_manage_seed_varieties(db, current_user.id, client_id)
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
    # Batch W-3 (2026-05-19) — re-fetch with pop_assignments eagerly
    # loaded so _variety_out doesn't trigger a lazy-load during
    # serialization. refresh() alone doesn't populate relationships.
    await db.refresh(variety, attribute_names=["pop_assignments"])
    return _variety_out(variety)


@router.put("/client/{client_id}/varieties/{variety_id}")
async def update_variety(
    client_id: str, variety_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_manage_seed_varieties(db, current_user.id, client_id)
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
    await _assert_can_manage_seed_varieties(db, current_user.id, client_id)
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
    await _assert_can_manage_seed_varieties(db, current_user.id, client_id)
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
    await _assert_can_manage_seed_varieties(db, current_user.id, client_id)
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
        .options(selectinload(SeedVariety.pop_assignments))
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
    # Batch W-3 (2026-05-19) — eager-load pop_assignments to avoid
    # MissingGreenlet during _variety_out serialization. Every caller
    # of this helper passes the result through _variety_out.
    v = (await db.execute(
        select(SeedVariety).options(
            selectinload(SeedVariety.pop_assignments),
        ).where(SeedVariety.id == variety_id, SeedVariety.client_id == client_id)
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
