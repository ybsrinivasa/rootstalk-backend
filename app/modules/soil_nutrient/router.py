"""SA Portal API — Soil Nutrient Management module (Phase 1).

Platform-wide CRUD for seeded reference data + Client flag toggles.
Permissions:
  - Client flag toggles: SA only (email == settings.sa_email).
    Enforcement: cannot enable on is_coaching=True Clients (both
    flags) or is_training=True Clients (soil_nutrient_module_enabled
    only). See Decisions L (coaching guard) and K (coaching guard).
  - Platform CRUD (parameters, categories, presets): SA or CM
    (RoleType.CONTENT_MANAGER). Per L3 — platform-wide seeded data
    is maintained by SA or CM, consumed by all Clients.

See `project_soil_nutrient_module_design.md` Decisions L, M, N, P.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.clients.models import Client
from app.modules.platform.models import RoleType, StatusEnum, User
from app.modules.clients.models import (
    CMClientAssignment, CMRights, ClientUser, ClientUserRole,
)
from app.modules.advisory.models import Package
from app.modules.soil_nutrient.models import (
    ClientNormalRange, ClientProduct, ClientProductCropAssignment,
    FallbackProduct, FallbackProductCropAssignment,
    NormalRangeOperator, PackageNormalRangeOverride,
    PlatformSoilParameter, PresetSoilProfile,
)
from app.modules.soil_nutrient.schemas import (
    ClientNormalRangeOut, ClientProductCreate, ClientProductOut,
    ClientProductUpdate, ClientSoilNutrientStatusOut,
    CoshFertilizerCandidateOut, CoshFertilizerConcentrationOut,
    DecisionReportOut,
    FallbackProductCreate, FallbackProductOut, FallbackProductUpdate,
    MockRunRequest, NormalRangeBulkUpsert, NormalRangeIn,
    PackageNormalRangeOverrideOut,
    PresetSoilProfileCreate, PresetSoilProfileOut, PresetSoilProfileUpdate,
    SoilNutrientModuleToggle, SoilParameterCreate,
    SoilParameterOut, SoilParameterUpdate, SoilTestingLabToggle,
)
from app.services.npk_candidates import load_fertiliser_candidates
from app.services.soil_nutrient_cosh_filters import (
    common_names_for_client_manufacturers,
)
from app.services.soil_nutrient_mock import report_to_dict, run_mock


router = APIRouter(tags=["Soil Nutrient"])


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _require_sa(current_user: User) -> None:
    """SA-only gate (email match against settings.sa_email)."""
    if current_user.email != settings.sa_email:
        raise HTTPException(
            status_code=403, detail="Super Admin access required",
        )


def _require_sa_or_cm(current_user: User) -> None:
    """SA OR any CM gate — for platform-wide seeded data CRUD.

    Decision L3 (2026-10-15): category schemas (and by extension all
    other platform seed data for the Soil Nutrient module) are
    authored in the SA Portal by SA or CM. A CM here does NOT need
    a specific CMClientAssignment because this data is platform-wide,
    not Client-scoped.
    """
    if current_user.email == settings.sa_email:
        return
    active_roles = {
        r.role_type for r in current_user.roles
        if r.status == StatusEnum.ACTIVE
    }
    if RoleType.CONTENT_MANAGER not in active_roles:
        raise HTTPException(
            status_code=403,
            detail="Super Admin or Content Manager access required",
        )


# ── Platform Soil Parameters CRUD (Decision N) ─────────────────────────────

@router.get(
    "/sa/soil-nutrient/parameters",
    response_model=list[SoilParameterOut],
)
async def list_soil_parameters(
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    q = select(PlatformSoilParameter).order_by(
        PlatformSoilParameter.display_order,
    )
    if not include_inactive:
        q = q.where(PlatformSoilParameter.status == "ACTIVE")
    result = await db.execute(q)
    return result.scalars().all()


@router.post(
    "/sa/soil-nutrient/parameters",
    response_model=SoilParameterOut,
    status_code=201,
)
async def create_soil_parameter(
    data: SoilParameterCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    # Name uniqueness guard — the DB has a unique index but we
    # pre-check for a clean 409 instead of a raw IntegrityError.
    existing = (await db.execute(
        select(PlatformSoilParameter).where(
            PlatformSoilParameter.name == data.name,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Soil parameter '{data.name}' already exists",
        )
    row = PlatformSoilParameter(
        name=data.name,
        unit=data.unit,
        category_group=data.category_group,
        formula=data.formula,
        measurement_method_note=data.measurement_method_note,
        display_order=data.display_order,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.patch(
    "/sa/soil-nutrient/parameters/{param_id}",
    response_model=SoilParameterOut,
)
async def update_soil_parameter(
    param_id: str,
    data: SoilParameterUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    row = await db.get(PlatformSoilParameter, param_id)
    if not row:
        raise HTTPException(status_code=404, detail="Parameter not found")

    updates = data.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(row, key, value)

    await db.commit()
    await db.refresh(row)
    return row


@router.delete(
    "/sa/soil-nutrient/parameters/{param_id}",
    response_model=SoilParameterOut,
)
async def deactivate_soil_parameter(
    param_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Soft delete — flips status to INACTIVE. Hard delete would
    orphan any downstream references (Client Normal Ranges, Package
    overrides, Preset profile values, CQ P/V bindings in Decision O).
    """
    _require_sa_or_cm(current_user)
    row = await db.get(PlatformSoilParameter, param_id)
    if not row:
        raise HTTPException(status_code=404, detail="Parameter not found")
    row.status = "INACTIVE"
    await db.commit()
    await db.refresh(row)
    return row




# ── Preset Soil Profiles CRUD (Decision P + MQ5-B) ─────────────────────────

@router.get(
    "/sa/soil-nutrient/preset-profiles",
    response_model=list[PresetSoilProfileOut],
)
async def list_preset_profiles(
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    q = select(PresetSoilProfile).order_by(PresetSoilProfile.display_order)
    if not include_inactive:
        q = q.where(PresetSoilProfile.status == "ACTIVE")
    result = await db.execute(q)
    return result.scalars().all()


@router.post(
    "/sa/soil-nutrient/preset-profiles",
    response_model=PresetSoilProfileOut,
    status_code=201,
)
async def create_preset_profile(
    data: PresetSoilProfileCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    existing = (await db.execute(
        select(PresetSoilProfile).where(PresetSoilProfile.name == data.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Preset profile '{data.name}' already exists",
        )
    row = PresetSoilProfile(
        name=data.name,
        description=data.description,
        values_json=data.values_json,
        display_order=data.display_order,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.patch(
    "/sa/soil-nutrient/preset-profiles/{preset_id}",
    response_model=PresetSoilProfileOut,
)
async def update_preset_profile(
    preset_id: str,
    data: PresetSoilProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    row = await db.get(PresetSoilProfile, preset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Preset not found")
    updates = data.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete(
    "/sa/soil-nutrient/preset-profiles/{preset_id}",
    response_model=PresetSoilProfileOut,
)
async def deactivate_preset_profile(
    preset_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    row = await db.get(PresetSoilProfile, preset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Preset not found")
    row.status = "INACTIVE"
    await db.commit()
    await db.refresh(row)
    return row


# ── Fallback Catalogue CRUD (Decision M) ───────────────────────────────────

def _apply_crop_assignments(
    product: FallbackProduct, crop_cosh_ids: list[str],
) -> None:
    """Replace-all semantics for crop assignments. Clears existing
    assignments on the product, then adds new rows for each crop in
    `crop_cosh_ids`. Empty list = universal (no crop restriction).
    SQLAlchemy's delete-orphan cascade handles the removals when the
    relationship list is reassigned.
    """
    new_rows = [
        FallbackProductCropAssignment(crop_cosh_id=cid, status="ACTIVE")
        for cid in crop_cosh_ids
    ]
    product.crop_assignments = new_rows


async def _candidates_by_cosh_id(
    db: AsyncSession,
) -> dict[str, CoshFertilizerCandidateOut]:
    """Load the fertilizer catalogue from Cosh once and key it by cosh_id.

    Shared with the Dealer NPK-select pipeline (`load_fertiliser_
    candidates`), so Fallback inherits every row Cosh considers valid
    (fertigation=False includes both soil + fertigation products since
    we accept non-water-soluble too). Phase 6b uses this to hydrate
    the picker + list, and to validate pointer-create input.
    """
    candidates, _ = await load_fertiliser_candidates(db, fertigation=False)
    return {
        c.cosh_id: CoshFertilizerCandidateOut(
            cosh_id=c.cosh_id,
            name=c.name,
            concentration=CoshFertilizerConcentrationOut(
                n=c.concentration.n,
                p=c.concentration.p,
                k=c.concentration.k,
            ),
            water_soluble=c.water_soluble,
        )
        for c in candidates
    }


def _hydrate_fallback(
    row: FallbackProduct,
    by_cosh_id: dict[str, CoshFertilizerCandidateOut],
) -> FallbackProductOut:
    return FallbackProductOut(
        id=row.id,
        cosh_product_id=row.cosh_product_id,
        status=row.status,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
        crop_assignments=[
            {"id": a.id, "crop_cosh_id": a.crop_cosh_id, "status": a.status}
            for a in row.crop_assignments
        ],
        cosh=by_cosh_id.get(row.cosh_product_id),
    )


@router.get(
    "/sa/soil-nutrient/cosh-fertilizers",
    response_model=list[CoshFertilizerCandidateOut],
)
async def list_cosh_fertilizers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Picker feed — the full Cosh fertilizer catalogue the SA chooses
    from when adding to the Fallback list. Shared Cosh query layer
    with Dealer NPK-select (`load_fertiliser_candidates`)."""
    _require_sa_or_cm(current_user)
    by_cosh_id = await _candidates_by_cosh_id(db)
    return sorted(by_cosh_id.values(), key=lambda c: c.name.lower())


@router.get(
    "/sa/soil-nutrient/fallback-products",
    response_model=list[FallbackProductOut],
)
async def list_fallback_products(
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List Fallback products — pointer rows hydrated with Cosh data."""
    _require_sa_or_cm(current_user)
    q = select(FallbackProduct).options(
        selectinload(FallbackProduct.crop_assignments),
    ).order_by(FallbackProduct.created_at.desc())
    if not include_inactive:
        q = q.where(FallbackProduct.status == "ACTIVE")
    result = await db.execute(q)
    rows = result.scalars().all()
    by_cosh_id = await _candidates_by_cosh_id(db)
    return [_hydrate_fallback(r, by_cosh_id) for r in rows]


@router.post(
    "/sa/soil-nutrient/fallback-products",
    response_model=FallbackProductOut,
    status_code=201,
)
async def create_fallback_product(
    data: FallbackProductCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    by_cosh_id = await _candidates_by_cosh_id(db)
    if data.cosh_product_id not in by_cosh_id:
        raise HTTPException(
            status_code=400,
            detail="cosh_product_id is not a known Cosh fertilizer",
        )

    existing = (await db.execute(
        select(FallbackProduct).where(
            FallbackProduct.cosh_product_id == data.cosh_product_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="This Cosh product is already in the Fallback catalogue",
        )

    row = FallbackProduct(
        cosh_product_id=data.cosh_product_id,
        notes=data.notes,
        created_by=current_user.id,
    )
    db.add(row)
    if data.crop_cosh_ids is not None:
        _apply_crop_assignments(row, data.crop_cosh_ids)
    await db.commit()
    reloaded = (await db.execute(
        select(FallbackProduct).options(
            selectinload(FallbackProduct.crop_assignments),
        ).where(FallbackProduct.id == row.id)
    )).scalar_one()
    return _hydrate_fallback(reloaded, by_cosh_id)


@router.patch(
    "/sa/soil-nutrient/fallback-products/{product_id}",
    response_model=FallbackProductOut,
)
async def update_fallback_product(
    product_id: str,
    data: FallbackProductUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    row = (await db.execute(
        select(FallbackProduct).options(
            selectinload(FallbackProduct.crop_assignments),
        ).where(FallbackProduct.id == product_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")

    updates = data.model_dump(exclude_unset=True, exclude={"crop_cosh_ids"})
    for key, value in updates.items():
        setattr(row, key, value)

    if data.crop_cosh_ids is not None:
        _apply_crop_assignments(row, data.crop_cosh_ids)

    await db.commit()
    reloaded = (await db.execute(
        select(FallbackProduct).options(
            selectinload(FallbackProduct.crop_assignments),
        ).where(FallbackProduct.id == product_id)
    )).scalar_one()
    by_cosh_id = await _candidates_by_cosh_id(db)
    return _hydrate_fallback(reloaded, by_cosh_id)


@router.delete(
    "/sa/soil-nutrient/fallback-products/{product_id}",
    response_model=FallbackProductOut,
)
async def deactivate_fallback_product(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Soft delete — status → INACTIVE. Hard delete would orphan
    engine-side references once the Soil Nutrient engine lands."""
    _require_sa_or_cm(current_user)
    row = await db.get(FallbackProduct, product_id)
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    row.status = "INACTIVE"
    await db.commit()
    reloaded = (await db.execute(
        select(FallbackProduct).options(
            selectinload(FallbackProduct.crop_assignments),
        ).where(FallbackProduct.id == product_id)
    )).scalar_one()
    by_cosh_id = await _candidates_by_cosh_id(db)
    return _hydrate_fallback(reloaded, by_cosh_id)


# ── Client Portal — Client Products CRUD (Decision L) ──────────────────────

async def _assert_can_edit_client_soil_nutrient(
    db: AsyncSession, user_id: str, client_id: str,
) -> None:
    """Gate for Client Portal Soil Nutrient write endpoints.

    Three gates in order:
      1. Client must exist AND have soil_nutrient_module_enabled=True.
         If not, 403 with code `soil_nutrient_module_disabled` so the
         UI can show a clear "Ask your SA to enable this module"
         message instead of a generic access error.
      2. Caller must be an ACTIVE SUBJECT_EXPERT ClientUser of this
         client, OR
      3. Caller must have an ACTIVE CMClientAssignment with EDIT
         rights for this client.
      Otherwise 403.
    """
    client = await db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if not client.soil_nutrient_module_enabled:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "soil_nutrient_module_disabled",
                "message": (
                    "Soil Nutrient Management is not enabled for this "
                    "client. Contact your Super Admin to enable it."
                ),
            },
        )

    cu = (await db.execute(
        select(ClientUser).where(
            ClientUser.user_id == user_id,
            ClientUser.client_id == client_id,
            ClientUser.status == StatusEnum.ACTIVE,
        ).limit(1)
    )).scalar_one_or_none()
    if cu is not None and cu.role == ClientUserRole.SUBJECT_EXPERT:
        return

    cm = (await db.execute(
        select(CMClientAssignment.id).where(
            CMClientAssignment.cm_user_id == user_id,
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
            CMClientAssignment.rights == CMRights.EDIT,
        ).limit(1)
    )).scalar_one_or_none()
    if cm is not None:
        return

    raise HTTPException(
        status_code=403,
        detail={
            "code": "ca_edit_forbidden",
            "message": (
                "Only Subject Experts or Content Managers with EDIT "
                "rights can author Soil Nutrient products for this "
                "client."
            ),
        },
    )


def _apply_client_crop_assignments(
    product: ClientProduct, client_id: str, crop_cosh_ids: list[str],
) -> None:
    """Replace-all for Client Product crop assignments."""
    product.crop_assignments = [
        ClientProductCropAssignment(
            client_id=client_id, crop_cosh_id=cid, status="ACTIVE",
        )
        for cid in crop_cosh_ids
    ]


async def _client_candidates_by_cosh_id(
    db: AsyncSession,
) -> dict[str, CoshFertilizerCandidateOut]:
    """Pre-hydrate the full Cosh fertilizer catalogue keyed by cosh_id.
    Used by Client Product list hydration + create-time validation."""
    candidates, _ = await load_fertiliser_candidates(db, fertigation=False)
    return {
        c.cosh_id: CoshFertilizerCandidateOut(
            cosh_id=c.cosh_id,
            name=c.name,
            concentration=CoshFertilizerConcentrationOut(
                n=c.concentration.n,
                p=c.concentration.p,
                k=c.concentration.k,
            ),
            water_soluble=c.water_soluble,
        )
        for c in candidates
    }


def _hydrate_client_product(
    row: ClientProduct,
    by_cosh_id: dict[str, CoshFertilizerCandidateOut],
) -> ClientProductOut:
    return ClientProductOut(
        id=row.id,
        client_id=row.client_id,
        cosh_product_id=row.cosh_product_id,
        status=row.status,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
        crop_assignments=[
            {"id": a.id, "crop_cosh_id": a.crop_cosh_id, "status": a.status}
            for a in row.crop_assignments
        ],
        cosh=by_cosh_id.get(row.cosh_product_id),
    )


@router.get(
    "/client/{client_id}/soil-nutrient/cosh-fertilizers",
    response_model=list[CoshFertilizerCandidateOut],
)
async def list_client_cosh_fertilizers(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Decision U picker feed — Cosh fertilizers manufactured by this
    Client's linked Manufacturers (via `client_cosh_manufacturers`).

    When the Client has no linked Manufacturers yet, returns an empty
    list — the frontend surfaces an inline hint pointing to the SA
    Portal Client edit modal where the link is set up.
    """
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    allowed_cns = await common_names_for_client_manufacturers(db, client_id)
    if allowed_cns is None or not allowed_cns:
        return []
    by_cosh_id = await _client_candidates_by_cosh_id(db)
    return sorted(
        (c for cid, c in by_cosh_id.items() if cid in allowed_cns),
        key=lambda c: c.name.lower(),
    )


@router.get(
    "/client/{client_id}/soil-nutrient/products",
    response_model=list[ClientProductOut],
)
async def list_client_products(
    client_id: str,
    include_inactive: bool = False,
    crop_cosh_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List this client's Global Product List — pointer rows hydrated
    with Cosh data. Filter by `crop_cosh_id` for the Per-Crop tab view."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    q = select(ClientProduct).options(
        selectinload(ClientProduct.crop_assignments),
    ).where(
        ClientProduct.client_id == client_id,
    ).order_by(ClientProduct.created_at.desc())
    if not include_inactive:
        q = q.where(ClientProduct.status == "ACTIVE")
    result = await db.execute(q)
    rows = result.scalars().all()
    if crop_cosh_id:
        rows = [
            r for r in rows
            if any(a.crop_cosh_id == crop_cosh_id and a.status == "ACTIVE"
                   for a in r.crop_assignments)
        ]
    by_cosh_id = await _client_candidates_by_cosh_id(db)
    return [_hydrate_client_product(r, by_cosh_id) for r in rows]


@router.post(
    "/client/{client_id}/soil-nutrient/products",
    response_model=ClientProductOut,
    status_code=201,
)
async def create_client_product(
    client_id: str,
    data: ClientProductCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)

    # Validate the cosh_product_id against the manufacturer-filtered
    # set — Clients can only add products made by their linked
    # Manufacturers (Decision U).
    allowed_cns = await common_names_for_client_manufacturers(db, client_id)
    if allowed_cns is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "no_linked_manufacturers",
                "message": (
                    "This Client isn't linked to any Cosh Manufacturers. "
                    "Ask the Super Admin to link your Manufacturer(s) from "
                    "the Company edit modal before adding products."
                ),
            },
        )
    if data.cosh_product_id not in allowed_cns:
        raise HTTPException(
            status_code=400,
            detail=(
                "cosh_product_id is not made by this Client's linked "
                "Manufacturer(s)"
            ),
        )

    by_cosh_id = await _client_candidates_by_cosh_id(db)
    if data.cosh_product_id not in by_cosh_id:
        raise HTTPException(
            status_code=400,
            detail="cosh_product_id is not a known Cosh fertilizer",
        )

    existing = (await db.execute(
        select(ClientProduct).where(
            ClientProduct.client_id == client_id,
            ClientProduct.cosh_product_id == data.cosh_product_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="This Cosh product is already in this Client's Global List",
        )

    row = ClientProduct(
        client_id=client_id,
        cosh_product_id=data.cosh_product_id,
        notes=data.notes,
        created_by=current_user.id,
    )
    db.add(row)
    if data.crop_cosh_ids is not None:
        _apply_client_crop_assignments(row, client_id, data.crop_cosh_ids)
    await db.commit()
    reloaded = (await db.execute(
        select(ClientProduct).options(
            selectinload(ClientProduct.crop_assignments),
        ).where(ClientProduct.id == row.id)
    )).scalar_one()
    return _hydrate_client_product(reloaded, by_cosh_id)


@router.patch(
    "/client/{client_id}/soil-nutrient/products/{product_id}",
    response_model=ClientProductOut,
)
async def update_client_product(
    client_id: str,
    product_id: str,
    data: ClientProductUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    row = (await db.execute(
        select(ClientProduct).options(
            selectinload(ClientProduct.crop_assignments),
        ).where(
            ClientProduct.id == product_id,
            ClientProduct.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")

    updates = data.model_dump(exclude_unset=True, exclude={"crop_cosh_ids"})
    for key, value in updates.items():
        setattr(row, key, value)

    if data.crop_cosh_ids is not None:
        _apply_client_crop_assignments(row, client_id, data.crop_cosh_ids)

    await db.commit()
    reloaded = (await db.execute(
        select(ClientProduct).options(
            selectinload(ClientProduct.crop_assignments),
        ).where(ClientProduct.id == product_id)
    )).scalar_one()
    by_cosh_id = await _client_candidates_by_cosh_id(db)
    return _hydrate_client_product(reloaded, by_cosh_id)


@router.delete(
    "/client/{client_id}/soil-nutrient/products/{product_id}",
    response_model=ClientProductOut,
)
async def discontinue_client_product(
    client_id: str,
    product_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Soft delete — flips status to DISCONTINUED. Soil Nutrient engine
    excludes DISCONTINUED products from Tier 1 candidate pool at the
    next personalisation run (prior personalisations stay as authored;
    farmer's live advisory isn't retroactively edited)."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    row = (await db.execute(
        select(ClientProduct).where(
            ClientProduct.id == product_id,
            ClientProduct.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    row.status = "DISCONTINUED"
    await db.commit()
    reloaded = (await db.execute(
        select(ClientProduct).options(
            selectinload(ClientProduct.crop_assignments),
        ).where(ClientProduct.id == product_id)
    )).scalar_one()
    by_cosh_id = await _client_candidates_by_cosh_id(db)
    return _hydrate_client_product(reloaded, by_cosh_id)


@router.get(
    "/client/{client_id}/soil-nutrient/parameters",
    response_model=list[SoilParameterOut],
)
async def list_client_portal_soil_parameters(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mirror of /sa/soil-nutrient/parameters for Client Portal.
    Returns the active 22-row canon that drives the Normal Ranges
    authoring form."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    result = await db.execute(
        select(PlatformSoilParameter).where(
            PlatformSoilParameter.status == "ACTIVE",
        ).order_by(PlatformSoilParameter.display_order)
    )
    return result.scalars().all()


# ── Normal Ranges (Decision N) ─────────────────────────────────────────────

def _validate_normal_range_row(row: NormalRangeIn) -> None:
    """Enforce operator-specific required-fields invariant.

    Called at upsert time; raises 400 with a clear message when a
    row's operator doesn't match the populated low/high values.
    """
    if row.operator == NormalRangeOperator.RANGE:
        if row.low_value is None or row.high_value is None:
            raise HTTPException(
                status_code=400,
                detail=f"RANGE operator requires both low_value and high_value (parameter {row.parameter_id}).",
            )
        if row.low_value > row.high_value:
            raise HTTPException(
                status_code=400,
                detail=f"RANGE operator requires low_value <= high_value (parameter {row.parameter_id}).",
            )
    elif row.operator == NormalRangeOperator.UPPER_BOUND:
        if row.high_value is None:
            raise HTTPException(
                status_code=400,
                detail=f"UPPER_BOUND operator requires high_value (parameter {row.parameter_id}).",
            )
        if row.low_value is not None:
            raise HTTPException(
                status_code=400,
                detail=f"UPPER_BOUND operator must not carry low_value (parameter {row.parameter_id}).",
            )
    elif row.operator == NormalRangeOperator.LOWER_BOUND:
        if row.low_value is None:
            raise HTTPException(
                status_code=400,
                detail=f"LOWER_BOUND operator requires low_value (parameter {row.parameter_id}).",
            )
        if row.high_value is not None:
            raise HTTPException(
                status_code=400,
                detail=f"LOWER_BOUND operator must not carry high_value (parameter {row.parameter_id}).",
            )

    # Decision V — operator-gated % adjustments.
    if row.operator == NormalRangeOperator.UPPER_BOUND and row.low_pct_adjustment != 0.0:
        raise HTTPException(
            status_code=400,
            detail=f"UPPER_BOUND operator has no Low classification; low_pct_adjustment must be 0 (parameter {row.parameter_id}).",
        )
    if row.operator == NormalRangeOperator.LOWER_BOUND and row.high_pct_adjustment != 0.0:
        raise HTTPException(
            status_code=400,
            detail=f"LOWER_BOUND operator has no High classification; high_pct_adjustment must be 0 (parameter {row.parameter_id}).",
        )


@router.get(
    "/client/{client_id}/soil-nutrient/normal-ranges",
    response_model=list[ClientNormalRangeOut],
)
async def list_client_normal_ranges(
    client_id: str,
    crop_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List this client's crop-level Normal Range defaults.

    Returns only parameters the client has actually authored for
    this crop. Parameters with no row = engine doesn't classify them.
    """
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    result = await db.execute(
        select(ClientNormalRange).where(
            ClientNormalRange.client_id == client_id,
            ClientNormalRange.crop_cosh_id == crop_cosh_id,
        )
    )
    return result.scalars().all()


@router.put(
    "/client/{client_id}/soil-nutrient/normal-ranges",
    response_model=list[ClientNormalRangeOut],
)
async def bulk_upsert_client_normal_ranges(
    client_id: str,
    crop_cosh_id: str,
    data: NormalRangeBulkUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bulk upsert the crop-level Normal Range defaults.

    One Save at the top of the Normal Ranges page writes every row.
    Rows passed in get upserted; parameters NOT in the payload are
    left unchanged (not deleted) — if the Client wants to clear a
    parameter, they use the DELETE endpoint for that parameter.
    """
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)

    for row in data.rows:
        _validate_normal_range_row(row)

    # Load existing rows for upsert
    existing = (await db.execute(
        select(ClientNormalRange).where(
            ClientNormalRange.client_id == client_id,
            ClientNormalRange.crop_cosh_id == crop_cosh_id,
        )
    )).scalars().all()
    by_param = {r.parameter_id: r for r in existing}

    for row in data.rows:
        if row.parameter_id in by_param:
            r = by_param[row.parameter_id]
            r.operator = row.operator
            r.low_value = row.low_value
            r.high_value = row.high_value
            r.low_pct_adjustment = row.low_pct_adjustment
            r.high_pct_adjustment = row.high_pct_adjustment
        else:
            new_row = ClientNormalRange(
                client_id=client_id,
                crop_cosh_id=crop_cosh_id,
                parameter_id=row.parameter_id,
                operator=row.operator,
                low_value=row.low_value,
                high_value=row.high_value,
                low_pct_adjustment=row.low_pct_adjustment,
                high_pct_adjustment=row.high_pct_adjustment,
                created_by=current_user.id,
            )
            db.add(new_row)

    await db.commit()
    result = await db.execute(
        select(ClientNormalRange).where(
            ClientNormalRange.client_id == client_id,
            ClientNormalRange.crop_cosh_id == crop_cosh_id,
        )
    )
    return result.scalars().all()


@router.delete(
    "/client/{client_id}/soil-nutrient/normal-ranges/{parameter_id}",
    status_code=204,
)
async def delete_client_normal_range(
    client_id: str,
    parameter_id: str,
    crop_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Clear a per-crop range for one parameter (engine stops
    classifying that parameter for this crop)."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    row = (await db.execute(
        select(ClientNormalRange).where(
            ClientNormalRange.client_id == client_id,
            ClientNormalRange.crop_cosh_id == crop_cosh_id,
            ClientNormalRange.parameter_id == parameter_id,
        )
    )).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return None


# ── Package-level Normal Range overrides (Decision N two-tier) ────────────

async def _assert_package_belongs_to_client(
    db: AsyncSession, package_id: str, client_id: str,
) -> Package:
    pkg = await db.get(Package, package_id)
    if pkg is None:
        raise HTTPException(status_code=404, detail="Package not found")
    # Global/platform packages have client_id=NULL — Normal Range
    # overrides only apply to client-authored packages.
    if pkg.client_id != client_id:
        raise HTTPException(
            status_code=403,
            detail="Package does not belong to this client",
        )
    return pkg


@router.get(
    "/client/{client_id}/soil-nutrient/packages/{package_id}/overrides",
    response_model=list[PackageNormalRangeOverrideOut],
)
async def list_package_normal_range_overrides(
    client_id: str,
    package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List overrides for one Package. Sparse — only parameters with
    overrides have rows. The authoring UI merges with crop defaults
    to show the effective range per parameter."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    await _assert_package_belongs_to_client(db, package_id, client_id)
    result = await db.execute(
        select(PackageNormalRangeOverride).where(
            PackageNormalRangeOverride.package_id == package_id,
        )
    )
    return result.scalars().all()


@router.put(
    "/client/{client_id}/soil-nutrient/packages/{package_id}/overrides",
    response_model=list[PackageNormalRangeOverrideOut],
)
async def bulk_upsert_package_overrides(
    client_id: str,
    package_id: str,
    data: NormalRangeBulkUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bulk upsert Package-level overrides. Same semantics as the
    crop-level endpoint — rows not in the payload are left alone;
    use DELETE to clear."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    await _assert_package_belongs_to_client(db, package_id, client_id)

    for row in data.rows:
        _validate_normal_range_row(row)

    existing = (await db.execute(
        select(PackageNormalRangeOverride).where(
            PackageNormalRangeOverride.package_id == package_id,
        )
    )).scalars().all()
    by_param = {r.parameter_id: r for r in existing}

    for row in data.rows:
        if row.parameter_id in by_param:
            r = by_param[row.parameter_id]
            r.operator = row.operator
            r.low_value = row.low_value
            r.high_value = row.high_value
            r.low_pct_adjustment = row.low_pct_adjustment
            r.high_pct_adjustment = row.high_pct_adjustment
        else:
            new_row = PackageNormalRangeOverride(
                package_id=package_id,
                parameter_id=row.parameter_id,
                operator=row.operator,
                low_value=row.low_value,
                high_value=row.high_value,
                low_pct_adjustment=row.low_pct_adjustment,
                high_pct_adjustment=row.high_pct_adjustment,
                created_by=current_user.id,
            )
            db.add(new_row)

    await db.commit()
    result = await db.execute(
        select(PackageNormalRangeOverride).where(
            PackageNormalRangeOverride.package_id == package_id,
        )
    )
    return result.scalars().all()


@router.delete(
    "/client/{client_id}/soil-nutrient/packages/{package_id}/overrides/{parameter_id}",
    status_code=204,
)
async def delete_package_normal_range_override(
    client_id: str,
    package_id: str,
    parameter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Clear one override — the parameter falls back to the crop
    default for this Package."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    await _assert_package_belongs_to_client(db, package_id, client_id)
    row = (await db.execute(
        select(PackageNormalRangeOverride).where(
            PackageNormalRangeOverride.package_id == package_id,
            PackageNormalRangeOverride.parameter_id == parameter_id,
        )
    )).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return None


# ── Client flag toggles (SA Portal, SA-only) ───────────────────────────────

@router.get(
    "/sa/clients/{client_id}/soil-nutrient-status",
    response_model=ClientSoilNutrientStatusOut,
)
async def get_client_soil_nutrient_status(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns the current state of the two Client flags. Used by the
    SA Portal Client-detail page to render the toggle controls."""
    _require_sa(current_user)
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return ClientSoilNutrientStatusOut(
        client_id=client.id,
        soil_nutrient_module_enabled=client.soil_nutrient_module_enabled,
        has_soil_testing_lab=client.has_soil_testing_lab,
    )


@router.patch(
    "/sa/clients/{client_id}/soil-nutrient-enabled",
    response_model=ClientSoilNutrientStatusOut,
)
async def toggle_soil_nutrient_module(
    client_id: str,
    data: SoilNutrientModuleToggle,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Toggle `soil_nutrient_module_enabled` for a Client.

    Guard: cannot enable on `is_coaching=True` or `is_training=True`
    Clients. Coaching and training are pedagogical/sandbox Clients
    and shouldn't carry live soil-nutrient personalisation
    (Decision L + Decision P on sandbox hygiene).

    Disabling is unrestricted — SA can turn off the module on any
    Client regardless of their sandbox state.
    """
    _require_sa(current_user)
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    if data.enabled and (client.is_coaching or client.is_training):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "sandbox_client_forbidden",
                "message": (
                    "Soil Nutrient Module cannot be enabled on "
                    "coaching or training sandbox clients."
                ),
            },
        )
    client.soil_nutrient_module_enabled = data.enabled
    await db.commit()
    await db.refresh(client)
    return ClientSoilNutrientStatusOut(
        client_id=client.id,
        soil_nutrient_module_enabled=client.soil_nutrient_module_enabled,
        has_soil_testing_lab=client.has_soil_testing_lab,
    )


@router.patch(
    "/sa/clients/{client_id}/soil-testing-lab",
    response_model=ClientSoilNutrientStatusOut,
)
async def toggle_soil_testing_lab(
    client_id: str,
    data: SoilTestingLabToggle,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Toggle `has_soil_testing_lab` for a Client (Decision K).

    Guard: cannot enable on `is_coaching=True` Clients. Coaching
    sandbox doesn't carry live lab-partner data entry.

    Disabling is unrestricted. The flag is toggle-able post-onboarding
    — a Client may acquire or divest a lab mid-tenure.
    """
    _require_sa(current_user)
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    if data.enabled and client.is_coaching:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "coaching_client_forbidden",
                "message": (
                    "Soil Testing Lab cannot be enabled on coaching "
                    "sandbox clients."
                ),
            },
        )
    client.has_soil_testing_lab = data.enabled
    await db.commit()
    await db.refresh(client)
    return ClientSoilNutrientStatusOut(
        client_id=client.id,
        soil_nutrient_module_enabled=client.soil_nutrient_module_enabled,
        has_soil_testing_lab=client.has_soil_testing_lab,
    )


# ── Mock engine (Phase 7) ──────────────────────────────────────────────────

@router.post(
    "/client/{client_id}/soil-nutrient/mock/run",
    response_model=DecisionReportOut,
)
async def run_mock_engine_client_portal(
    client_id: str,
    data: MockRunRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Client Portal Mock run — produces the six-section Decision Report
    (Decision T) for a hypothetical soil state against one of the
    Client's Packages. Does NOT touch subscriptions, overlay rows, or
    notifications — that's the Real engine's job (Decisions R + S).
    """
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    report = await run_mock(
        db,
        client_id=client_id,
        package_id=data.package_id,
        preset_id=data.preset_id,
        soil_values_input=data.soil_values,
    )
    return report_to_dict(report)


@router.post(
    "/sa/clients/{client_id}/soil-nutrient/mock/run",
    response_model=DecisionReportOut,
)
async def run_mock_engine_sa_portal(
    client_id: str,
    data: MockRunRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA Portal Mock run — same engine, SA-side auth gate. SA + any
    active CM can mock-run against any Client (Decision S aggregation).
    """
    _require_sa_or_cm(current_user)
    report = await run_mock(
        db,
        client_id=client_id,
        package_id=data.package_id,
        preset_id=data.preset_id,
        soil_values_input=data.soil_values,
    )
    return report_to_dict(report)


@router.get(
    "/client/{client_id}/soil-nutrient/preset-profiles",
    response_model=list[PresetSoilProfileOut],
)
async def list_client_portal_preset_profiles(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mirror of the SA preset-profiles endpoint for the Client Portal
    Mock form. Presets are platform-seeded so Clients just read the
    ACTIVE list — no per-Client authoring."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    result = await db.execute(
        select(PresetSoilProfile).where(
            PresetSoilProfile.status == "ACTIVE",
        ).order_by(PresetSoilProfile.display_order)
    )
    return result.scalars().all()


@router.get(
    "/sa/clients/{client_id}/soil-nutrient/packages",
    response_model=list[dict],
)
async def list_sa_client_packages_for_mock(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA Portal Mock form helper — lists this Client's Packages so SA/CM
    can pick one to Mock-run against. Shape matches what the Client
    Portal's /cca/packages endpoint returns (plus crop name denorm),
    so the UI can use the same type for both portals.

    Separate endpoint instead of reusing /client/{client_id}/cca/packages
    because that gate is CA/SE/CM-with-assignment; the Mock allows any
    active CM to simulate against any Client (Decision S aggregation).
    """
    _require_sa_or_cm(current_user)
    pkgs = (await db.execute(
        select(Package).where(Package.client_id == client_id)
        .order_by(Package.updated_at.desc().nullslast(), Package.created_at.desc())
    )).scalars().all()

    # Denormalise crop names from client_crops so the UI doesn't need
    # a second fetch.
    from app.modules.clients.models import ClientCrop
    crops = (await db.execute(
        select(ClientCrop).where(ClientCrop.client_id == client_id)
    )).scalars().all()
    crop_name_by_id = {c.crop_cosh_id: c.crop_name_en for c in crops}

    out = []
    for p in pkgs:
        out.append({
            "id": p.id,
            "name": p.name,
            "crop_cosh_id": p.crop_cosh_id,
            "crop_name_en": crop_name_by_id.get(p.crop_cosh_id) or p.crop_cosh_id,
            "status": p.status.value if hasattr(p.status, "value") else str(p.status),
            "version": p.version,
        })
    return out
