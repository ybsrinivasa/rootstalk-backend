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
    ClientProduct, ClientProductCropAssignment,
    FallbackProduct, FallbackProductCropAssignment,
    NormalRangeOperator, PackageNormalRangeOverride,
    PlatformSoilParameter, PresetSoilProfile,
    SoilRangeTemplate, SoilRangeTemplateRow,
)
from app.modules.soil_nutrient.schemas import (
    ClientProductCreate, ClientProductOut,
    ClientProductUpdate, ClientSoilNutrientStatusOut,
    CoshFertilizerCandidateOut, CoshFertilizerConcentrationOut,
    DecisionReportOut,
    FallbackProductCreate, FallbackProductOut, FallbackProductUpdate,
    MockRunRequest,
    PackageNormalRangeOverrideBulkUpsert,
    PackageNormalRangeOverrideIn,
    PackageNormalRangeOverrideOut,
    PresetSoilProfileCreate, PresetSoilProfileOut, PresetSoilProfileUpdate,
    SoilNutrientModuleToggle, SoilParameterCreate,
    SoilParameterOut, SoilParameterUpdate, SoilTestingLabToggle,
    SoilRangeTemplateCreate, SoilRangeTemplateOut,
    SoilRangeTemplateRowIn, SoilRangeTemplateRowsBulkUpsert,
    SoilRangeTemplateUpdate,
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
                n=c.concentration.n,   p=c.concentration.p,   k=c.concentration.k,
                ca=c.concentration.ca, mg=c.concentration.mg, s=c.concentration.s,
                zn=c.concentration.zn, fe=c.concentration.fe, mn=c.concentration.mn,
                cu=c.concentration.cu, b=c.concentration.b,
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
                n=c.concentration.n,   p=c.concentration.p,   k=c.concentration.k,
                ca=c.concentration.ca, mg=c.concentration.mg, s=c.concentration.s,
                zn=c.concentration.zn, fe=c.concentration.fe, mn=c.concentration.mn,
                cu=c.concentration.cu, b=c.concentration.b,
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


# ── Soil Range Templates (Decision W) ──────────────────────────────────────

def _validate_template_row(
    row: SoilRangeTemplateRowIn,
    param_name: str | None = None,
) -> None:
    """Enforce operator-specific required-fields invariant on template
    rows. Templates carry bounds only (no adjustments). `param_name`
    is the display name of the parameter — included in error messages
    so users don't see raw UUIDs. Falls back to the UUID when the
    caller didn't look up a name."""
    label = param_name or f"parameter {row.parameter_id}"
    if row.operator == NormalRangeOperator.RANGE:
        if row.low_value is None or row.high_value is None:
            raise HTTPException(
                status_code=400,
                detail=f"{label}: RANGE operator requires both Low and High values.",
            )
        if row.low_value > row.high_value:
            raise HTTPException(
                status_code=400,
                detail=f"{label}: Low must be less than or equal to High.",
            )
    elif row.operator == NormalRangeOperator.UPPER_BOUND:
        if row.high_value is None:
            raise HTTPException(
                status_code=400,
                detail=f"{label}: operator \"< x\" requires a High value.",
            )
        if row.low_value is not None:
            raise HTTPException(
                status_code=400,
                detail=f"{label}: operator \"< x\" doesn't take a Low value — clear it before saving.",
            )
    elif row.operator == NormalRangeOperator.LOWER_BOUND:
        if row.low_value is None:
            raise HTTPException(
                status_code=400,
                detail=f"{label}: operator \"> x\" requires a Low value.",
            )
        if row.high_value is not None:
            raise HTTPException(
                status_code=400,
                detail=f"{label}: operator \"> x\" doesn't take a High value — clear it before saving.",
            )


# Decision V-amend (2026-10-21) — CategoryGroup values that are NOT
# dosable via a fertilizer product. Low/High adjustments make no
# engine sense here (you can't "shift pH target by 20%") so they're
# rejected server-side and hidden in the UI. The classification itself
# (operator + Low + High) stays valid — these parameters still appear
# in Mock Section 2 (Soil Interpretation).
_NON_DOSABLE_CATEGORY_GROUPS = {"BASE", "OTHER", "RATIO"}


def _validate_package_override_row(
    row: PackageNormalRangeOverrideIn,
    param_category_group: str | None = None,
    param_name: str | None = None,
) -> None:
    """Validate a Package override row — bounds AND adjustments.

    `param_name` is the display name of the parameter — included in
    error messages so users don't see raw UUIDs.

    Rules:
      - If operator is set, run the same per-operator low/high
        invariants as template rows.
      - If operator is None, low/high must also be None (can't have
        a bound without declaring the operator shape).
      - Decision V adjustments gated by operator when operator is set.
        When operator is None we take the Package template's shape at
        engine time and gate there; the row itself just carries the
        two floats as supplied.
      - Decision V-amend: for BASE / OTHER / RATIO parameters,
        low_pct_adjustment + high_pct_adjustment must both be 0 —
        these parameters aren't dosable.
    """
    label = param_name or f"parameter {row.parameter_id}"

    # Decision V-amend: dosability check runs first — simpler failure
    # mode and clearest message when the parameter isn't dosable.
    if (
        param_category_group in _NON_DOSABLE_CATEGORY_GROUPS
        and (row.low_pct_adjustment != 0.0 or row.high_pct_adjustment != 0.0)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{label} isn't a dosable parameter — Low Adj % and "
                f"High Adj % must both be 0."
            ),
        )

    if row.operator is None:
        if row.low_value is not None or row.high_value is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{label}: inheriting the template's operator means "
                    f"Low and High must also be left blank."
                ),
            )
        return  # No per-operator adjustment gating in inherit-mode.

    # Bounds validation (same as template rows). Pass the name through
    # so the inner validator's error messages also use the name.
    _validate_template_row(
        SoilRangeTemplateRowIn(
            parameter_id=row.parameter_id,
            operator=row.operator,
            low_value=row.low_value,
            high_value=row.high_value,
        ),
        param_name=param_name,
    )

    # Decision V — operator-gated % adjustments when operator is set.
    if row.operator == NormalRangeOperator.UPPER_BOUND and row.low_pct_adjustment != 0.0:
        raise HTTPException(
            status_code=400,
            detail=f"{label}: operator \"< x\" has no Low classification — Low Adj % must be 0.",
        )
    if row.operator == NormalRangeOperator.LOWER_BOUND and row.high_pct_adjustment != 0.0:
        raise HTTPException(
            status_code=400,
            detail=f"{label}: operator \"> x\" has no High classification — High Adj % must be 0.",
        )


@router.get(
    "/client/{client_id}/soil-nutrient/templates",
    response_model=list[SoilRangeTemplateOut],
)
async def list_soil_range_templates(
    client_id: str,
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List this client's Soil Range Templates with their rows."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    q = select(SoilRangeTemplate).options(
        selectinload(SoilRangeTemplate.rows),
    ).where(
        SoilRangeTemplate.client_id == client_id,
    ).order_by(SoilRangeTemplate.name)
    if not include_inactive:
        q = q.where(SoilRangeTemplate.status == "ACTIVE")
    result = await db.execute(q)
    return result.scalars().all()


@router.post(
    "/client/{client_id}/soil-nutrient/templates",
    response_model=SoilRangeTemplateOut,
    status_code=201,
)
async def create_soil_range_template(
    client_id: str,
    data: SoilRangeTemplateCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new template. Name unique per Client."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    existing = (await db.execute(
        select(SoilRangeTemplate).where(
            SoilRangeTemplate.client_id == client_id,
            SoilRangeTemplate.name == data.name,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Template '{data.name}' already exists",
        )
    row = SoilRangeTemplate(
        client_id=client_id,
        name=data.name,
        description=data.description,
        created_by=current_user.id,
    )
    db.add(row)
    await db.commit()
    reloaded = (await db.execute(
        select(SoilRangeTemplate).options(
            selectinload(SoilRangeTemplate.rows),
        ).where(SoilRangeTemplate.id == row.id)
    )).scalar_one()
    return reloaded


@router.patch(
    "/client/{client_id}/soil-nutrient/templates/{template_id}",
    response_model=SoilRangeTemplateOut,
)
async def update_soil_range_template(
    client_id: str,
    template_id: str,
    data: SoilRangeTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit template metadata (name / description / status). Row-level
    edits go through the bulk-upsert endpoint below."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    row = (await db.execute(
        select(SoilRangeTemplate).options(
            selectinload(SoilRangeTemplate.rows),
        ).where(
            SoilRangeTemplate.id == template_id,
            SoilRangeTemplate.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    updates = data.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(row, key, value)
    await db.commit()
    reloaded = (await db.execute(
        select(SoilRangeTemplate).options(
            selectinload(SoilRangeTemplate.rows),
        ).where(SoilRangeTemplate.id == template_id)
    )).scalar_one()
    return reloaded


@router.delete(
    "/client/{client_id}/soil-nutrient/templates/{template_id}",
    response_model=SoilRangeTemplateOut,
)
async def deactivate_soil_range_template(
    client_id: str,
    template_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Soft delete — status → INACTIVE. Packages pointing at this
    template keep the FK (classification silently stops working for
    them, surfaced in the Mock's Section 6 warnings)."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    row = (await db.execute(
        select(SoilRangeTemplate).where(
            SoilRangeTemplate.id == template_id,
            SoilRangeTemplate.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    row.status = "INACTIVE"
    await db.commit()
    reloaded = (await db.execute(
        select(SoilRangeTemplate).options(
            selectinload(SoilRangeTemplate.rows),
        ).where(SoilRangeTemplate.id == template_id)
    )).scalar_one()
    return reloaded


@router.put(
    "/client/{client_id}/soil-nutrient/templates/{template_id}/rows",
    response_model=SoilRangeTemplateOut,
)
async def bulk_upsert_template_rows(
    client_id: str,
    template_id: str,
    data: SoilRangeTemplateRowsBulkUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bulk upsert the per-parameter bounds for one template. Rows
    not in the payload are left unchanged; use the per-row DELETE
    below to clear a parameter."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    tpl = (await db.execute(
        select(SoilRangeTemplate).options(
            selectinload(SoilRangeTemplate.rows),
        ).where(
            SoilRangeTemplate.id == template_id,
            SoilRangeTemplate.client_id == client_id,
        )
    )).scalar_one_or_none()
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")

    # Pre-load parameter names so validator errors surface the human
    # name instead of the UUID.
    param_ids = [row.parameter_id for row in data.rows]
    name_by_param: dict[str, str] = {}
    if param_ids:
        rows_p = (await db.execute(
            select(PlatformSoilParameter.id, PlatformSoilParameter.name)
            .where(PlatformSoilParameter.id.in_(param_ids))
        )).all()
        name_by_param = {pid: name for pid, name in rows_p}

    for row in data.rows:
        _validate_template_row(row, param_name=name_by_param.get(row.parameter_id))

    by_param = {r.parameter_id: r for r in tpl.rows}
    for row in data.rows:
        if row.parameter_id in by_param:
            r = by_param[row.parameter_id]
            r.operator = row.operator
            r.low_value = row.low_value
            r.high_value = row.high_value
        else:
            db.add(SoilRangeTemplateRow(
                template_id=template_id,
                parameter_id=row.parameter_id,
                operator=row.operator,
                low_value=row.low_value,
                high_value=row.high_value,
            ))
    await db.commit()
    reloaded = (await db.execute(
        select(SoilRangeTemplate).options(
            selectinload(SoilRangeTemplate.rows),
        ).where(SoilRangeTemplate.id == template_id)
    )).scalar_one()
    return reloaded


@router.delete(
    "/client/{client_id}/soil-nutrient/templates/{template_id}/rows/{parameter_id}",
    status_code=204,
)
async def delete_template_row(
    client_id: str,
    template_id: str,
    parameter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Clear one parameter from a template."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    tpl = await db.get(SoilRangeTemplate, template_id)
    if tpl is None or tpl.client_id != client_id:
        raise HTTPException(status_code=404, detail="Template not found")
    row = (await db.execute(
        select(SoilRangeTemplateRow).where(
            SoilRangeTemplateRow.template_id == template_id,
            SoilRangeTemplateRow.parameter_id == parameter_id,
        )
    )).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return None


# ── Package-level overrides (Decision W — same table, reshaped) ───────────

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
    overrides have rows. The authoring UI merges with the Package's
    chosen template to show the effective range + adjustment per
    parameter."""
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
    data: PackageNormalRangeOverrideBulkUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bulk upsert Package-level override rows. Each row can carry
    any combination of bounds (operator + low/high) and adjustments
    (low_pct / high_pct). Rows not in the payload are left alone;
    use DELETE to clear."""
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    await _assert_package_belongs_to_client(db, package_id, client_id)

    # Decision V-amend: load category_group AND name for every
    # parameter the payload touches, so the validator can (a) reject
    # adjustments on non-dosable parameters (BASE / OTHER / RATIO)
    # and (b) surface the parameter's human name in errors instead
    # of the raw UUID.
    param_ids = [row.parameter_id for row in data.rows]
    category_by_param: dict[str, str] = {}
    name_by_param: dict[str, str] = {}
    if param_ids:
        rows = (await db.execute(
            select(
                PlatformSoilParameter.id,
                PlatformSoilParameter.category_group,
                PlatformSoilParameter.name,
            ).where(PlatformSoilParameter.id.in_(param_ids))
        )).all()
        for pid, cg, nm in rows:
            category_by_param[pid] = cg.value if hasattr(cg, "value") else str(cg)
            name_by_param[pid] = nm

    for row in data.rows:
        _validate_package_override_row(
            row,
            param_category_group=category_by_param.get(row.parameter_id),
            param_name=name_by_param.get(row.parameter_id),
        )

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
    """Clear one override — the parameter falls back to the Package's
    template bounds (and 0% adjustments)."""
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
