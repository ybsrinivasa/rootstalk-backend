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
from app.services.i18n_cosh import get_locale, resolve_names_by_cosh_id

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
        cultivation_notes=data.get("cultivation_notes"),
        created_by_user_id=current_user.id,
    )
    db.add(variety)
    await db.commit()
    # Batch W-3 (2026-05-19) — re-fetch with pop_assignments eagerly
    # loaded so _variety_out doesn't trigger a lazy-load during
    # serialization. refresh() alone doesn't populate relationships.
    await db.refresh(variety, attribute_names=["pop_assignments"])
    # Phase T-2: description_points fan out to 12 locales via Claude.
    # Best-effort — never block the save.
    if variety.description_points:
        try:
            from app.tasks.translate_content import translate_field
            from app.modules.translations.models import EntityType
            translate_field.delay(
                EntityType.SEED_VARIETY_DESCRIPTION_POINTS, variety.id, "",
            )
        except Exception:
            pass
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
    description_changed = "description_points" in data
    for field in ["name", "variety_type", "description_points", "dus_characters", "photos", "cultivation_notes", "status"]:
        if field in data:
            setattr(variety, field, data[field])
    await db.commit()
    # Phase T-2: description_points fan out to 12 locales when the
    # bullets change. Hash check inside the task no-ops when the
    # content is unchanged (e.g. photos-only save).
    if description_changed and variety.description_points:
        try:
            from app.tasks.translate_content import translate_field
            from app.modules.translations.models import EntityType
            translate_field.delay(
                EntityType.SEED_VARIETY_DESCRIPTION_POINTS, variety.id, "",
            )
        except Exception:
            pass
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
    lang: str = Depends(get_locale),
):
    """Farmer browses varieties recommended for their subscription's PoP.

    DUS character rows on each variety carry `part_cosh_id`,
    `character_cosh_id`, and `descriptor_cosh_id` (snapshotted into
    `dus_characters` JSONB at SE-save time alongside `_name_en`
    fallbacks). Resolve those cosh_ids against the user's locale on
    the read path so the farmer sees ತೋಟಗಾರಿಕೆ vocabulary in Cosh's
    curated form rather than the snapshotted English. Falls through
    to `_name_en` when Cosh has no translation for the user's
    language (Latin binomials live exclusively in English by design).
    """
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

    cosh_ids: set[str] = set()
    for v in varieties:
        for r in (v.dus_characters or []):
            for k in ("part_cosh_id", "character_cosh_id", "descriptor_cosh_id"):
                cid = r.get(k)
                if cid:
                    cosh_ids.add(cid)
    names = await resolve_names_by_cosh_id(db, cosh_ids, lang) if cosh_ids else {}

    return [_variety_out(v, dus_names=names) for v in varieties]


# ── Farmer: Lookup a recipient by phone ───────────────────────────────────────
#
# Points 1 + 2 (2026-06-18). The seed-order picker used to be a
# pure pick-from-list — the farmer couldn't type a known dealer /
# facilitator's number. This endpoint backs the new phone-entry
# input on the seed-varieties picker: returns whether the looked-up
# user is eligible to receive THIS seed order, with localised
# state/district + role + photo so the farmer can confirm before
# sending.
#
# Variety-blind for safety: we do NOT echo `variety_name` or
# `variety_id` back to the farmer here (the farmer sees the variety
# on the variety-detail screen separately). This keeps the response
# shape symmetric with the facilitator/dealer surfaces' Point 4b
# rule, even though the farmer is allowed to see the variety
# elsewhere.

@router.get("/farmer/seed-orders/lookup-recipient")
async def lookup_seed_recipient(
    phone: str,
    variety_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    lang: str = Depends(get_locale),
):
    """Resolve a phone number for the seed-order recipient picker.

    Eligibility rules (locked 2026-06-18 audit):
      - FACILITATOR — always allowed, even when not onboarded by the
        variety's owning client. The brand-lock kicks in only on the
        facilitator's eventual onward route-to-dealer step.
      - DEALER — allowed only when onboarded as DEALER by the
        variety's owning client (ClientPromoter row, ACTIVE). Seed
        varieties are brand-locked.
      - Anything else (no DEALER / FACILITATOR ClientPromoter row,
        or the user is the caller themselves) — not eligible.

    Always returns 200 with structured payload so the PWA can render
    a friendly card per reason. The single exception is auth/missing
    params, which raise normally.
    """
    from app.modules.auth.service import get_user_by_phone
    from app.modules.clients.models import Client, ClientPromoter
    from app.modules.orders.router import _is_dealer_onboarded_by_client

    variety = (await db.execute(
        select(SeedVariety).where(SeedVariety.id == variety_id)
    )).scalar_one_or_none()
    if not variety:
        raise HTTPException(status_code=404, detail="Variety not found")
    client_id = variety.client_id

    # Phone normalisation mirrors /platform/lookup-user-by-phone:
    # strip non-digits, take last 10, prefix +91.
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) < 10:
        return {"found": False, "reason": "phone_not_registered", "phone": phone}
    normalised = "+91" + digits[-10:]
    target = await get_user_by_phone(db, normalised)
    if target is None:
        return {"found": False, "reason": "phone_not_registered", "phone": normalised}

    if target.id == current_user.id:
        return {
            "found": True,
            "user_id": target.id,
            "phone": target.phone,
            "name": target.name,
            "can_receive": False,
            "reason": "self",
        }

    # Resolve which (active) roles this user holds across all clients.
    cp_rows = (await db.execute(
        select(ClientPromoter.promoter_type, ClientPromoter.client_id)
        .where(
            ClientPromoter.user_id == target.id,
            ClientPromoter.promoter_type.in_(("FACILITATOR", "DEALER")),
            ClientPromoter.status == "ACTIVE",
        )
    )).all()
    roles_held = {ptype for (ptype, _cid) in cp_rows}
    is_active = bool(cp_rows)

    # Resolve the seed company name (for the reason copy) +
    # localised state / district names for the confirmation card.
    seed_company = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    client_name = (
        (seed_company.display_name or seed_company.short_name)
        if seed_company else None
    )

    loc_ids = {
        cid for cid in (target.state_cosh_id, target.district_cosh_id) if cid
    }
    loc_names = await resolve_names_by_cosh_id(db, loc_ids, lang) if loc_ids else {}

    base = {
        "found": True,
        "user_id": target.id,
        "phone": target.phone,
        "name": target.name,
        "photo_url": target.photo_url,
        "state_name": loc_names.get(target.state_cosh_id) if target.state_cosh_id else None,
        "district_name": loc_names.get(target.district_cosh_id) if target.district_cosh_id else None,
        "is_active": is_active,
        "client_name": client_name,
    }

    # Role precedence: prefer DEALER when onboarded by THIS client
    # (direct fulfilment path). Otherwise fall back to FACILITATOR
    # (permissive passthrough). Both held + dealer-not-onboarded:
    # treat as FACILITATOR — the farmer can still route through
    # them as a passthrough.
    if "DEALER" in roles_held and await _is_dealer_onboarded_by_client(
        db, target.id, client_id,
    ):
        return {
            **base,
            "role": "DEALER",
            "can_receive": True,
            "reason": "ok",
        }
    if "FACILITATOR" in roles_held:
        return {
            **base,
            "role": "FACILITATOR",
            "can_receive": True,
            "reason": "ok",
        }
    if "DEALER" in roles_held:
        # Dealer-only, not onboarded by this client → brand-lock blocks.
        return {
            **base,
            "role": "DEALER",
            "can_receive": False,
            "reason": "dealer_not_onboarded",
        }
    return {
        **base,
        "role": None,
        "can_receive": False,
        "reason": "not_dealer_or_facilitator",
    }


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

    # 2026-06-20 — Recipient guard. The POST endpoint defaults the
    # order status to SENT, so a request without dealer_user_id AND
    # without facilitator_user_id used to create an orphan SENT seed
    # with no recipient (surfaces on the farmer's Manage tab as
    # "Routed to: —"). DRAFT seed orders intentionally have both
    # recipient IDs null — those land via cancel-and-migrate, not
    # this endpoint. recipient = dealer XOR facilitator
    # (memory: feedback_order_recipient_mutual_exclusion.md).
    dealer_user_id = data.get("dealer_user_id")
    facilitator_user_id = data.get("facilitator_user_id")
    if not dealer_user_id and not facilitator_user_id:
        raise HTTPException(
            status_code=400,
            detail="Seed order must specify either dealer_user_id or facilitator_user_id",
        )
    if dealer_user_id and facilitator_user_id:
        raise HTTPException(
            status_code=400,
            detail="Seed order cannot specify both dealer_user_id and facilitator_user_id",
        )

    # Brand-lock guard (Point 3a, 2026-06-18). Seed varieties are
    # always brand-locked: a SEED order can only be sent to a dealer
    # who is onboarded by the variety's owning client. Facilitators
    # are exempt at the farmer→facilitator hop — the facilitator's
    # eventual onward route-to-dealer carries the same check (deferred
    # until those endpoints get built; see Point 3c in the audit).
    # Mirrors the pesticide/fertiliser pattern at
    # `orders/router.py:1583-1601`.
    if dealer_user_id:
        from app.modules.orders.router import _is_dealer_onboarded_by_client
        if not await _is_dealer_onboarded_by_client(
            db, dealer_user_id, variety.client_id,
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "locked_brand_requires_onboarded_dealer",
                    "message": (
                        "Seed varieties are brand-locked — this order "
                        "can only be sent to a dealer onboarded by the "
                        "seed company."
                    ),
                },
            )

    # 2026-06-19 — Human-readable Order ID. Same RT-YY-NNNNNN
    # format the regular Order uses, so the dealer's unified
    # orders feed shows one consistent identifier.
    from app.modules.orders.router import _generate_order_reference
    reference_number = await _generate_order_reference(db)
    order = SeedOrderFull(
        subscription_id=data["subscription_id"],
        farmer_user_id=current_user.id,
        variety_id=data["variety_id"],
        client_id=variety.client_id,
        dealer_user_id=dealer_user_id,
        facilitator_user_id=facilitator_user_id,
        reference_number=reference_number,
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return {
        "id": order.id, "status": order.status,
        "variety_id": order.variety_id,
        "reference_number": order.reference_number,
    }


@router.get("/farmer/seed-orders")
async def list_farmer_seed_orders(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.order_meta import (
        load_meta_for_subscription_ids, load_recipients,
    )

    result = await db.execute(
        select(SeedOrderFull).where(
            SeedOrderFull.farmer_user_id == current_user.id
        ).order_by(SeedOrderFull.created_at.desc())
    )
    orders = result.scalars().all()

    # Phase 1 of the farmer Orders restructure (2026-06-02): seed
    # cards get the same crop / company / start-date header rest of
    # the order surface uses, so the farmer reads them consistently.
    # NB: get_locale is a FastAPI Depends, not a callable; resolve
    # inline to match the rest of the codebase.
    lang = current_user.language_code or "en"
    meta_by_sub = await load_meta_for_subscription_ids(
        db, [o.subscription_id for o in orders],
        lang=lang,
    )
    recipients = await load_recipients(
        db,
        [o.dealer_user_id for o in orders],
        [o.facilitator_user_id for o in orders],
    )

    # 2026-06-19 — Bulk-resolve crop_cosh_id → localised crop_name so
    # the PWA's cropDisplayName helper renders the real crop name
    # instead of falling through to its UUID-safe "Crop" placeholder.
    # Mirror of the same fix applied to /dealer/seed-orders.
    variety_ids = [o.variety_id for o in orders]
    variety_by_id: dict[str, SeedVariety] = {}
    if variety_ids:
        rows = (await db.execute(
            select(SeedVariety).where(SeedVariety.id.in_(variety_ids))
        )).scalars().all()
        variety_by_id = {v.id: v for v in rows}
    crop_cosh_ids = {v.crop_cosh_id for v in variety_by_id.values() if v.crop_cosh_id}
    crop_name_by_id = await resolve_names_by_cosh_id(db, crop_cosh_ids, lang) if crop_cosh_ids else {}

    # 2026-07-06 — QR availability per row. Farmer's crop-detail
    # Received tab needs to render a Scan-to-Verify CTA on
    # PURCHASED seed cards, mirroring the pesticide/fertilizer path.
    # Batch-check whether at least one ACTIVE ProductQRCode exists
    # for (client_id, variety_id) so the whole list resolves in a
    # single query.
    from app.modules.qr.models import ProductQRCode as _ProductQRCode
    qr_ready_pairs: set[tuple[str, str]] = set()
    pairs_to_check = {(o.client_id, o.variety_id) for o in orders if o.variety_id}
    if pairs_to_check:
        client_ids_c = {c for c, _ in pairs_to_check}
        variety_ids_c = {v for _, v in pairs_to_check}
        qr_rows = (await db.execute(
            select(_ProductQRCode.client_id, _ProductQRCode.variety_id).where(
                _ProductQRCode.status == "ACTIVE",
                _ProductQRCode.client_id.in_(client_ids_c),
                _ProductQRCode.variety_id.in_(variety_ids_c),
            ).distinct()
        )).all()
        for cid, vid in qr_rows:
            qr_ready_pairs.add((cid, vid))

    out = []
    for o in orders:
        variety = variety_by_id.get(o.variety_id)
        meta = meta_by_sub.get(o.subscription_id)
        rcp = recipients.get(o.dealer_user_id) or recipients.get(o.facilitator_user_id)
        out.append({
            "id": o.id, "status": o.status,
            "reference_number": o.reference_number,
            "variety_name": variety.name if variety else None,
            "variety_id": o.variety_id,
            "crop_cosh_id": variety.crop_cosh_id if variety else None,
            "crop_name": (
                crop_name_by_id.get(variety.crop_cosh_id)
                if variety and variety.crop_cosh_id else None
            ),
            "unit": o.unit, "quantity": float(o.quantity) if o.quantity else None,
            "total_price": float(o.total_price) if o.total_price else None,
            "created_at": o.created_at,
            # Batch 14 additions for the farmer-side list page —
            # the recipient ids drive "Sent to X" hints + downstream
            # picker pre-population for DRAFT orders.
            "dealer_user_id": o.dealer_user_id,
            "facilitator_user_id": o.facilitator_user_id,
            "subscription_id": o.subscription_id,
            # 2026-07-06 — Scan-verify state so the Received tab can
            # render the same chip/CTA pair as the pesticide side.
            "scan_verified": bool(o.scan_verified),
            "qr_available": (
                bool(o.variety_id)
                and (o.client_id, o.variety_id) in qr_ready_pairs
            ),
            **(meta.to_dict() if meta else {}),
            **(rcp.to_dict() if rcp else {}),
        })
    return out


@router.get("/farmer/seed-orders/{order_id}")
async def get_farmer_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Detail for one seed order — drives /seed-orders/{id} on the
    farmer PWA. Surfaces enough to render the status badge, the
    qty/price when the dealer's submitted them, plus
    `subscription_id` so the DRAFT picker can fetch nearby
    recipients."""
    order = await _get_seed_order(db, order_id, current_user.id, farmer=True)
    variety = (await db.execute(
        select(SeedVariety).where(SeedVariety.id == order.variety_id)
    )).scalar_one_or_none()
    # 2026-06-19 — Resolve crop_name same way the list endpoint does.
    lang = current_user.language_code or "en"
    crop_name = None
    if variety and variety.crop_cosh_id:
        names = await resolve_names_by_cosh_id(db, {variety.crop_cosh_id}, lang)
        crop_name = names.get(variety.crop_cosh_id)
    # 2026-07-05 — Gate the PWA Scan CTA on whether the client has
    # actually generated at least one ACTIVE ProductQRCode for this
    # seed variety. Otherwise the farmer scans a package without a
    # rootsTALK QR, always mismatches, and gets confused.
    from app.modules.qr.models import ProductQRCode as _ProductQRCode
    qr_available = False
    if order.variety_id:
        qr_present = (await db.execute(
            select(_ProductQRCode.id).where(
                _ProductQRCode.client_id == order.client_id,
                _ProductQRCode.variety_id == order.variety_id,
                _ProductQRCode.status == "ACTIVE",
            ).limit(1)
        )).scalar_one_or_none()
        qr_available = qr_present is not None
    return {
        "id": order.id,
        "status": order.status,
        "reference_number": order.reference_number,
        "variety_id": order.variety_id,
        "variety_name": variety.name if variety else None,
        "crop_cosh_id": variety.crop_cosh_id if variety else None,
        "crop_name": crop_name,
        "unit": order.unit,
        "quantity": float(order.quantity) if order.quantity else None,
        "total_price": float(order.total_price) if order.total_price else None,
        "dealer_user_id": order.dealer_user_id,
        "facilitator_user_id": order.facilitator_user_id,
        "subscription_id": order.subscription_id,
        "client_id": order.client_id,
        "postponed_until": order.postponed_until,
        "scan_verified": order.scan_verified,
        "qr_available": qr_available,
        "created_at": order.created_at,
        # 2026-08-11 — Cancel-migrate marker (see orders/router.py).
        "is_returned_to_farmer": bool(getattr(order, "is_returned_to_farmer", False)),
        # 2026-08-12 — Chip text differentiator surfaced on /forward.
        "return_reason": getattr(order, "return_reason", None),
    }


@router.put("/farmer/seed-orders/{order_id}/approve")
async def approve_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await _get_seed_order(db, order_id, current_user.id, farmer=True)
    if order.status != SeedOrderStatus.SENT_FOR_APPROVAL:
        raise HTTPException(status_code=400, detail="Order is not awaiting approval")
    # 2026-06-19 — Park at READY_FOR_PICKUP instead of jumping
    # straight to PURCHASED. Dealer's "Packing / Hand over" pill
    # surfaces the order so they know to physically prepare it +
    # confirm pickup. Pre-fix the order vanished into Completed
    # the moment the farmer tapped Approve, with no signal to the
    # dealer.
    order.status = SeedOrderStatus.READY_FOR_PICKUP
    await db.commit()
    return {"id": order_id, "status": order.status}


@router.put("/dealer/seed-orders/{order_id}/handover")
async def handover_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer marks the seed packet as physically handed over to
    the farmer.

    Allowed only from `READY_FOR_PICKUP`. Lands at `PURCHASED`
    (terminal). Farmer has a parallel endpoint (`/mark-received`)
    that lands at the same terminal — whichever party taps first
    closes the order; the other side just sees it drop off their
    pill on the next refresh.
    """
    from app.modules.orders.router import _assert_active_dealer
    await _assert_active_dealer(db, current_user.id)
    order = await _get_seed_order(db, order_id, current_user.id, farmer=False)
    if order.status != SeedOrderStatus.READY_FOR_PICKUP:
        raise HTTPException(
            status_code=400,
            detail="Seed order can only be handed over from READY_FOR_PICKUP",
        )
    order.status = SeedOrderStatus.PURCHASED
    await db.commit()
    return {"id": order_id, "status": order.status}


@router.put("/farmer/seed-orders/{order_id}/mark-received")
async def mark_received_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer confirms they have physically picked up the seed
    packet from the dealer.

    Allowed only from `READY_FOR_PICKUP`. Lands at `PURCHASED`
    (terminal). Mirror of the dealer's `/handover` endpoint —
    whichever party taps first closes the order.
    """
    order = await _get_seed_order(db, order_id, current_user.id, farmer=True)
    if order.status != SeedOrderStatus.READY_FOR_PICKUP:
        raise HTTPException(
            status_code=400,
            detail="Seed order can only be marked received from READY_FOR_PICKUP",
        )
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


async def _check_seed_cancel_eligibility(order, db: AsyncSession) -> tuple[bool, str | None, str | None]:
    """Seed parity of _check_cancel_eligibility in orders/router.py."""
    from datetime import datetime, timezone
    terminal = {
        SeedOrderStatus.CANCELLED, SeedOrderStatus.PURCHASED,
        SeedOrderStatus.REROUTED, SeedOrderStatus.READY_FOR_PICKUP,
    }
    if order.status in terminal:
        return False, "already_terminal", f"Order is already {order.status}; nothing to cancel."
    now = datetime.now(timezone.utc)
    if order.dealer_viewing_until and order.dealer_viewing_until > now:
        return False, "dealer_currently_viewing", (
            "Your dealer is looking at this order right now. Please try again in a minute."
        )
    return True, None, None


@router.get("/farmer/seed-orders/{order_id}/eligible-recipients")
async def list_seed_eligible_recipients(
    order_id: str,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Seed parity of pest/fert /farmer/orders/{id}/eligible-recipients.

    Returns dealers + facilitators eligible to receive this seed order,
    ranked by distance. Seeds are always brand-locked to the variety's
    owning client, so dealers must be onboarded by that client;
    facilitators are always allowed (brand-lock kicks in on their
    onward route-to-dealer step). Powers the /seed-orders/[id]/forward
    picker page.
    """
    from app.modules.subscriptions.router import (
        nearby_dealers_for_farmer, nearby_facilitators_for_farmer,
    )
    order = await _get_seed_order(db, order_id, current_user.id, farmer=True)
    # Both nearby_* helpers return a plain list (per subscriptions/
    # router.py:6417 for dealers, mirrored on facilitators). Wrap
    # into the {dealers, facilitators} shape that the pest/fert
    # /eligible-recipients uses so the frontend can share code.
    dealers = await nearby_dealers_for_farmer(
        subscription_id=order.subscription_id,
        order_type="SEED",
        variety_id=order.variety_id,
        lat=lat, lng=lng,
        db=db, current_user=current_user,
    )
    facilitators = await nearby_facilitators_for_farmer(
        subscription_id=order.subscription_id,
        variety_id=order.variety_id,
        lat=lat, lng=lng,
        db=db, current_user=current_user,
    )
    return {
        "category": "SEED",
        "has_locked_brand": True,
        "locked_brand_explainer": None,
        "dealers": dealers if isinstance(dealers, list) else [],
        "facilitators": facilitators if isinstance(facilitators, list) else [],
    }


@router.get("/farmer/seed-orders/{order_id}/cancel-eligibility")
async def cancel_seed_eligibility(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Tap-time eligibility check for the farmer's Cancel button on
    seed orders. See regular-order counterpart for the full rationale."""
    order = await _get_seed_order(db, order_id, current_user.id, farmer=True)
    can_cancel, code, message = await _check_seed_cancel_eligibility(order, db)
    return {"can_cancel": can_cancel, "code": code, "message": message}


@router.put("/farmer/seed-orders/{order_id}/cancel")
async def cancel_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer cancels a seed order (Phase 2 rework, 2026-08-14). Model
    B DRAFT flow unwound — source flagged returned-to-farmer, status
    → NOT_AVAILABLE. No DRAFT row created (there was never one for
    seed since seed is single-item, but the cancel-and-flip-to-DRAFT
    dance is gone).

    Cancel is BLOCKED when:
      - Order is already terminal (CANCELLED / PURCHASED / REROUTED)
      - Order is READY_FOR_PICKUP AND final_confirmed_at IS NOT NULL
        (dealer has committed physically; cancel would void a real
        transaction — admin intervention needed for edge cases)
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
    if (
        order.status == SeedOrderStatus.READY_FOR_PICKUP
        and order.final_confirmed_at is not None
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "final_confirmed_cannot_cancel",
                "message": "This order has been Final Confirmed by the dealer for pickup. Contact the dealer to resolve.",
            },
        )
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if order.dealer_viewing_until and order.dealer_viewing_until > now:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "dealer_currently_viewing",
                "message": "Your dealer is looking at this order right now. Please try again in a minute.",
            },
        )

    prev_status = order.status
    prev_dealer = order.dealer_user_id
    prev_facilitator = order.facilitator_user_id

    order.status = SeedOrderStatus.NOT_AVAILABLE
    order.postponed_until = None
    order.is_returned_to_farmer = True
    if prev_dealer and not order.released_dealer_user_id:
        order.released_dealer_user_id = prev_dealer
    if prev_facilitator and not order.released_facilitator_user_id:
        order.released_facilitator_user_id = prev_facilitator
    order.return_reason = 'farmer_cancel'

    await _record_event(
        db, lineage_id=order.lineage_id,
        event_type="RETURNED_TO_FARMER_BY_CANCEL",
        actor_user_id=current_user.id, actor_role="FARMER",
        seed_order_id=order.id,
        prev_status=prev_status,
        new_status=SeedOrderStatus.NOT_AVAILABLE.value,
        metadata={
            "released_dealer_user_id": prev_dealer,
            "released_facilitator_user_id": prev_facilitator,
        },
    )

    await db.commit()
    return {
        "id": order_id,
        "status": order.status,
        "is_returned_to_farmer": True,
    }


@router.put("/farmer/seed-orders/{order_id}/discard")
async def discard_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer's Discard on a returned-to-farmer seed order (Phase 2
    rework, 2026-08-14). Fate decision — flips status to CANCELLED
    and clears is_returned_to_farmer."""
    from app.services.order_events import record_event as _record_event

    order = await _get_seed_order(db, order_id, current_user.id, farmer=True)
    if not order.is_returned_to_farmer:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "not_returned_to_farmer",
                "message": "This seed order is not currently back with you for a fate decision.",
            },
        )
    if order.status == SeedOrderStatus.CANCELLED:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "already_cancelled",
                "message": "Seed order is already cancelled.",
            },
        )

    prev_status = order.status
    order.status = SeedOrderStatus.CANCELLED
    order.is_returned_to_farmer = False
    await _record_event(
        db, lineage_id=order.lineage_id,
        event_type="DISCARDED_BY_FARMER",
        actor_user_id=current_user.id, actor_role="FARMER",
        seed_order_id=order.id,
        prev_status=prev_status,
        new_status=SeedOrderStatus.CANCELLED.value,
        metadata={"phase2": True},
    )
    await db.commit()
    return {"id": order.id, "status": order.status}


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
    # 2026-08-11 — Clear the cancel-migrate marker once the DRAFT is
    # sent so it no longer surfaces on the Returned pill. Only
    # meaningful while the farmer is deciding forward-or-discard.
    # Drop the released-from hint + return-reason too — informational
    # only, and the new recipient replaces the "with X" context.
    order.is_returned_to_farmer = False
    order.released_dealer_user_id = None
    order.released_facilitator_user_id = None
    order.return_reason = None
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
    lang: str = Depends(get_locale),
):
    from app.modules.orders.router import _assert_active_dealer
    from app.modules.clients.models import Client
    await _assert_active_dealer(db, current_user.id)
    result = await db.execute(
        select(SeedOrderFull).where(
            SeedOrderFull.dealer_user_id == current_user.id,
            SeedOrderFull.status.notin_([SeedOrderStatus.CANCELLED]),
        ).order_by(SeedOrderFull.created_at.desc())
    )
    orders = result.scalars().all()
    # 2026-06-06 — Per-order details enriched with
    # farmer_phone / farmer_photo_url / client_name and the explicit
    # category sentinel "SEED" so the unified dealer-orders feed can
    # render seed cards through the same OrderHeaderRow component
    # used for regular inputs orders. Batched lookups (one query per
    # FK across the page) keep the N+1 in check.
    variety_ids = {o.variety_id for o in orders}
    farmer_ids = {o.farmer_user_id for o in orders}
    sub_ids = {o.subscription_id for o in orders}
    client_ids = {o.client_id for o in orders}

    varieties: dict[str, SeedVariety] = {}
    if variety_ids:
        varieties = {
            v.id: v for v in (await db.execute(
                select(SeedVariety).where(SeedVariety.id.in_(variety_ids))
            )).scalars().all()
        }
    farmers: dict[str, User] = {}
    if farmer_ids:
        farmers = {
            u.id: u for u in (await db.execute(
                select(User).where(User.id.in_(farmer_ids))
            )).scalars().all()
        }
    subs: dict[str, Subscription] = {}
    if sub_ids:
        subs = {
            s.id: s for s in (await db.execute(
                select(Subscription).where(Subscription.id.in_(sub_ids))
            )).scalars().all()
        }
    clients: dict[str, Client] = {}
    if client_ids:
        clients = {
            c.id: c for c in (await db.execute(
                select(Client).where(Client.id.in_(client_ids))
            )).scalars().all()
        }

    # 2026-06-19 — Resolve crop_cosh_id → localised crop name.
    # Pre-fix the response shipped only `crop_cosh_id`; the PWA's
    # `cropDisplayName` helper sees a UUID and falls back to the
    # neutral "Crop" placeholder, leaving the dealer card without
    # the actual crop. Bulk-resolve via the i18n_cosh helper.
    crop_cosh_ids = {v.crop_cosh_id for v in varieties.values() if v.crop_cosh_id}
    crop_name_by_id = await resolve_names_by_cosh_id(
        db, crop_cosh_ids, lang,
    ) if crop_cosh_ids else {}

    # 2026-07-08 — Resolve each variety's crop_cosh_id to its
    # AREA_WISE vs PLANT_WISE measure so the dealer card can render
    # acreage vs plant count consistently with the farmer-side crop
    # dashboard. Bulk lookup — one query per unique crop_cosh_id
    # keeps the N+1 gone from this endpoint.
    from app.services.cosh_crop_view import get_measure_for_biological_name
    from app.modules.subscriptions.router import _compute_crop_age
    measure_by_cosh_id: dict[str, str] = {}
    for cid in crop_cosh_ids:
        m = await get_measure_for_biological_name(db, cid)
        measure_by_cosh_id[cid] = m or "AREA_WISE"

    # Point 4a (2026-06-18): the dealer cannot see the variety name
    # until they accept the order. Seed varieties are brand-locked,
    # and exposing the name pre-accept would let a non-onboarded
    # dealer fish for brands. Pre-accept = `SENT` (or `DRAFT`, which
    # only exists transiently on cancel-and-migrate and wouldn't
    # carry a dealer assignment in practice, but guarded anyway).
    PRE_ACCEPT_STATUSES = {
        SeedOrderStatus.SENT.value,
        SeedOrderStatus.DRAFT.value,
    }
    out = []
    for o in orders:
        variety = varieties.get(o.variety_id)
        farmer = farmers.get(o.farmer_user_id)
        sub = subs.get(o.subscription_id)
        client = clients.get(o.client_id)
        variety_hidden = o.status in PRE_ACCEPT_STATUSES
        crop_cosh_id = variety.crop_cosh_id if variety else None
        crop_measure = (
            measure_by_cosh_id.get(crop_cosh_id) if crop_cosh_id else None
        )
        computed_crop_age = (
            _compute_crop_age(sub, crop_measure) if sub and crop_measure else None
        )
        out.append({
            "id": o.id, "status": o.status,
            "reference_number": o.reference_number,
            "category": "SEED",
            "variety_name": (
                None if variety_hidden
                else (variety.name if variety else None)
            ),
            "variety_name_hidden": variety_hidden,
            "crop_cosh_id": crop_cosh_id,
            "crop_name": (
                crop_name_by_id.get(crop_cosh_id) if crop_cosh_id else None
            ),
            # 2026-07-08 — Farmer-side crop context surfaced to the
            # dealer so they can confirm the order against the crop
            # before packing. `crop_measure` drives whether the PWA
            # renders acreage or plant count; `computed_crop_age`
            # matches the envelope shape the crop dashboard already
            # uses (source-agnostic; PWA renders `value unit`).
            "crop_measure": crop_measure,
            "computed_crop_age": computed_crop_age,
            "farmer_user_id": o.farmer_user_id,
            "farmer_name": farmer.name if farmer else None,
            "farmer_phone": farmer.phone if farmer else None,
            "farmer_photo_url": farmer.photo_url if farmer else None,
            "farm_area_acres": float(sub.farm_area_acres) if sub and sub.farm_area_acres else None,
            "number_of_plants": int(sub.number_of_plants) if sub and sub.number_of_plants else None,
            "client_id": o.client_id,
            "client_name": (client.display_name or client.short_name) if client else None,
            "unit": o.unit,
            "quantity": float(o.quantity) if o.quantity else None,
            "total_price": float(o.total_price) if o.total_price else None,
            "created_at": o.created_at,
            # 2026-08-14 (Phase 2 rework): Final Confirmation timestamp
            # on seed. Dealer's PWA renders "Final Confirmation" vs
            # "Handover" based on whether this is null.
            "final_confirmed_at": (
                o.final_confirmed_at.isoformat() if o.final_confirmed_at else None
            ),
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
    """Dealer declines a seed order.

    2026-08-12 — Routes through the same returned-to-farmer plumbing
    that cancel-migrate uses (DRAFT + is_returned_to_farmer=True +
    released_dealer_user_id + return_reason='dealer_declined'). Effect
    is that the farmer's Manage tab picks it up on the Returned pill
    with the standard two-button UI (Send to another dealer /
    Don't need these now), same as for a farmer-cancelled seed. Chip
    reads "DECLINED BY DEALER · from [Dealer]" via return_reason.

    Old behaviour just flipped status → NOT_AVAILABLE and left the
    dealer on the row; the farmer's Send-to-another-dealer tap 404'd
    (routed to the pest/fert /forward URL which doesn't understand
    seed IDs).
    """
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
    prev_dealer = order.dealer_user_id
    prev_facilitator = order.facilitator_user_id
    facilitator_owns = prev_facilitator is not None
    # 2026-08-14 (Phase 2 rework): flag-flip only, no DRAFT reset. Both
    # branches keep dealer_user_id / facilitator_user_id intact (queue
    # filters on the flags, not on the FK nullability). Order status
    # goes to NOT_AVAILABLE (unified unsold-state for seed).
    order.status = SeedOrderStatus.NOT_AVAILABLE
    order.postponed_until = None
    if facilitator_owns:
        order.is_returned_to_facilitator = True
        if prev_dealer and not order.released_dealer_user_id:
            order.released_dealer_user_id = prev_dealer
    else:
        order.is_returned_to_farmer = True
        order.return_reason = 'dealer_declined'
        if prev_dealer and not order.released_dealer_user_id:
            order.released_dealer_user_id = prev_dealer
        if prev_facilitator and not order.released_facilitator_user_id:
            order.released_facilitator_user_id = prev_facilitator
    await _record_event(
        db, lineage_id=order.lineage_id,
        event_type="DECLINED_BY_DEALER",
        actor_user_id=current_user.id, actor_role="DEALER",
        seed_order_id=order.id,
        prev_status=prev_status,
        new_status=order.status.value if hasattr(order.status, 'value') else order.status,
        metadata={
            "released_dealer_user_id": prev_dealer,
            "released_facilitator_user_id": prev_facilitator,
            "returned_to_farmer": not facilitator_owns,
            "returned_to_facilitator": facilitator_owns,
        },
    )
    await db.commit()
    return {
        "id": order_id, "status": order.status,
        "is_returned_to_farmer": not facilitator_owns,
        "is_returned_to_facilitator": facilitator_owns,
    }


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


# 2026-08-14 — Final Confirmation for seed orders (Phase 2 rework).
# Farmer's approval takes the order to READY_FOR_PICKUP with a null
# final_confirmed_at. Dealer stamps final_confirmed_at when payment /
# credit is settled → farmer's Pickup pill fires. See the equivalent
# `final_confirm_item` in orders/router.py for the pest/fert path.
@router.put("/dealer/seed-orders/{order_id}/final-confirm")
async def seed_final_confirm(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from datetime import datetime, timezone
    from app.modules.orders.router import _assert_active_dealer
    await _assert_active_dealer(db, current_user.id)
    order = await _get_seed_order(db, order_id, current_user.id, farmer=False)
    if order.status != SeedOrderStatus.READY_FOR_PICKUP:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "not_approved",
                "message": "Only farmer-approved seed orders can be Final Confirmed.",
            },
        )
    if order.final_confirmed_at is not None:
        return {"id": order_id, "final_confirmed_at": order.final_confirmed_at}
    order.final_confirmed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": order_id, "final_confirmed_at": order.final_confirmed_at}


@router.put("/dealer/seed-orders/{order_id}/cancel-final-confirm")
async def seed_cancel_final_confirm(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dealer's back-out on a farmer-approved seed order (before Final
    Confirmation). Status → NOT_AVAILABLE, joins the wrapper. Common
    reason: payment / credit didn't come through."""
    from app.modules.orders.router import _assert_active_dealer
    await _assert_active_dealer(db, current_user.id)
    order = await _get_seed_order(db, order_id, current_user.id, farmer=False)
    if order.status != SeedOrderStatus.READY_FOR_PICKUP:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "not_approved",
                "message": "Cancel-final-confirm only applies to farmer-approved seed orders.",
            },
        )
    if order.final_confirmed_at is not None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "already_final_confirmed",
                "message": "This order is already Final Confirmed and can no longer be cancelled — the farmer is expecting pickup.",
            },
        )
    order.status = SeedOrderStatus.NOT_AVAILABLE
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


# ── Dealer: presence heartbeat (2026-08-11, seed parity) ──────────────────────
#
# Mirror of `/dealer/orders/{oid}/heartbeat` (Orders V2 Batch 2) on the
# seed side. Ping every ~20 s while the dealer's seed-order detail
# screen is mounted; each call extends `dealer_viewing_until` by ~30 s.
# The farmer's cancel refuses while the lease is in the future. No
# dealer seed-order detail screen exists in the PWA today — this
# endpoint is provisioned so the future screen just wires the mount
# effect and the gate goes live automatically.

_DEALER_SEED_VIEWING_LEASE_SECONDS = 30

@router.put("/dealer/seed-orders/{order_id}/heartbeat")
async def dealer_seed_heartbeat(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from datetime import datetime, timedelta, timezone
    from app.modules.orders.router import _assert_active_dealer
    await _assert_active_dealer(db, current_user.id)
    order = await _get_seed_order(db, order_id, current_user.id, farmer=False)
    order.dealer_viewing_until = (
        datetime.now(timezone.utc) + timedelta(seconds=_DEALER_SEED_VIEWING_LEASE_SECONDS)
    )
    await db.commit()
    return {
        "viewing_until": order.dealer_viewing_until.isoformat(),
        "lease_seconds": _DEALER_SEED_VIEWING_LEASE_SECONDS,
    }


# ── Facilitator: variety-blind passthrough surface ────────────────────────────
#
# Point 3c + 4b (2026-06-18). The farmer can route a seed order to a
# facilitator without the facilitator being onboarded by the variety's
# owning client (Point 3b — permissive on the farmer→facilitator hop).
# When the facilitator forwards onward to a dealer, the same brand-lock
# rule that fires on the direct farmer→dealer path kicks in here:
# dealer must be onboarded by `order.client_id`.
#
# The variety name + id are NEVER exposed on the facilitator surface —
# not in the list, not in detail, not even after the facilitator
# accepts. The facilitator is a routing role for seeds; the brand
# secrecy is what makes brand-lock meaningful (otherwise a
# non-onboarded facilitator could fish for variety names just by
# accepting orders).


def _seed_for_facilitator_payload(
    o: "SeedOrderFull",
    farmer: Optional[User] = None,
    sub: Optional[Subscription] = None,
    client: Optional["Client"] = None,
    dealer: Optional[User] = None,
) -> dict:
    """Variety-blind response shape for the facilitator surface.

    Deliberately omits `variety_name` AND `variety_id`. The
    facilitator gets enough to ring the farmer and pick a dealer —
    crop name (via crop_cosh_id resolves to a generic Chilli / Tomato
    label), farmer contact info, the client (seed company) name, and
    farm area for context.

    `reference_number` + `dealer_name` were added 2026-06-22 so the
    seed card can be inlined into /facilitator/orders alongside
    regular orders, matching the dealer-PWA parity. Both are null when
    not yet assigned.
    """
    return {
        "id": o.id,
        "status": o.status,
        "reference_number": o.reference_number,
        "category": "SEED",
        "crop_cosh_id": None,  # set by caller after variety lookup
        "farmer_user_id": o.farmer_user_id,
        "farmer_name": farmer.name if farmer else None,
        "farmer_phone": farmer.phone if farmer else None,
        "farmer_photo_url": farmer.photo_url if farmer else None,
        "farm_area_acres": float(sub.farm_area_acres) if sub and sub.farm_area_acres else None,
        "client_id": o.client_id,
        "client_name": (client.display_name or client.short_name) if client else None,
        "dealer_user_id": o.dealer_user_id,
        "dealer_name": dealer.name if dealer else None,
        "created_at": o.created_at,
        # 2026-08-12 — Facilitator-side returned marker (see Order model).
        "is_returned_to_facilitator": bool(getattr(o, "is_returned_to_facilitator", False)),
        "released_dealer_user_id": getattr(o, "released_dealer_user_id", None),
    }


@router.get("/facilitator/seed-orders")
async def list_facilitator_seed_orders(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Seed orders the farmer routed to this facilitator. Variety
    name + id are stripped from the payload — see module note above."""
    from app.modules.orders.router import _assert_active_facilitator
    from app.modules.clients.models import Client
    await _assert_active_facilitator(db, current_user.id)
    result = await db.execute(
        select(SeedOrderFull).where(
            SeedOrderFull.facilitator_user_id == current_user.id,
            SeedOrderFull.status.notin_([SeedOrderStatus.CANCELLED]),
        ).order_by(SeedOrderFull.created_at.desc())
    )
    orders = result.scalars().all()

    variety_ids = {o.variety_id for o in orders}
    farmer_ids = {o.farmer_user_id for o in orders}
    sub_ids = {o.subscription_id for o in orders}
    client_ids = {o.client_id for o in orders}

    # We still load the variety — but only to resolve the crop_cosh_id
    # for the response. The variety's `name` never leaves this scope.
    varieties: dict[str, SeedVariety] = {}
    if variety_ids:
        varieties = {
            v.id: v for v in (await db.execute(
                select(SeedVariety).where(SeedVariety.id.in_(variety_ids))
            )).scalars().all()
        }
    farmers: dict[str, User] = {}
    if farmer_ids:
        farmers = {
            u.id: u for u in (await db.execute(
                select(User).where(User.id.in_(farmer_ids))
            )).scalars().all()
        }
    subs: dict[str, Subscription] = {}
    if sub_ids:
        subs = {
            s.id: s for s in (await db.execute(
                select(Subscription).where(Subscription.id.in_(sub_ids))
            )).scalars().all()
        }
    clients: dict[str, Client] = {}
    if client_ids:
        clients = {
            c.id: c for c in (await db.execute(
                select(Client).where(Client.id.in_(client_ids))
            )).scalars().all()
        }

    # Batch-fetch dealers for orders that have been routed onwards.
    # The card's Routed pill shows "to <dealer name>" so the facilitator
    # remembers where the order went.
    dealer_ids = {o.dealer_user_id for o in orders if o.dealer_user_id}
    dealers: dict[str, User] = {}
    if dealer_ids:
        dealers = {
            u.id: u for u in (await db.execute(
                select(User).where(User.id.in_(dealer_ids))
            )).scalars().all()
        }

    out = []
    for o in orders:
        variety = varieties.get(o.variety_id)
        payload = _seed_for_facilitator_payload(
            o,
            farmer=farmers.get(o.farmer_user_id),
            sub=subs.get(o.subscription_id),
            client=clients.get(o.client_id),
            dealer=dealers.get(o.dealer_user_id) if o.dealer_user_id else None,
        )
        payload["crop_cosh_id"] = variety.crop_cosh_id if variety else None
        out.append(payload)
    return out


async def _get_facilitator_seed_order(
    db: AsyncSession, order_id: str, facilitator_user_id: str,
) -> SeedOrderFull:
    order = (await db.execute(
        select(SeedOrderFull).where(
            SeedOrderFull.id == order_id,
            SeedOrderFull.facilitator_user_id == facilitator_user_id,
        )
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Seed order not found")
    return order


@router.put("/facilitator/seed-orders/{order_id}/accept")
async def facilitator_accept_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator accepts the routing. Status SENT → ACCEPTED.

    Per user spec 2026-06-18: a facilitator CAN accept a seed order
    even when not onboarded by the variety's owning client. The
    brand-lock check fires only on the onward route-to-dealer step.
    """
    from app.modules.orders.router import _assert_active_facilitator
    await _assert_active_facilitator(db, current_user.id)
    order = await _get_facilitator_seed_order(db, order_id, current_user.id)
    if order.status != SeedOrderStatus.SENT:
        raise HTTPException(
            status_code=400,
            detail="Seed order can only be accepted from SENT status",
        )
    order.status = SeedOrderStatus.ACCEPTED
    await db.commit()
    return {"id": order_id, "status": order.status}


@router.put("/facilitator/seed-orders/{order_id}/reject")
async def facilitator_reject_seed_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator declines a seed order.

    2026-08-12 — Routes through the same returned-to-farmer plumbing
    as farmer cancel + dealer decline: DRAFT + is_returned_to_farmer=
    True + released_facilitator_user_id + return_reason=
    'facilitator_declined'. Farmer's Manage tab picks it up on the
    Returned pill with the standard two-button UI. Chip reads
    "DECLINED BY FACILITATOR · from [Facilitator]".

    Old behaviour just flipped status → REJECTED (terminal). Farmer
    then had to manually cancel to migrate the intent, which was
    friction; now the migrate happens at reject-time.
    """
    from app.modules.orders.router import _assert_active_facilitator
    from app.services.order_events import record_event as _record_event

    await _assert_active_facilitator(db, current_user.id)
    order = await _get_facilitator_seed_order(db, order_id, current_user.id)
    if order.status not in (SeedOrderStatus.SENT, SeedOrderStatus.ACCEPTED):
        raise HTTPException(
            status_code=400,
            detail="Seed order can only be rejected from SENT or ACCEPTED",
        )
    prev_status = order.status
    prev_facilitator = order.facilitator_user_id
    # 2026-08-14 (Phase 2 rework): flag-flip only, no DRAFT reset.
    order.status = SeedOrderStatus.NOT_AVAILABLE
    order.postponed_until = None
    order.is_returned_to_farmer = True
    if prev_facilitator and not order.released_facilitator_user_id:
        order.released_facilitator_user_id = prev_facilitator
    order.return_reason = 'facilitator_declined'
    await _record_event(
        db, lineage_id=order.lineage_id,
        event_type="DECLINED_BY_FACILITATOR",
        actor_user_id=current_user.id, actor_role="FACILITATOR",
        seed_order_id=order.id,
        prev_status=prev_status,
        new_status=SeedOrderStatus.NOT_AVAILABLE.value,
        metadata={
            "released_facilitator_user_id": prev_facilitator,
        },
    )
    await db.commit()
    return {"id": order_id, "status": order.status, "is_returned_to_farmer": True}


@router.get("/facilitator/seed-orders/{order_id}/nearby-dealers")
async def facilitator_seed_order_nearby_dealers(
    order_id: str,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Brand-lock-filtered dealer picker for the facilitator's
    forward step. Reads `order.client_id` and drops any dealer not
    onboarded by that client — same invariant the farmer-side
    picker enforces (`subscriptions/router.py::nearby_dealers_for_farmer`
    with `variety_id`)."""
    from app.modules.orders.router import _assert_active_facilitator
    from app.modules.clients.models import ClientPromoter
    from app.modules.subscriptions.router import _haversine_sub
    await _assert_active_facilitator(db, current_user.id)
    order = await _get_facilitator_seed_order(db, order_id, current_user.id)

    fac_lat = lat or (float(current_user.gps_lat) if current_user.gps_lat else 0.0)
    fac_lng = lng or (float(current_user.gps_lng) if current_user.gps_lng else 0.0)

    onboarded_rows = (await db.execute(
        select(ClientPromoter.user_id).where(
            ClientPromoter.client_id == order.client_id,
            ClientPromoter.promoter_type == "DEALER",
            ClientPromoter.status == "ACTIVE",
        )
    )).scalars().all()
    onboarded_dealer_ids = set(onboarded_rows)
    if not onboarded_dealer_ids:
        return []

    from app.modules.orders.models import DealerProfile
    profiles = (await db.execute(
        select(DealerProfile).where(DealerProfile.user_id.in_(onboarded_dealer_ids))
    )).scalars().all()

    results = []
    for profile in profiles:
        if "SEEDS" not in (profile.sell_categories or []):
            continue
        if not profile.shop_gps_lat or not profile.shop_gps_lng:
            continue
        dist = _haversine_sub(fac_lat, fac_lng,
                              float(profile.shop_gps_lat), float(profile.shop_gps_lng))
        dealer = (await db.execute(
            select(User).where(User.id == profile.user_id)
        )).scalar_one_or_none()
        if dealer:
            results.append({
                "user_id": dealer.id,
                "name": dealer.name,
                "phone": dealer.phone,
                "shop_name": profile.shop_name,
                "shop_address": profile.shop_address,
                "distance_km": round(dist, 1),
            })

    results.sort(key=lambda x: x["distance_km"])
    return results[:5]


@router.put("/facilitator/seed-orders/{order_id}/route-to-dealer")
async def facilitator_route_seed_to_dealer(
    order_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Facilitator forwards the seed order to a chosen dealer.

    Brand-lock guard: dealer must be onboarded by `order.client_id`.
    Mirrors the write-time check on
    `POST /farmer/seed-orders` so a direct API call can't bypass
    the picker's filter.

    Status flips to SENT after re-assignment so the dealer sees an
    Accept / Decline decision — same shape the
    `route-to-dealer` on the regular-orders side uses
    (orders/router.py:3822-3825, 2026-06-09 design).
    """
    from app.modules.orders.router import (
        _assert_active_dealer,
        _assert_active_facilitator,
        _is_dealer_onboarded_by_client,
    )
    await _assert_active_facilitator(db, current_user.id)
    order = await _get_facilitator_seed_order(db, order_id, current_user.id)
    if order.status not in (SeedOrderStatus.SENT, SeedOrderStatus.ACCEPTED):
        raise HTTPException(
            status_code=400,
            detail="Seed order cannot be routed in current status",
        )
    dealer_user_id = data.get("dealer_user_id")
    if not dealer_user_id:
        raise HTTPException(status_code=422, detail="dealer_user_id required")
    await _assert_active_dealer(db, dealer_user_id)
    if not await _is_dealer_onboarded_by_client(
        db, dealer_user_id, order.client_id,
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "locked_brand_requires_onboarded_dealer",
                "message": (
                    "Seed varieties are brand-locked — this order "
                    "can only be forwarded to a dealer onboarded by "
                    "the seed company."
                ),
            },
        )
    order.dealer_user_id = dealer_user_id
    order.status = SeedOrderStatus.SENT
    # 2026-08-12 — Clear the returned-to-facilitator marker on forward
    # (parity with pest/fert route_order_to_dealer).
    order.is_returned_to_facilitator = False
    order.released_dealer_user_id = None
    await db.commit()
    return {
        "id": order_id,
        "status": order.status,
        "dealer_user_id": order.dealer_user_id,
    }


@router.get("/facilitator/seed-orders/{order_id}/lookup-dealer")
async def facilitator_lookup_dealer_for_seed_order(
    order_id: str,
    phone: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    lang: str = Depends(get_locale),
):
    """Facilitator-side phone lookup for the
    `/facilitator/seed-orders/{id}/route-to-dealer` flow. Mirrors
    `/facilitator/orders/{id}/lookup-dealer` but with the
    seed-flow rule baked in: every seed variety is brand-locked,
    so the dealer must always be onboarded by `order.client_id`.

    Variety-blind per Point 4b — no variety_name / variety_id
    appears in the response. The facilitator never sees the
    underlying variety, even at the dealer-picking step.
    """
    from app.modules.orders.router import (
        _assert_active_facilitator,
        _is_dealer_onboarded_by_client,
    )
    from app.modules.auth.service import get_user_by_phone
    from app.modules.clients.models import Client, ClientPromoter

    await _assert_active_facilitator(db, current_user.id)
    order = await _get_facilitator_seed_order(db, order_id, current_user.id)

    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) < 10:
        return {"found": False, "reason": "phone_not_registered", "phone": phone}
    normalised = "+91" + digits[-10:]
    target = await get_user_by_phone(db, normalised)
    if target is None:
        return {"found": False, "reason": "phone_not_registered", "phone": normalised}

    if target.id == current_user.id:
        return {
            "found": True, "user_id": target.id, "phone": target.phone,
            "name": target.name, "can_receive": False, "reason": "self",
        }

    cp_rows = (await db.execute(
        select(ClientPromoter.promoter_type)
        .where(
            ClientPromoter.user_id == target.id,
            ClientPromoter.promoter_type.in_(("FACILITATOR", "DEALER")),
            ClientPromoter.status == "ACTIVE",
        )
    )).scalars().all()
    roles_held = set(cp_rows)
    is_active = bool(cp_rows)

    company = (await db.execute(
        select(Client).where(Client.id == order.client_id)
    )).scalar_one_or_none()
    client_name = (company.display_name or company.short_name) if company else None

    loc_ids = {cid for cid in (target.state_cosh_id, target.district_cosh_id) if cid}
    loc_names = await resolve_names_by_cosh_id(db, loc_ids, lang) if loc_ids else {}

    base = {
        "found": True,
        "user_id": target.id,
        "phone": target.phone,
        "name": target.name,
        "photo_url": target.photo_url,
        "state_name": loc_names.get(target.state_cosh_id) if target.state_cosh_id else None,
        "district_name": loc_names.get(target.district_cosh_id) if target.district_cosh_id else None,
        "is_active": is_active,
        "client_name": client_name,
        "has_locked_brand": True,  # always True for seeds
    }

    if "DEALER" not in roles_held:
        return {**base, "role": None, "can_receive": False,
                "reason": "not_dealer_or_facilitator"}

    if not await _is_dealer_onboarded_by_client(
        db, target.id, order.client_id,
    ):
        return {**base, "role": "DEALER", "can_receive": False,
                "reason": "dealer_not_onboarded"}

    return {**base, "role": "DEALER", "can_receive": True, "reason": "ok"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _variety_out(v: SeedVariety, dus_names: Optional[dict[str, str]] = None) -> dict:
    return {
        "id": v.id,
        "client_id": v.client_id,
        "crop_cosh_id": v.crop_cosh_id,
        "name": v.name,
        "variety_type": v.variety_type,
        "description_points": v.description_points or [],
        "dus_characters": _localise_dus_rows(v.dus_characters, dus_names),
        "photos": v.photos or [],
        "cultivation_notes": v.cultivation_notes,
        "status": v.status,
        "pop_assignments": [{"package_id": a.package_id, "status": a.status}
                            for a in (v.pop_assignments or [])],
    }


def _localise_dus_rows(
    rows: Optional[list[dict]],
    names: Optional[dict[str, str]],
) -> Optional[list[dict]]:
    """Inject localised `part_name` / `character_name` / `descriptor_name`
    alongside the existing `_name_en` snapshots. PWA prefers the
    plain `_name` field; falls through to `_name_en` when a cosh_id
    has no translation for the user's locale (e.g. Latin binomials).
    Returns a NEW list — does not mutate the JSONB column."""
    if not rows:
        return rows
    if not names:
        names = {}
    out = []
    for r in rows:
        copy = dict(r)
        for field, en_field in (
            ("part_name", "part_name_en"),
            ("character_name", "character_name_en"),
            ("descriptor_name", "descriptor_name_en"),
        ):
            cid_field = field.replace("_name", "_cosh_id")
            cid = r.get(cid_field)
            copy[field] = (names.get(cid) if cid else None) or r.get(en_field)
        out.append(copy)
    return out


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
