"""Pydantic schemas for the Soil Nutrient Management module (SA Portal).

Phase 1 — platform-wide seed data CRUD + Client flag toggles.
See `project_soil_nutrient_module_design.md` Decisions L, M, N, P.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.modules.soil_nutrient.models import (
    CategoryGroup, NormalRangeOperator, ProductCategoryScope,
    ProductFieldType,
)


# ── Platform Soil Parameters (Decision N canon) ─────────────────────────────

class SoilParameterCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    unit: Optional[str] = Field(None, max_length=40)
    category_group: CategoryGroup
    formula: Optional[str] = Field(None, max_length=200)
    measurement_method_note: Optional[str] = None
    display_order: int = 0


class SoilParameterUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    unit: Optional[str] = Field(None, max_length=40)
    category_group: Optional[CategoryGroup] = None
    formula: Optional[str] = Field(None, max_length=200)
    measurement_method_note: Optional[str] = None
    display_order: Optional[int] = None
    status: Optional[str] = Field(None, pattern="^(ACTIVE|INACTIVE)$")


class SoilParameterOut(BaseModel):
    id: str
    name: str
    unit: Optional[str]
    category_group: CategoryGroup
    formula: Optional[str]
    measurement_method_note: Optional[str]
    display_order: int
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ── Product Categories + Fields (Decision L) ────────────────────────────────

class ProductCategoryFieldCreate(BaseModel):
    field_key: str = Field(..., min_length=1, max_length=100)
    field_label: str = Field(..., min_length=1, max_length=200)
    field_type: ProductFieldType
    required: bool = False
    unit: Optional[str] = Field(None, max_length=40)
    enum_values_json: Optional[dict] = None
    display_order: int = 0


class ProductCategoryFieldUpdate(BaseModel):
    field_key: Optional[str] = Field(None, min_length=1, max_length=100)
    field_label: Optional[str] = Field(None, min_length=1, max_length=200)
    field_type: Optional[ProductFieldType] = None
    required: Optional[bool] = None
    unit: Optional[str] = Field(None, max_length=40)
    enum_values_json: Optional[dict] = None
    display_order: Optional[int] = None
    status: Optional[str] = Field(None, pattern="^(ACTIVE|INACTIVE)$")


class ProductCategoryFieldOut(BaseModel):
    id: str
    category_id: str
    field_key: str
    field_label: str
    field_type: ProductFieldType
    required: bool
    unit: Optional[str]
    enum_values_json: Optional[dict]
    display_order: int
    status: str

    class Config:
        from_attributes = True


class ProductCategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    scope: ProductCategoryScope = ProductCategoryScope.GLOBAL
    display_order: int = 0


class ProductCategoryUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    display_order: Optional[int] = None
    status: Optional[str] = Field(None, pattern="^(ACTIVE|INACTIVE)$")


class ProductCategoryOut(BaseModel):
    id: str
    name: str
    scope: ProductCategoryScope
    display_order: int
    status: str
    created_at: datetime
    updated_at: datetime
    fields: list[ProductCategoryFieldOut] = []

    class Config:
        from_attributes = True


# ── Preset Soil Profiles (Decision P + MQ5-B) ──────────────────────────────

class PresetSoilProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    # Keyed by platform_soil_parameters.name
    values_json: dict
    display_order: int = 0


class PresetSoilProfileUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    values_json: Optional[dict] = None
    display_order: Optional[int] = None
    status: Optional[str] = Field(None, pattern="^(ACTIVE|INACTIVE)$")


class PresetSoilProfileOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    values_json: dict
    display_order: int
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ── Fallback Catalogue (Decision U — pointer over Cosh) ─────────────────────

class FallbackProductCreate(BaseModel):
    """Pointer-shape create. Caller passes a Cosh product id; the server
    verifies it exists in `load_fertiliser_candidates()` before insert."""
    cosh_product_id: str = Field(..., min_length=1, max_length=200)
    notes: Optional[str] = None
    # Optional crop restriction. Empty list / None = universal.
    crop_cosh_ids: Optional[list[str]] = None


class FallbackProductUpdate(BaseModel):
    notes: Optional[str] = None
    status: Optional[str] = Field(None, pattern="^(ACTIVE|INACTIVE)$")
    # Replace-all semantics: pass [] to clear all crop assignments
    # (making the product universal); pass None to leave assignments
    # unchanged.
    crop_cosh_ids: Optional[list[str]] = None


class FallbackProductCropAssignmentOut(BaseModel):
    id: str
    crop_cosh_id: str
    status: str

    class Config:
        from_attributes = True


class CoshFertilizerConcentrationOut(BaseModel):
    """Mirror of `app.services.npk_candidates.Concentration` — grams of
    N / P2O5 / K2O per kg of product (as modelled by Cosh)."""
    n: float
    p: float
    k: float


class CoshFertilizerCandidateOut(BaseModel):
    """One row from `load_fertiliser_candidates()`. Shared shape for the
    picker (SA Portal) and the hydrated Fallback list view."""
    cosh_id: str
    name: str
    concentration: CoshFertilizerConcentrationOut
    water_soluble: bool


class FallbackProductOut(BaseModel):
    """Hydrated view — pointer row joined with its Cosh resolution.
    `cosh` is `None` only in the unlikely case that the referenced
    Cosh product has been removed since we linked it (surface a
    warning in the UI)."""
    id: str
    cosh_product_id: str
    status: str
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime
    crop_assignments: list[FallbackProductCropAssignmentOut] = []
    cosh: Optional[CoshFertilizerCandidateOut] = None

    class Config:
        from_attributes = True


# ── Client Products (Decision L) ────────────────────────────────────────────

class ClientProductCreate(BaseModel):
    category_id: str
    display_name: str = Field(..., min_length=1, max_length=500)
    manufacturer: Optional[str] = Field(None, max_length=500)
    grade: Optional[str] = Field(None, max_length=100)
    formulation: Optional[str] = Field(None, max_length=100)
    n_pct: Optional[float] = None
    p2o5_pct: Optional[float] = None
    k2o_pct: Optional[float] = None
    ca_pct: Optional[float] = None
    mg_pct: Optional[float] = None
    s_pct: Optional[float] = None
    zn_pct: Optional[float] = None
    fe_pct: Optional[float] = None
    mn_pct: Optional[float] = None
    cu_pct: Optional[float] = None
    b_pct: Optional[float] = None
    application_methods: Optional[list[str]] = None
    data_json: Optional[dict] = None
    crop_cosh_ids: Optional[list[str]] = None


class ClientProductUpdate(BaseModel):
    category_id: Optional[str] = None
    display_name: Optional[str] = Field(None, min_length=1, max_length=500)
    manufacturer: Optional[str] = Field(None, max_length=500)
    grade: Optional[str] = Field(None, max_length=100)
    formulation: Optional[str] = Field(None, max_length=100)
    n_pct: Optional[float] = None
    p2o5_pct: Optional[float] = None
    k2o_pct: Optional[float] = None
    ca_pct: Optional[float] = None
    mg_pct: Optional[float] = None
    s_pct: Optional[float] = None
    zn_pct: Optional[float] = None
    fe_pct: Optional[float] = None
    mn_pct: Optional[float] = None
    cu_pct: Optional[float] = None
    b_pct: Optional[float] = None
    application_methods: Optional[list[str]] = None
    data_json: Optional[dict] = None
    status: Optional[str] = Field(None, pattern="^(ACTIVE|DISCONTINUED)$")
    crop_cosh_ids: Optional[list[str]] = None


class ClientProductCropAssignmentOut(BaseModel):
    id: str
    crop_cosh_id: str
    status: str

    class Config:
        from_attributes = True


class ClientProductOut(BaseModel):
    id: str
    client_id: str
    category_id: str
    display_name: str
    manufacturer: Optional[str]
    grade: Optional[str]
    formulation: Optional[str]
    n_pct: Optional[float]
    p2o5_pct: Optional[float]
    k2o_pct: Optional[float]
    ca_pct: Optional[float]
    mg_pct: Optional[float]
    s_pct: Optional[float]
    zn_pct: Optional[float]
    fe_pct: Optional[float]
    mn_pct: Optional[float]
    cu_pct: Optional[float]
    b_pct: Optional[float]
    application_methods: Optional[list[str]]
    data_json: Optional[dict]
    status: str
    created_at: datetime
    updated_at: datetime
    crop_assignments: list[ClientProductCropAssignmentOut] = []

    class Config:
        from_attributes = True


# ── Normal Ranges (Decision N) ──────────────────────────────────────────────

class NormalRangeIn(BaseModel):
    """Input shape for both crop-level defaults and Package overrides.

    Validation rules (enforced in router):
      - RANGE: both low_value and high_value required, low <= high.
      - UPPER_BOUND: only high_value required (low_value must be None).
      - LOWER_BOUND: only low_value required (high_value must be None).
    """
    parameter_id: str
    operator: NormalRangeOperator
    low_value: Optional[float] = None
    high_value: Optional[float] = None


class ClientNormalRangeOut(BaseModel):
    id: str
    client_id: str
    crop_cosh_id: str
    parameter_id: str
    operator: NormalRangeOperator
    low_value: Optional[float]
    high_value: Optional[float]
    updated_at: datetime

    class Config:
        from_attributes = True


class PackageNormalRangeOverrideOut(BaseModel):
    id: str
    package_id: str
    parameter_id: str
    operator: NormalRangeOperator
    low_value: Optional[float]
    high_value: Optional[float]
    updated_at: datetime

    class Config:
        from_attributes = True


class NormalRangeBulkUpsert(BaseModel):
    """Bulk upsert payload — a single Save at the top of the Normal
    Ranges page writes every edited row in one call."""
    rows: list[NormalRangeIn]


# ── Client flag toggles (Phase 1 — SA Portal only) ─────────────────────────

class SoilNutrientModuleToggle(BaseModel):
    enabled: bool


class SoilTestingLabToggle(BaseModel):
    enabled: bool


class ClientSoilNutrientStatusOut(BaseModel):
    """Returned from Client flag endpoints. Confirms the current flag
    state after a toggle so the UI can refresh without a separate
    read."""
    client_id: str
    soil_nutrient_module_enabled: bool
    has_soil_testing_lab: bool
