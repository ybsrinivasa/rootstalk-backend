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
    ProductCategory, ProductCategoryField,
)
from app.modules.soil_nutrient.schemas import (
    ClientNormalRangeOut, ClientProductCreate, ClientProductOut,
    ClientProductUpdate, ClientSoilNutrientStatusOut,
    FallbackProductCreate, FallbackProductOut, FallbackProductUpdate,
    NormalRangeBulkUpsert, NormalRangeIn,
    PackageNormalRangeOverrideOut,
    PresetSoilProfileCreate, PresetSoilProfileOut, PresetSoilProfileUpdate,
    ProductCategoryCreate, ProductCategoryFieldCreate,
    ProductCategoryFieldOut, ProductCategoryFieldUpdate,
    ProductCategoryOut, ProductCategoryUpdate,
    SoilNutrientModuleToggle, SoilParameterCreate,
    SoilParameterOut, SoilParameterUpdate, SoilTestingLabToggle,
)


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


# ── Product Categories CRUD (Decision L) ───────────────────────────────────

@router.get(
    "/sa/soil-nutrient/product-categories",
    response_model=list[ProductCategoryOut],
)
async def list_product_categories(
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    q = select(ProductCategory).options(
        selectinload(ProductCategory.fields),
    ).order_by(ProductCategory.display_order)
    if not include_inactive:
        q = q.where(ProductCategory.status == "ACTIVE")
    result = await db.execute(q)
    return result.scalars().all()


@router.post(
    "/sa/soil-nutrient/product-categories",
    response_model=ProductCategoryOut,
    status_code=201,
)
async def create_product_category(
    data: ProductCategoryCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    existing = (await db.execute(
        select(ProductCategory).where(ProductCategory.name == data.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Product category '{data.name}' already exists",
        )
    row = ProductCategory(
        name=data.name,
        scope=data.scope,
        display_order=data.display_order,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    # Reload with fields relationship for consistent response shape
    reloaded = (await db.execute(
        select(ProductCategory).options(
            selectinload(ProductCategory.fields),
        ).where(ProductCategory.id == row.id)
    )).scalar_one()
    return reloaded


@router.patch(
    "/sa/soil-nutrient/product-categories/{category_id}",
    response_model=ProductCategoryOut,
)
async def update_product_category(
    category_id: str,
    data: ProductCategoryUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    row = await db.get(ProductCategory, category_id)
    if not row:
        raise HTTPException(status_code=404, detail="Category not found")
    updates = data.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(row, key, value)
    await db.commit()
    reloaded = (await db.execute(
        select(ProductCategory).options(
            selectinload(ProductCategory.fields),
        ).where(ProductCategory.id == row.id)
    )).scalar_one()
    return reloaded


@router.delete(
    "/sa/soil-nutrient/product-categories/{category_id}",
    response_model=ProductCategoryOut,
)
async def deactivate_product_category(
    category_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Soft delete — flips status to INACTIVE. Hard delete would
    orphan Client products and Fallback products referencing this
    category (land in Phase 2+)."""
    _require_sa_or_cm(current_user)
    row = await db.get(ProductCategory, category_id)
    if not row:
        raise HTTPException(status_code=404, detail="Category not found")
    row.status = "INACTIVE"
    await db.commit()
    reloaded = (await db.execute(
        select(ProductCategory).options(
            selectinload(ProductCategory.fields),
        ).where(ProductCategory.id == row.id)
    )).scalar_one()
    return reloaded


# ── Product Category Fields CRUD (Decision L) ──────────────────────────────

@router.post(
    "/sa/soil-nutrient/product-categories/{category_id}/fields",
    response_model=ProductCategoryFieldOut,
    status_code=201,
)
async def add_product_category_field(
    category_id: str,
    data: ProductCategoryFieldCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    category = await db.get(ProductCategory, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    # field_key uniqueness within category
    existing = (await db.execute(
        select(ProductCategoryField).where(
            ProductCategoryField.category_id == category_id,
            ProductCategoryField.field_key == data.field_key,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Field '{data.field_key}' already exists in this category",
        )
    row = ProductCategoryField(
        category_id=category_id,
        field_key=data.field_key,
        field_label=data.field_label,
        field_type=data.field_type,
        required=data.required,
        unit=data.unit,
        enum_values_json=data.enum_values_json,
        display_order=data.display_order,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.patch(
    "/sa/soil-nutrient/product-category-fields/{field_id}",
    response_model=ProductCategoryFieldOut,
)
async def update_product_category_field(
    field_id: str,
    data: ProductCategoryFieldUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    row = await db.get(ProductCategoryField, field_id)
    if not row:
        raise HTTPException(status_code=404, detail="Field not found")
    updates = data.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete(
    "/sa/soil-nutrient/product-category-fields/{field_id}",
    response_model=ProductCategoryFieldOut,
)
async def deactivate_product_category_field(
    field_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa_or_cm(current_user)
    row = await db.get(ProductCategoryField, field_id)
    if not row:
        raise HTTPException(status_code=404, detail="Field not found")
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


@router.get(
    "/sa/soil-nutrient/fallback-products",
    response_model=list[FallbackProductOut],
)
async def list_fallback_products(
    include_inactive: bool = False,
    category_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List Fallback products. Filter by category optional."""
    _require_sa_or_cm(current_user)
    q = select(FallbackProduct).options(
        selectinload(FallbackProduct.crop_assignments),
    ).order_by(FallbackProduct.display_name)
    if not include_inactive:
        q = q.where(FallbackProduct.status == "ACTIVE")
    if category_id:
        q = q.where(FallbackProduct.category_id == category_id)
    result = await db.execute(q)
    return result.scalars().all()


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
    category = await db.get(ProductCategory, data.category_id)
    if not category:
        raise HTTPException(status_code=400, detail="Category not found")

    row = FallbackProduct(
        category_id=data.category_id,
        display_name=data.display_name,
        manufacturer=data.manufacturer,
        grade=data.grade,
        formulation=data.formulation,
        n_pct=data.n_pct,
        p2o5_pct=data.p2o5_pct,
        k2o_pct=data.k2o_pct,
        ca_pct=data.ca_pct,
        mg_pct=data.mg_pct,
        s_pct=data.s_pct,
        zn_pct=data.zn_pct,
        fe_pct=data.fe_pct,
        mn_pct=data.mn_pct,
        cu_pct=data.cu_pct,
        b_pct=data.b_pct,
        application_methods=data.application_methods,
        data_json=data.data_json,
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
    return reloaded


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

    if data.category_id is not None:
        category = await db.get(ProductCategory, data.category_id)
        if not category:
            raise HTTPException(status_code=400, detail="Category not found")

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
    return reloaded


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
    return reloaded


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


@router.get(
    "/client/{client_id}/soil-nutrient/products",
    response_model=list[ClientProductOut],
)
async def list_client_products(
    client_id: str,
    include_inactive: bool = False,
    category_id: str | None = None,
    crop_cosh_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List this client's Global Product List.

    Filter by `category_id` for Tab 1 view, by `crop_cosh_id` for
    Tab 2 view, or both.
    """
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    q = select(ClientProduct).options(
        selectinload(ClientProduct.crop_assignments),
    ).where(
        ClientProduct.client_id == client_id,
    ).order_by(ClientProduct.display_name)
    if not include_inactive:
        q = q.where(ClientProduct.status == "ACTIVE")
    if category_id:
        q = q.where(ClientProduct.category_id == category_id)
    result = await db.execute(q)
    products = result.scalars().all()
    if crop_cosh_id:
        products = [
            p for p in products
            if any(a.crop_cosh_id == crop_cosh_id and a.status == "ACTIVE"
                   for a in p.crop_assignments)
        ]
    return products


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
    category = await db.get(ProductCategory, data.category_id)
    if not category:
        raise HTTPException(status_code=400, detail="Category not found")

    row = ClientProduct(
        client_id=client_id,
        category_id=data.category_id,
        display_name=data.display_name,
        manufacturer=data.manufacturer,
        grade=data.grade,
        formulation=data.formulation,
        n_pct=data.n_pct,
        p2o5_pct=data.p2o5_pct,
        k2o_pct=data.k2o_pct,
        ca_pct=data.ca_pct,
        mg_pct=data.mg_pct,
        s_pct=data.s_pct,
        zn_pct=data.zn_pct,
        fe_pct=data.fe_pct,
        mn_pct=data.mn_pct,
        cu_pct=data.cu_pct,
        b_pct=data.b_pct,
        application_methods=data.application_methods,
        data_json=data.data_json,
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
    return reloaded


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

    if data.category_id is not None:
        category = await db.get(ProductCategory, data.category_id)
        if not category:
            raise HTTPException(status_code=400, detail="Category not found")

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
    return reloaded


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
    return reloaded


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


@router.get(
    "/client/{client_id}/soil-nutrient/product-categories",
    response_model=list[ProductCategoryOut],
)
async def list_client_portal_product_categories(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mirror of /sa/soil-nutrient/product-categories for Client Portal.
    Client Portal consumes these to render the authoring form fields
    (field schema is platform-wide per Decision L3).
    """
    await _assert_can_edit_client_soil_nutrient(db, current_user.id, client_id)
    q = select(ProductCategory).options(
        selectinload(ProductCategory.fields),
    ).where(
        ProductCategory.status == "ACTIVE",
    ).order_by(ProductCategory.display_order)
    result = await db.execute(q)
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
        else:
            new_row = ClientNormalRange(
                client_id=client_id,
                crop_cosh_id=crop_cosh_id,
                parameter_id=row.parameter_id,
                operator=row.operator,
                low_value=row.low_value,
                high_value=row.high_value,
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
        else:
            new_row = PackageNormalRangeOverride(
                package_id=package_id,
                parameter_id=row.parameter_id,
                operator=row.operator,
                low_value=row.low_value,
                high_value=row.high_value,
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
