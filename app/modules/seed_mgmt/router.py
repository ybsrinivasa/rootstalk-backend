from typing import Optional
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
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

# 2026-05-22 — switched from the legacy hardcoded slug
# "org_type_seed_companies" to the live Cosh `organization_types`
# Core UUID for "Seed Company" (synced 2026-05-22). The hardcoded
# UUID is acceptable here because (a) Cosh UUIDs are stable, and
# (b) this gate fires on every Seed Module request — a per-call
# Cosh lookup by English name would add an extra DB roundtrip.
# If Cosh ever renames or replaces the "Seed Company" row, update
# this constant.
SEED_COMPANY_COSH_ID = "4b0847f9-a590-452f-9129-ee0e2d946dd9"


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


# ── Assignable packages for a variety (Batch Y, 2026-05-19) ─────────────────

@router.get("/client/{client_id}/seed/assignable-packages")
async def list_assignable_packages(
    client_id: str,
    crop_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Packages on this crop the SE can assign a variety to, with
    everything the variety form's "Sold via Packages" picker needs
    inline:

      * id, name, status (DRAFT or ACTIVE only; INACTIVE skipped),
        package_type, duration_days
      * locations: list of {state_cosh_id, state_name_en,
        district_cosh_id, district_name_en} — friendly names
        resolved from Cosh `state_list` / `district_list` Cores.
      * parameters: list of {parameter_id, parameter_name_en,
        variables: [{variable_id, variable_name_en}]} grouped by
        parameter — feeds the P-V details disclosure.

    One round-trip so the picker can render cards + filter by
    district without chatty per-package fetches.
    """
    await _assert_can_manage_seed_varieties(db, current_user.id, client_id)

    from app.modules.advisory.models import (
        Package, PackageLocation, PackageVariable, Parameter, Variable,
    )
    from app.modules.sync.models import CoshCoreItem

    # 2026-05-22 — pull every non-INACTIVE row, then roll up by
    # lineage `(client, crop, lower(name))` so the picker shows one
    # row per lineage (DRAFT > ACTIVE precedence) instead of every
    # version. Variety assignment is per-lineage anyway — the SE
    # picks the package, not a specific historical version.
    raw_pkgs = (await db.execute(
        select(Package).where(
            Package.client_id == client_id,
            Package.crop_cosh_id == crop_cosh_id,
            Package.status.in_(("DRAFT", "ACTIVE")),
        ).order_by(Package.name)
    )).scalars().all()
    if not raw_pkgs:
        return []

    _STATUS_RANK = {"DRAFT": 0, "ACTIVE": 1, "INACTIVE": 2}

    def _lineage_key(p):
        return (p.crop_cosh_id, (p.name or "").strip().lower())

    def _sort_within_lineage(p):
        # Most-current-first inside the bucket: DRAFT > ACTIVE; ties
        # broken by version desc, created_at desc.
        return (
            _STATUS_RANK.get(getattr(p.status, "value", p.status), 99),
            -p.version,
            -p.created_at.timestamp(),
        )

    by_lineage: dict[tuple, list] = {}
    for p in raw_pkgs:
        by_lineage.setdefault(_lineage_key(p), []).append(p)
    pkgs = [
        sorted(group, key=_sort_within_lineage)[0]
        for group in by_lineage.values()
    ]
    pkgs.sort(key=lambda p: (p.name or "").casefold())

    pkg_ids = [p.id for p in pkgs]

    # Locations (joined to grab raw state/district cosh_ids).
    loc_rows = (await db.execute(
        select(PackageLocation).where(
            PackageLocation.package_id.in_(pkg_ids),
        )
    )).scalars().all()
    locs_by_pkg: dict[str, list] = {p.id: [] for p in pkgs}
    cosh_ids_needed: set[str] = set()
    for l in loc_rows:
        locs_by_pkg.setdefault(l.package_id, []).append(l)
        if l.state_cosh_id:
            cosh_ids_needed.add(l.state_cosh_id)
        if l.district_cosh_id:
            cosh_ids_needed.add(l.district_cosh_id)

    # Resolve state/district friendly names from Cosh Core in one shot.
    cosh_names: dict[str, str] = {}
    if cosh_ids_needed:
        cores = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.cosh_id.in_(cosh_ids_needed),
                CoshCoreItem.core_type.in_(("state_list", "district_list")),
                CoshCoreItem.status == "active",
            )
        )).scalars().all()
        for c in cores:
            t = c.translations or {}
            cosh_names[c.cosh_id] = t.get("en") or t.get("English") or c.cosh_id

    # P-V wiring: PackageVariable links a package to a (parameter, variable).
    pv_rows = (await db.execute(
        select(PackageVariable, Parameter, Variable)
        .join(Parameter, Parameter.id == PackageVariable.parameter_id)
        .join(Variable, Variable.id == PackageVariable.variable_id)
        .where(PackageVariable.package_id.in_(pkg_ids))
    )).all()
    # pvs_by_pkg[pkg_id] = {parameter_id: {"name": ..., "variables": [(vid,name)]}}
    pvs_by_pkg: dict[str, dict] = {p.id: {} for p in pkgs}
    for pv, param, var in pv_rows:
        bucket = pvs_by_pkg.setdefault(pv.package_id, {})
        slot = bucket.setdefault(param.id, {
            "parameter_id": param.id,
            "parameter_name_en": param.name,
            "variables": [],
        })
        slot["variables"].append({
            "variable_id": var.id,
            "variable_name_en": var.name,
        })

    out = []
    for p in pkgs:
        out.append({
            "id": p.id,
            "name": p.name,
            "status": p.status,
            "package_type": p.package_type,
            "duration_days": p.duration_days,
            "locations": [
                {
                    "state_cosh_id": l.state_cosh_id,
                    "state_name_en": cosh_names.get(l.state_cosh_id, l.state_cosh_id),
                    "district_cosh_id": l.district_cosh_id,
                    "district_name_en": cosh_names.get(l.district_cosh_id, l.district_cosh_id),
                }
                for l in sorted(
                    locs_by_pkg.get(p.id, []),
                    key=lambda x: (
                        (cosh_names.get(x.state_cosh_id, "") or "").casefold(),
                        (cosh_names.get(x.district_cosh_id, "") or "").casefold(),
                    ),
                )
            ],
            "parameters": sorted(
                list(pvs_by_pkg.get(p.id, {}).values()),
                key=lambda x: (x["parameter_name_en"] or "").casefold(),
            ),
        })
    return out


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
    # Batch Z (2026-05-19) — INACTIVE varieties stay on the list so
    # the SE can see what they've retired and reactivate if needed.
    # Frontend dims the card and offers Reactivate instead of
    # Deactivate.
    q = select(SeedVariety).options(
        selectinload(SeedVariety.pop_assignments),
    ).where(
        SeedVariety.client_id == client_id,
    ).order_by(SeedVariety.name)
    if crop_cosh_id:
        q = q.where(SeedVariety.crop_cosh_id == crop_cosh_id)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [_variety_out(v) for v in rows]


# Batch Z (2026-05-19) — cap photos per variety. User-set
# 2026-05-19: "limit the number of images per variety to four."
MAX_PHOTOS_PER_VARIETY = 4


def _validate_photos(photos):
    if not isinstance(photos, list):
        return
    if len(photos) > MAX_PHOTOS_PER_VARIETY:
        raise HTTPException(status_code=422, detail={
            "code": "too_many_photos",
            "message": (
                f"A variety can have at most {MAX_PHOTOS_PER_VARIETY} "
                f"photos. Remove some before saving."
            ),
            "max": MAX_PHOTOS_PER_VARIETY,
            "submitted": len(photos),
        })


@router.post("/client/{client_id}/varieties", status_code=201)
async def create_variety(
    client_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_manage_seed_varieties(db, current_user.id, client_id)
    _validate_photos(data.get("photos"))
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
    if "photos" in data:
        _validate_photos(data["photos"])
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


@router.put("/client/{client_id}/varieties/{variety_id}/reactivate")
async def reactivate_variety(
    client_id: str, variety_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Batch Z (2026-05-19) — flip an INACTIVE variety back to
    ACTIVE. Idempotent on already-ACTIVE rows."""
    await _assert_can_manage_seed_varieties(db, current_user.id, client_id)
    variety = await _get_variety(db, variety_id, client_id)
    variety.status = "ACTIVE"
    await db.commit()
    return {"detail": "Variety reactivated", "id": variety_id}


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


_SEED_POSTPONE_MAX_DAYS = 14
"""Hard cap on dealer-postpone days for seed orders.

Seeds aren't on a timeline so we can't compute "remaining window − 1"
the way pesticide / fertiliser items do. 14 days keeps a dealer
from sitting on a seed order indefinitely while giving them a
realistic restock window. (2026-05-31 narrative, Batch 12 carve-out.)
"""


@router.put("/farmer/seed-orders/{order_id}/cancel")
async def cancel_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer cancels a seed order and migrates it to a fresh DRAFT.

    Orders V2 (2026-05-31) parity: seeds get the same cancel-and-
    migrate flow as pesticide / fertiliser items. The husk goes
    CANCELLED + REROUTED; a new DRAFT row inherits the same
    `lineage_id`, the variety, quantity and unit, and waits for the
    farmer to pick a new recipient via `/farmer/seed-orders/{id}/send`.
    """
    from app.services.order_events import record_event as _record_event

    order = await _get_seed_order(db, order_id, current_user.id, farmer=True)
    terminal = {
        SeedOrderStatus.CANCELLED, SeedOrderStatus.PURCHASED,
        SeedOrderStatus.REROUTED,
    }
    if order.status in terminal:
        raise HTTPException(
            status_code=400,
            detail=f"Order is already {order.status}; nothing to cancel.",
        )

    prev_status = order.status

    new_draft = SeedOrderFull(
        subscription_id=order.subscription_id,
        farmer_user_id=order.farmer_user_id,
        variety_id=order.variety_id,
        client_id=order.client_id,
        dealer_user_id=None,
        facilitator_user_id=None,
        unit=order.unit,
        quantity=order.quantity,
        total_price=None,  # reset — next dealer prices afresh
        status=SeedOrderStatus.DRAFT,
        lineage_id=order.lineage_id,
    )
    db.add(new_draft)
    await db.flush()

    await _record_event(
        db, lineage_id=order.lineage_id,
        event_type="REROUTED_FROM",
        actor_user_id=current_user.id, actor_role="FARMER",
        seed_order_id=order.id,
        prev_status=prev_status,
        new_status=SeedOrderStatus.REROUTED.value,
        metadata={
            "to_seed_order_id": new_draft.id,
            "reason": "seed_cancel_migrate",
        },
    )
    await _record_event(
        db, lineage_id=order.lineage_id,
        event_type="REROUTED_TO",
        actor_user_id=current_user.id, actor_role="FARMER",
        seed_order_id=new_draft.id,
        prev_status=SeedOrderStatus.REROUTED.value,
        new_status=SeedOrderStatus.DRAFT.value,
        metadata={
            "from_seed_order_id": order.id,
            "reason": "seed_cancel_migrate",
        },
    )
    await _record_event(
        db, lineage_id=order.id,  # husk-level event keyed on husk.id
        event_type="CANCELLED_BY_FARMER",
        actor_user_id=current_user.id, actor_role="FARMER",
        seed_order_id=order.id,
        prev_status=prev_status,
        new_status=SeedOrderStatus.CANCELLED.value,
        metadata={"new_draft_seed_order_id": new_draft.id},
    )

    order.status = SeedOrderStatus.CANCELLED
    await db.commit()
    return {
        "id": order_id,
        "status": order.status,
        "new_draft_seed_order_id": new_draft.id,
    }


@router.delete("/farmer/seed-orders/{order_id}")
async def delete_cancelled_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Drop a CANCELLED seed-order husk. Mirrors the pesticide /
    fertiliser DELETE on `/farmer/orders/{id}`."""
    order = await _get_seed_order(db, order_id, current_user.id, farmer=True)
    if order.status != SeedOrderStatus.CANCELLED:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "must_cancel_first",
                "message": "Only cancelled seed orders can be deleted.",
            },
        )
    await db.delete(order)
    await db.commit()
    return {"deleted": True}


class SeedOrderSend(BaseModel):
    dealer_user_id: Optional[str] = None
    facilitator_user_id: Optional[str] = None


@router.put("/farmer/seed-orders/{order_id}/send")
async def send_draft_seed_order(
    order_id: str,
    body: SeedOrderSend,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Assign a recipient to a DRAFT seed order and send it."""
    from app.modules.orders.router import (
        _assert_active_dealer, _assert_active_facilitator,
    )
    from app.services.order_events import record_event as _record_event

    order = await _get_seed_order(db, order_id, current_user.id, farmer=True)
    if order.status != SeedOrderStatus.DRAFT:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "not_a_draft",
                "message": "Only DRAFT seed orders can be sent.",
            },
        )
    if bool(body.dealer_user_id) == bool(body.facilitator_user_id):
        raise HTTPException(
            status_code=422,
            detail="Pick exactly one — dealer or facilitator.",
        )

    if body.dealer_user_id:
        await _assert_active_dealer(db, body.dealer_user_id)
        order.dealer_user_id = body.dealer_user_id
        order.facilitator_user_id = None
    else:
        await _assert_active_facilitator(db, body.facilitator_user_id)
        order.facilitator_user_id = body.facilitator_user_id
        order.dealer_user_id = None

    order.status = SeedOrderStatus.SENT
    await _record_event(
        db, lineage_id=order.lineage_id,
        event_type="SENT",
        actor_user_id=current_user.id, actor_role="FARMER",
        seed_order_id=order.id,
        prev_status=SeedOrderStatus.DRAFT.value,
        new_status=SeedOrderStatus.SENT.value,
        metadata={
            "dealer_user_id": order.dealer_user_id,
            "facilitator_user_id": order.facilitator_user_id,
        },
    )
    await db.commit()
    return {
        "id": order.id, "status": order.status,
        "dealer_user_id": order.dealer_user_id,
        "facilitator_user_id": order.facilitator_user_id,
    }


# ── Dealer: Seed orders ────────────────────────────────────────────────────────

@router.get("/dealer/seed-orders")
async def list_dealer_seed_orders(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.modules.orders.router import _assert_active_dealer
    await _assert_active_dealer(db, current_user.id)
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


@router.get("/dealer/seed-orders/{order_id}/postpone-window")
async def seed_postpone_window(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Authoritative `max_days` for the dealer's postpone picker.

    Seeds aren't timeline-anchored so we use a fixed cap
    (`_SEED_POSTPONE_MAX_DAYS`) instead of "remaining window − 1".
    """
    from app.modules.orders.router import _assert_active_dealer
    await _assert_active_dealer(db, current_user.id)
    order = await _get_seed_order(db, order_id, current_user.id, farmer=False)
    return {
        "max_days": _SEED_POSTPONE_MAX_DAYS,
        "can_postpone": True,
    }


@router.put("/dealer/seed-orders/{order_id}/postpone")
async def postpone_seed_order(
    order_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer postpones a seed order by N days.

    Body: `{ "days": int }` in [1, _SEED_POSTPONE_MAX_DAYS].
    Server stamps `postponed_until = IST today + days`.
    """
    from app.modules.orders.router import _assert_active_dealer
    from app.services.order_events import record_event as _record_event

    await _assert_active_dealer(db, current_user.id)
    order = await _get_seed_order(db, order_id, current_user.id, farmer=False)
    if order.status not in [SeedOrderStatus.SENT, SeedOrderStatus.ACCEPTED]:
        raise HTTPException(
            status_code=400,
            detail="Order can only be postponed from SENT or ACCEPTED status",
        )

    days = data.get("days")
    if not isinstance(days, int) or days < 1 or days > _SEED_POSTPONE_MAX_DAYS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "postpone_days_out_of_range",
                "message": f"Pick between 1 and {_SEED_POSTPONE_MAX_DAYS} day(s).",
                "max_days": _SEED_POSTPONE_MAX_DAYS,
            },
        )

    ist_today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
    target = ist_today + timedelta(days=days)
    order.postponed_until = datetime(
        target.year, target.month, target.day, 0, 0, tzinfo=timezone.utc,
    ) - timedelta(hours=5, minutes=30)

    prev_status = order.status
    order.status = SeedOrderStatus.POSTPONED
    await _record_event(
        db, lineage_id=order.lineage_id,
        event_type="MARKED_POSTPONED",
        actor_user_id=current_user.id, actor_role="DEALER",
        seed_order_id=order.id,
        prev_status=prev_status,
        new_status=SeedOrderStatus.POSTPONED.value,
        metadata={
            "days": days,
            "postponed_until": order.postponed_until.isoformat(),
        },
    )
    await db.commit()
    return {
        "id": order_id, "status": order.status,
        "postponed_until": order.postponed_until,
    }


@router.put("/dealer/seed-orders/{order_id}/not-available")
async def mark_seed_order_not_available(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer marks a seed order as NOT_AVAILABLE — bounces to the
    farmer for a re-route (cancel → new DRAFT → pick someone else)."""
    from app.modules.orders.router import _assert_active_dealer
    from app.services.order_events import record_event as _record_event

    await _assert_active_dealer(db, current_user.id)
    order = await _get_seed_order(db, order_id, current_user.id, farmer=False)
    if order.status not in [
        SeedOrderStatus.SENT, SeedOrderStatus.ACCEPTED, SeedOrderStatus.POSTPONED,
    ]:
        raise HTTPException(
            status_code=400,
            detail="Order cannot be marked Not Available in current status",
        )
    prev_status = order.status
    order.status = SeedOrderStatus.NOT_AVAILABLE
    await _record_event(
        db, lineage_id=order.lineage_id,
        event_type="MARKED_NOT_AVAILABLE",
        actor_user_id=current_user.id, actor_role="DEALER",
        seed_order_id=order.id,
        prev_status=prev_status,
        new_status=SeedOrderStatus.NOT_AVAILABLE.value,
    )
    await db.commit()
    return {"id": order_id, "status": order.status}


@router.put("/dealer/seed-orders/{order_id}/accept")
async def accept_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.modules.orders.router import _assert_active_dealer
    await _assert_active_dealer(db, current_user.id)
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
    from app.modules.orders.router import _assert_active_dealer
    await _assert_active_dealer(db, current_user.id)
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
    from app.modules.orders.router import _assert_active_dealer
    await _assert_active_dealer(db, current_user.id)
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
