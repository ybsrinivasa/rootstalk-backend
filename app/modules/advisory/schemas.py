from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from app.modules.advisory.models import PackageType, PackageStatus, TimelineFromType, PracticeL0, RelationType, ConditionalAnswer


# ── Package ────────────────────────────────────────────────────────────────────

class PackageCreate(BaseModel):
    crop_cosh_id: str
    name: str
    package_type: PackageType
    # Annual: required, 1-365. Perennial: input is ignored (forced to 365).
    duration_days: Optional[int] = None
    # Mandatory per spec §4.1 — fixed Cosh list (Sowing/Planting/Pruning Date).
    # Editable post-create via PackageUpdate.
    start_date_label_cosh_id: str
    description: Optional[str] = None


class PackageUpdate(BaseModel):
    name: Optional[str] = None
    duration_days: Optional[int] = None
    start_date_label_cosh_id: Optional[str] = None
    description: Optional[str] = None
    # Batch 28: SE-driven status toggle. ACTIVE ↔ INACTIVE freely;
    # DRAFT → INACTIVE allowed (per user 2026-05-14: SE may want to
    # discard a draft); DRAFT → ACTIVE blocked here — must go through
    # the publish endpoint so lineage migration runs correctly.
    status: Optional[str] = None


class PackageLocationIn(BaseModel):
    state_cosh_id: str
    district_cosh_id: str


class PackageAuthorIn(BaseModel):
    """Input row for PUT /packages/{id}/authors — one per Subject
    Expert credited on the Package. `user_id` must reference an
    ACTIVE ClientUser of the same client with role SUBJECT_EXPERT
    (validated server-side; spec §4.1)."""
    user_id: str
    designation: Optional[str] = None
    professional_profile: Optional[str] = None
    display_order: int = 0


class PackageAuthorOut(BaseModel):
    id: str
    user_id: str
    user_name: Optional[str] = None  # joined from User.name for portal rendering
    designation: Optional[str] = None
    professional_profile: Optional[str] = None
    display_order: int

    class Config:
        from_attributes = True


class PackageOut(BaseModel):
    id: str
    client_id: Optional[str] = None
    parent_global_id: Optional[str] = None
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

class VariableNameIn(BaseModel):
    """Inline variable input for the atomic Parameter create endpoint
    (Batch 28). The Parameter and its first ≥ 2 variables are
    created in a single round-trip so a half-created Parameter (one
    with zero or one variable) never exists in the DB."""
    name: str


class ParameterCreate(BaseModel):
    crop_cosh_id: str
    name: str
    display_order: int = 0
    # Batch 28: a CUSTOM Parameter must ship with at least 2 variables
    # at creation. Per user 2026-05-14 — a one-variable parameter is
    # never semantically useful (PoP signature picker needs choices).
    variables: list[VariableNameIn] = Field(..., min_length=2)


class ParameterUpdate(BaseModel):
    """Rename a CUSTOM Parameter (Batch 28). Only the display name is
    editable; crop_cosh_id and source are immutable."""
    name: Optional[str] = None
    display_order: Optional[int] = None


class VariableCreate(BaseModel):
    parameter_id: str
    name: str


class VariableUpdate(BaseModel):
    """Rename a Variable under a CUSTOM Parameter (Batch 28)."""
    name: Optional[str] = None


class VariableOut(BaseModel):
    """Serialized Variable — Batch 35 (2026-05-14) introduces this to
    surface `cosh_id` to the SA portal so it can gate per-variable
    edit/delete (NULL = SE-added, editable; non-NULL = Cosh-mirrored,
    read-only) independently of the parent Parameter's source."""
    id: str
    parameter_id: str
    cosh_id: Optional[str] = None
    name: str
    status: str

    class Config:
        from_attributes = True


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
    # Batch 28: SE toggles ACTIVE / INACTIVE. from_type stays immutable
    # — type-vs-package consistency is fixed at create time.
    status: Optional[str] = None


class TimelineOut(BaseModel):
    id: str
    package_id: str
    name: str
    from_type: TimelineFromType
    from_value: int
    to_value: int
    display_order: int
    status: str = "ACTIVE"
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
    frequency_days: Optional[int] = None
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


class ElementWithLabelOut(BaseModel):
    """Element row enriched for read-back display (Batch 32).

    `label` is the human-readable field name from the rule book
    (e.g. "Common Name" for COMMON_NAME). `display_value` is the
    server-resolved English name for `cosh_ref` references, or the
    raw `value` for free-text / numeric fields. Frontend renders
    `{label}: {display_value}` without any further lookups."""
    element_type: str
    label: str
    cosh_ref: Optional[str] = None
    value: Optional[str] = None
    unit_cosh_id: Optional[str] = None
    display_value: Optional[str] = None
    display_order: int = 0


class PracticeWithElementsOut(BaseModel):
    """List shape for the Practice index (Batch 32) — bundles each
    Practice with its resolved-label Element rows so the SA portal
    can expand a Practice inline without a follow-on per-element
    fetch."""
    id: str
    timeline_id: str
    l0_type: PracticeL0
    l1_type: Optional[str] = None
    l2_type: Optional[str] = None
    display_order: int
    is_special_input: bool
    relation_id: Optional[str] = None
    created_at: datetime
    elements: list[ElementWithLabelOut] = []

    class Config:
        from_attributes = True


# ── Relations ──────────────────────────────────────────────────────────────────

class RelationCreate(BaseModel):
    """Save-time payload for `POST /timelines/{id}/relations`.

    `parts` is a 3-D list mirroring the spec's mental model:
        parts[i][j][k] = practice_id at Part i+1, Option j+1, Position k+1

    Pure AND of N practices: `[[[p1, p2, ..., pN]]]` (single Part,
    single Option, N positions).
    Pure OR of N practices: `[[[p1], [p2], ..., [pN]]]` (single
    Part, N Options of 1 position each).
    Mixed AND-OR per spec §10.1 example "(A+B) or (C) or (D) +
    (P or Q) + (M) or (N)":
        [[[A, B], [C], [D]],
         [[P], [Q]],
         [[M], [N]]]

    `expression` is a human-readable display string the CA portal
    builds for confirmation; the backend stores it for read-back
    but doesn't parse or validate it.
    """
    relation_type: RelationType
    expression: Optional[str] = None
    parts: List[List[List[str]]]


# ── Conditional Questions ──────────────────────────────────────────────────────

class ConditionalQuestionCreate(BaseModel):
    question_text: str
    display_order: int = 0


class CQAttachmentIn(BaseModel):
    """Batch 39G (2026-05-15) — one side (YES or NO) of a Conditional
    Question's binding. `kind` distinguishes Path B (practice) from
    Path A (relation). Used inside `CQReplace`."""
    kind: str  # "practice" | "relation"
    id: str


class CQReplace(BaseModel):
    """Atomic replace payload for `PUT /conditional-questions/{id}`.

    The handler updates the question text and replaces both side
    attachments in one transaction — wipe existing PracticeConditional
    / RelationConditional rows for the CQ, then insert whatever YES /
    NO carries (each may be None to leave that side unbound). Mirrors
    the create-time spec so the SE's mental model is "one CQ row + at
    most one Practice/Relation per answer side"."""
    question_text: str
    yes: Optional[CQAttachmentIn] = None
    no: Optional[CQAttachmentIn] = None


class PracticeConditionalCreate(BaseModel):
    practice_id: str
    question_id: str
    answer: ConditionalAnswer


# ── PG Recommendations ────────────────────────────────────────────────────────

class PGRecommendationCreate(BaseModel):
    problem_group_cosh_id: str
    # CHA-hub: each (client, PG, area_or_plant) is its own bundle.
    # Required for client-local creates; tolerated as None on legacy
    # global rows (will be filled by Cosh-side authoring later).
    area_or_plant: Optional[str] = None


class PGTimelineCreate(BaseModel):
    name: str
    from_type: str = "DAYS_AFTER_DETECTION"
    from_value: int = 0
    to_value: int


class PGPracticeCreate(BaseModel):
    l0_type: str
    l1_type: Optional[str] = None
    l2_type: Optional[str] = None
    display_order: int = 0
    is_special_input: bool = False
    frequency_days: Optional[int] = None
    elements: List["ElementIn"] = []


class PGPracticeOut(BaseModel):
    id: str
    timeline_id: str
    l0_type: str
    l1_type: Optional[str] = None
    l2_type: Optional[str] = None
    display_order: int
    is_special_input: bool
    created_at: datetime

    class Config:
        from_attributes = True


class PGTimelineOut(BaseModel):
    id: str
    # Both parents are nullable since pg_timelines is polymorphic
    # post-2026-05-09 (UCAT pipe-3 / commit 4b8e2c1a93f5). Exactly
    # one of these is populated per row, enforced by the DB CHECK
    # `pg_timelines_one_parent_chk`.
    pg_recommendation_id: Optional[str] = None
    standard_response_id: Optional[str] = None
    parent_kind: Optional[str] = None  # 'PG' | 'QA' — derived
    name: str
    from_type: str
    from_value: int
    to_value: int
    practices: List[PGPracticeOut] = []

    class Config:
        from_attributes = True


class QATimelineCreate(BaseModel):
    """Q&A timeline creation. Same shape as PGTimelineCreate but the
    anchor unit defaults to DAYS_AFTER_RESPONSE — the unit that
    matches the spec §14.9 framing ("days after the Pundit's
    response is delivered to the farmer")."""
    name: str
    from_type: str = "DAYS_AFTER_RESPONSE"
    from_value: int = 0
    to_value: int


class QAPracticeCreate(BaseModel):
    """Q&A practice creation. Identical to PGPracticeCreate by design —
    UCAT means Practice and Element shapes are pipe-agnostic."""
    l0_type: str
    l1_type: Optional[str] = None
    l2_type: Optional[str] = None
    display_order: int = 0
    is_special_input: bool = False
    frequency_days: Optional[int] = None
    elements: List["ElementIn"] = []


class PGRecommendationOut(BaseModel):
    id: str
    problem_group_cosh_id: str
    client_id: Optional[str] = None
    parent_id: Optional[str] = None
    area_or_plant: Optional[str] = None
    status: str
    version: int
    created_at: datetime

    class Config:
        from_attributes = True


# ── SP Recommendations ────────────────────────────────────────────────────────

class SPRecommendationCreate(BaseModel):
    specific_problem_cosh_id: str
    # CHA-SP hub: crop is picked before the specific problem; snapshot
    # it on the row for fast filtering + audit clarity.
    crop_cosh_id: str


class SPTimelineCreate(BaseModel):
    name: str
    from_type: str = "DAYS_AFTER_DETECTION"
    from_value: int = 0
    to_value: int


class SPPracticeCreate(BaseModel):
    l0_type: str
    l1_type: Optional[str] = None
    l2_type: Optional[str] = None
    display_order: int = 0
    is_special_input: bool = False
    frequency_days: Optional[int] = None
    elements: List["ElementIn"] = []


class SPPracticeOut(BaseModel):
    id: str
    timeline_id: str
    l0_type: str
    l1_type: Optional[str] = None
    l2_type: Optional[str] = None
    display_order: int
    is_special_input: bool
    created_at: datetime

    class Config:
        from_attributes = True


class SPTimelineOut(BaseModel):
    id: str
    sp_recommendation_id: str
    name: str
    from_type: str
    from_value: int
    to_value: int
    practices: List[SPPracticeOut] = []

    class Config:
        from_attributes = True


class SPRecommendationOut(BaseModel):
    id: str
    specific_problem_cosh_id: str
    client_id: str
    crop_cosh_id: Optional[str] = None
    status: str
    version: int
    created_at: datetime

    class Config:
        from_attributes = True
