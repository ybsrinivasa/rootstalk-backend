from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from app.modules.advisory.models import PackageType, PackageStatus, TimelineFromType, PracticeL0, RelationType, ConditionalAnswer


# ── Package ────────────────────────────────────────────────────────────────────

class PackageCreate(BaseModel):
    crop_cosh_id: str
    name: str
    package_type: PackageType
    duration_days: Optional[int] = None
    start_date_label_cosh_id: Optional[str] = None
    description: Optional[str] = None


class PackageUpdate(BaseModel):
    name: Optional[str] = None
    duration_days: Optional[int] = None
    start_date_label_cosh_id: Optional[str] = None
    description: Optional[str] = None


class PackageLocationIn(BaseModel):
    state_cosh_id: str
    district_cosh_id: str


class PackageOut(BaseModel):
    id: str
    client_id: str
    crop_cosh_id: str
    name: str
    package_type: PackageType
    duration_days: int
    start_date_label_cosh_id: Optional[str] = None
    description: Optional[str] = None
    status: PackageStatus
    version: int
    published_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ── Parameters and Variables ───────────────────────────────────────────────────

class ParameterCreate(BaseModel):
    crop_cosh_id: str
    name: str
    display_order: int = 0


class VariableCreate(BaseModel):
    parameter_id: str
    name: str


class PackageVariableSet(BaseModel):
    """Set the parameter→variable fingerprint for a package."""
    assignments: List[dict]  # [{"parameter_id": ..., "variable_id": ...}]


# ── Timeline ───────────────────────────────────────────────────────────────────

class TimelineCreate(BaseModel):
    name: str
    from_type: TimelineFromType
    from_value: int
    to_value: int
    display_order: int = 0


class TimelineUpdate(BaseModel):
    name: Optional[str] = None
    from_value: Optional[int] = None
    to_value: Optional[int] = None


class TimelineOut(BaseModel):
    id: str
    package_id: str
    name: str
    from_type: TimelineFromType
    from_value: int
    to_value: int
    display_order: int
    created_at: datetime

    class Config:
        from_attributes = True


# ── Practice and Elements ──────────────────────────────────────────────────────

class ElementIn(BaseModel):
    element_type: str
    cosh_ref: Optional[str] = None
    value: Optional[str] = None
    unit_cosh_id: Optional[str] = None
    display_order: int = 0


class PracticeCreate(BaseModel):
    l0_type: PracticeL0
    l1_type: Optional[str] = None
    l2_type: Optional[str] = None
    display_order: int = 0
    is_special_input: bool = False
    elements: List[ElementIn] = []


class PracticeOut(BaseModel):
    id: str
    timeline_id: str
    l0_type: PracticeL0
    l1_type: Optional[str] = None
    l2_type: Optional[str] = None
    display_order: int
    is_special_input: bool
    relation_id: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ── Relations ──────────────────────────────────────────────────────────────────

class RelationCreate(BaseModel):
    relation_type: RelationType
    expression: Optional[str] = None
    practice_ids: List[str] = []


# ── Conditional Questions ──────────────────────────────────────────────────────

class ConditionalQuestionCreate(BaseModel):
    question_text: str
    display_order: int = 0


class PracticeConditionalCreate(BaseModel):
    practice_id: str
    question_id: str
    answer: ConditionalAnswer
