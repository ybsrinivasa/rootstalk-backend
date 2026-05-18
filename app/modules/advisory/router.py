from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import StatusEnum, User
from app.modules.advisory.models import (
    Package, PackageLocation, PackageAuthor, PackageVariable,
    Parameter, Variable, PackageVariable,
    ParameterTranslation, VariableTranslation, TranslationStatus,
    Timeline, Practice, Element, Relation,
    ConditionalAnswer, ConditionalQuestion, PracticeConditional,
    RelationConditional,
    PackageStatus, PackageType, PackageCreatedVia, ParameterSource,
)
from app.modules.advisory.schemas import (
    PackageCreate, PackageUpdate, PackageOut,
    PackageLocationIn, PackageAuthorIn, PackageAuthorOut,
    PackagePushRequest, PVAssignmentIn,
    ParameterCreate, ParameterUpdate, VariableCreate, VariableUpdate, VariableOut,
    PackageVariableSet,
    TimelineCreate, TimelineUpdate, TimelineOut,
    PracticeCreate, PracticeOut, PracticeWithElementsOut,
    RelationCreate, ConditionalQuestionCreate, PracticeConditionalCreate, CQReplace,
    PGRecommendationCreate, PGRecommendationOut, PGRecommendationUpdate,
    PGTimelineCreate, PGTimelineOut, PGTimelineUpdate, PGPracticeCreate,
    SPRecommendationCreate, SPRecommendationOut, SPTimelineCreate, SPTimelineOut, SPPracticeCreate,
    QATimelineCreate, QAPracticeCreate,
    ElementIn,
)
from app.modules.advisory.models import (
    PGRecommendation, SPRecommendation,
)
from app.modules.clients.models import ClientCrop, ClientLocation, ClientUser, ClientUserRole
from app.modules.sync.models import CoshCoreItem
from app.services.cosh_constants import COSH_BIOLOGICAL_NAMES_CORE
from app.services.bl13_versioning import (
    compute_publish_version, validate_publish_transition,
)
from app.services.crop_lifecycle import (
    CropNotOnBeltError, assert_crop_on_belt,
)
from app.services.package_validation import (
    PackageValidationError,
    validate_package_duration_for_create,
    validate_package_duration_for_update,
)
from app.services.pv_uniqueness import (
    PVConflictError, assert_pv_unique_for_package,
)
from app.services.pv_consistency import (
    PVConsistencyError, assert_pv_consistency_for_package,
)
from app.services.publish_validation import (
    PublishBlockedError, assert_package_publish_ready,
    assert_global_package_publish_ready, assert_global_pg_publish_ready,
)
from app.services.timeline_validation import (
    TimelineValidationError, validate_timeline,
)
from app.services.relations import PracticeRef, decode_role
from app.services.relation_validation import (
    RelationValidationFailed, validate_relation_save,
)
from app.services.conditional_validation import (
    ConditionalValidationError,
    assert_practice_can_be_linked_to_conditional,
    assert_practices_have_no_independent_conditional,
    assert_relation_can_be_linked_to_conditional,
)
from app.services.l2_element_validator import (
    L2ElementValidationError, assert_l2_elements_valid,
)


def _raise_conditional_validation(e: ConditionalValidationError):
    raise HTTPException(
        status_code=422,
        detail={"code": e.code, "message": e.message, **(e.extra or {})},
    )


def _raise_relation_validation(e: RelationValidationFailed):
    """Map RelationValidationFailed to a 422 with the complete list
    of violated rules so the CA portal can render a checklist."""
    raise HTTPException(
        status_code=422,
        detail={
            "code": e.code,
            "message": str(e),
            "errors": [
                {"code": err.code, "message": err.message, **(err.extra or {})}
                for err in e.errors
            ],
        },
    )


def _raise_timeline_validation(e: TimelineValidationError):
    raise HTTPException(
        status_code=422,
        detail={"code": e.code, "message": e.message},
    )


def _validate_brand_lock(request: "PracticeCreate") -> None:
    """Batch 39I-a (2026-05-16) — `is_brand_locked=True` is only valid
    when the Practice carries a BRAND_NAME element with a non-empty
    cosh_ref. The SE opts in to brand-lock per Practice; the flag is
    meaningless without a Trade Name."""
    if not request.is_brand_locked:
        return
    has_brand = any(
        e.element_type == "BRAND_NAME" and (e.cosh_ref or "").strip()
        for e in request.elements
    )
    if not has_brand:
        raise HTTPException(status_code=422, detail={
            "code": "brand_lock_requires_brand_name",
            "message": "Lock Brand requires a Trade Name (BRAND_NAME) to be set on the Practice.",
        })


async def _assert_can_edit_client_advisory(
    db: AsyncSession, user_id: str, client_id: str,
) -> None:
    """Authorisation gate for CA-side advisory writes — covers all
    four pipes: CCA, CHA-PG, CHA-SP, QA.

    Batch J (2026-05-18) tightens from "any ACTIVE ClientUser" to
    "ACTIVE SUBJECT_EXPERT only". Per user 2026-05-17: "Only the
    Subject Experts have the right to manage CCA, PG, SP, and QA.
    No other role can do this, not even the CA."

    Accepts:
      1. ACTIVE ClientUser of this client with role SUBJECT_EXPERT.
      2. ACTIVE CMClientAssignment with EDIT rights — per the
         documented Ram-direct-edit pattern the CM may edit a
         client's Local advisory content directly without going
         through pull.

    Refused (with distinct codes for clearer UX):
      - Non-SE ClientUser (CA / FIELD_MANAGER / REPORT_USER) →
        403 `subject_expert_only`. They retain View access via
        unchanged GET endpoints.
      - Not a member + not a CM-EDIT assignee → 403 `ca_edit_forbidden`.

    Cross-tenant access is already refused at the request boundary
    by `get_current_user` (cross_client_forbidden); this gate runs
    after that and only sees same-client attempts.
    """
    from app.modules.clients.models import (
        CMClientAssignment, CMRights, ClientUser, ClientUserRole,
    )
    from app.modules.platform.models import StatusEnum

    cu = (await db.execute(
        select(ClientUser).where(
            ClientUser.user_id == user_id,
            ClientUser.client_id == client_id,
            ClientUser.status == StatusEnum.ACTIVE,
        ).limit(1)
    )).scalar_one_or_none()
    if cu is not None:
        if cu.role == ClientUserRole.SUBJECT_EXPERT:
            return
        # Member but wrong role — surface a specific error so the UI
        # can render a helpful "ask your Subject Expert to do this"
        # message instead of a generic 403.
        raise HTTPException(
            status_code=403,
            detail={
                "code": "subject_expert_only",
                "message": (
                    "Only Subject Experts can edit advisory content. "
                    "Your role can view but not modify CCA / PG / SP / QA."
                ),
                "your_role": cu.role.value,
            },
        )

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
                "Editing advisory content requires either an active "
                "Subject Expert role on this client, or an active Content "
                "Manager assignment with EDIT rights."
            ),
        },
    )


async def _assert_pg_recommendation_draft(
    db: AsyncSession, pg_id: str, *, client_id: str,
) -> "PGRecommendation":
    """Batch S (2026-05-18). Refuse mutations on ACTIVE / INACTIVE
    PGRecommendation rows — they leak straight to farmers (ACTIVE)
    or rewrite historical record (INACTIVE).

    The right edit path is clone-to-draft → edit DRAFT → publish.
    Returns the row so callers don't double-fetch.
    """
    from app.modules.advisory.models import PGRecommendation
    pg = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.id == pg_id,
            PGRecommendation.client_id == client_id,
        )
    )).scalar_one_or_none()
    if pg is None:
        raise HTTPException(
            status_code=404, detail="PG recommendation not found",
        )
    if pg.status != "DRAFT":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "pg_not_draft",
                "message": (
                    f"This PG recommendation is {pg.status.lower()}. "
                    "To make changes, start a new draft from the "
                    "version-history panel."
                ),
                "current_status": pg.status,
            },
        )
    return pg


async def _assert_can_view_client_advisory(
    db: AsyncSession, user_id: str, client_id: str,
) -> None:
    """Authorisation gate for CA-side advisory READS.

    Batch K (2026-05-18): non-SE / non-CA roles cannot see CCA / PG /
    SP / QA content at all — not even via direct URL. The sidebar
    hides the items but the backend is the audit-grade guarantee.

    Accepts:
      1. ACTIVE ClientUser of this client with role SUBJECT_EXPERT or CA.
      2. ACTIVE CMClientAssignment with EDIT rights (Ram-direct-view
         path — CMs administer the client's content and need read
         access too).

    Refused (403 `advisory_view_forbidden`):
      - Any other ACTIVE ClientUser role (FIELD_MANAGER /
        SEED_DATA_MANAGER / REPORT_USER / CLIENT_RM / PRODUCT_MANAGER).
      - Not a member of this client AND not a CM-EDIT assignee.
    """
    from app.modules.clients.models import (
        CMClientAssignment, CMRights, ClientUser, ClientUserRole,
    )
    from app.modules.platform.models import StatusEnum

    cu_rows = (await db.execute(
        select(ClientUser).where(
            ClientUser.user_id == user_id,
            ClientUser.client_id == client_id,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalars().all()
    if any(
        cu.role in (ClientUserRole.SUBJECT_EXPERT, ClientUserRole.CA)
        for cu in cu_rows
    ):
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
            "code": "advisory_view_forbidden",
            "message": (
                "Viewing advisory content requires the Subject Expert "
                "or CA role on this company. Ask your CA to grant "
                "the appropriate role."
            ),
        },
    )


async def _assert_sa_or_cm(
    db: AsyncSession, current_user: "User",
) -> None:
    """Authorisation gate for SA-Portal-scope writes — Global advisory
    content (/advisory/global/*) and platform admin endpoints
    (/admin/crop-health-crops/*, etc.). The SA Portal exposes only
    CCA + PG at the Global level; SP and QA are CA-Portal-only.

    Accepts:
      1. The SA identity — `current_user.email == settings.sa_email`
         (single SA, env-configured; mirror of the frontend `is_sa`
         flag at /portal/auth/me).
      2. An ACTIVE UserRole(role_type=CONTENT_MANAGER).

    Rejects:
      - RM / BM / FARMER / DEALER / FACILITATOR / FARM_PUNDIT roles
        (they don't author Global content).
      - CA-Portal-only ClientUsers (SUBJECT_EXPERT / CA /
        FIELD_MANAGER / REPORT_USER) with no SA-Portal role — closes
        the cross-portal hole flagged by user 2026-05-17.

    Raises 403 with stable code `global_edit_forbidden`. Mirrors
    Batch 39S's `_assert_can_edit_client_advisory` in the opposite
    direction (that one gates CA-side writes; this one gates SA-side).
    """
    from app.config import settings
    from app.modules.platform.models import RoleType, StatusEnum, UserRole

    if current_user.email and current_user.email == settings.sa_email:
        return

    cm = (await db.execute(
        select(UserRole.id).where(
            UserRole.user_id == current_user.id,
            UserRole.role_type == RoleType.CONTENT_MANAGER,
            UserRole.status == StatusEnum.ACTIVE,
        ).limit(1)
    )).scalar_one_or_none()
    if cm is not None:
        return

    raise HTTPException(
        status_code=403,
        detail={
            "code": "global_edit_forbidden",
            "message": (
                "Editing Global / platform content requires either the "
                "Super Admin identity or an active Content Manager role."
            ),
        },
    )


async def _assert_cm_can_edit_client(
    db: AsyncSession, user_id: str, client_id: str,
) -> None:
    """Authorisation gate for Global → Local exports of CCA / CHA
    content. Only an active CM with EDIT rights to the target client
    may import / fork content into that client's scope.

    Raises 403 with stable code `cm_assignment_required`. Used by
    fork_global_package, import_global_pg, import_pg_into_sp.

    Note: this is the V1 boundary check on the import pipe only. The
    broader `_require_client_role` audit (covering ~30 advisory
    mutating endpoints) remains a V2 task.
    """
    from app.modules.clients.models import CMClientAssignment, CMRights
    from app.modules.platform.models import StatusEnum

    assignment = (await db.execute(
        select(CMClientAssignment).where(
            CMClientAssignment.cm_user_id == user_id,
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
            CMClientAssignment.rights == CMRights.EDIT,
        )
    )).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "cm_assignment_required",
                "message": (
                    "Only a Content Manager with active EDIT rights for "
                    "this client may import or fork content into the "
                    "client's scope."
                ),
            },
        )


def _raise_l2_element_validation(e: L2ElementValidationError):
    """Map L2ElementValidationError to a 422 carrying the full error list
    so the CA portal can render every failed rule at once instead of
    forcing the SE through one-fix-per-roundtrip."""
    raise HTTPException(
        status_code=422,
        detail={
            "code": e.code,
            "message": str(e),
            "errors": [
                {
                    "code": err.code,
                    "field_name": err.field_name,
                    "message": err.message,
                    "details": err.details,
                }
                for err in e.errors
            ],
        },
    )


# ── Frequency-interval guard (Batch 34) ────────────────────────────────────
#
# Frequency-based L2s carry an INTERVAL element (FERTIGATION_INTERVAL /
# IRRIGATION_INTERVAL / REPEAT_INTERVAL). The first application is
# the day AFTER the timeline commences; subsequent applications fall
# every `interval` days. Per user 2026-05-14, the interval must
# produce ≥ 2 applications across the timeline window — if it'd
# yield only one, the SE should use a non-frequency practice
# instead. Formula:
#
#     duration  = |to_value - from_value|       (timeline span)
#     N         = floor((duration - 1) / interval) + 1
#
# Rejected with `frequency_too_long` 422 when N < 2.

def compute_number_of_applications(
    from_value: int, to_value: int, interval: int,
) -> int:
    """Pure helper — kept module-public so tests and the eventual
    farmer-advisory renderer can share the formula."""
    duration = abs(to_value - from_value)
    if interval <= 0 or duration < 1:
        return 0
    return (duration - 1) // interval + 1


async def _assert_interval_fits_timeline(
    db: AsyncSession, *,
    l2_type: Optional[str], elements, timeline_id: str,
) -> None:
    """Raises 422 frequency_too_long when N < 2 for a frequency-based
    L2. No-op for non-frequency L2s and when interval is missing /
    unparseable (the L2 validator handles those error codes)."""
    from app.services.l2_element_rules import get_l2_spec
    if l2_type is None:
        return
    spec = get_l2_spec(l2_type)
    if spec is None or not spec.frequency_based:
        return
    interval_field = next((f for f in spec.fields if f.is_interval), None)
    if interval_field is None:
        return
    raw_value: Optional[str] = None
    for e in elements:
        et = e.element_type if hasattr(e, "element_type") else e.get("element_type")
        if et != interval_field.name:
            continue
        v = e.value if hasattr(e, "value") else e.get("value")
        raw_value = v if v is not None else None
        break
    if not raw_value:
        return
    try:
        interval = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return
    timeline = (await db.execute(
        select(Timeline).where(Timeline.id == timeline_id)
    )).scalar_one_or_none()
    if timeline is None:
        return  # caller's 404 path will fire separately
    n_apps = compute_number_of_applications(
        from_value=timeline.from_value,
        to_value=timeline.to_value,
        interval=interval,
    )
    if n_apps < 2:
        duration = abs(timeline.to_value - timeline.from_value)
        raise HTTPException(status_code=422, detail={
            "code": "frequency_too_long",
            "message": (
                f"Interval {interval} produces only {n_apps} application"
                f"{'' if n_apps == 1 else 's'} across the timeline's "
                f"{duration}-day window. Frequency practices repeat at "
                f"least twice — either shorten the interval, lengthen "
                f"the timeline, or use a non-frequency practice."
            ),
            "details": {
                "interval": interval,
                "timeline_duration_days": duration,
                "number_of_applications": n_apps,
            },
        })


# ── Element-level CRUD helpers (Round 2 — element-level authoring) ─────────

def _element_row_to_in(row) -> ElementIn:
    """Coerce a persisted Element / Element / Element row back into
    the ElementIn shape the L2 validator consumes. Used when we need to
    re-validate a Practice's full element set after a per-element edit."""
    return ElementIn(
        element_type=row.element_type,
        cosh_ref=row.cosh_ref,
        value=row.value,
        unit_cosh_id=row.unit_cosh_id,
        display_order=row.display_order,
    )


def _element_row_to_out(row) -> dict:
    return {
        "id": row.id,
        "element_type": row.element_type,
        "cosh_ref": row.cosh_ref,
        "value": row.value,
        "unit_cosh_id": row.unit_cosh_id,
        "display_order": row.display_order,
    }


async def _revalidate_practice_elements(db, practice, expected_elements):
    """Run the L2 rule book over the proposed element set for a Practice.
    Same envelope as the create-time validator — 422 with the full error
    list — so the CA portal renders consistent feedback regardless of
    whether the SE saved the whole Practice or just tweaked one
    element."""
    try:
        await assert_l2_elements_valid(
            db,
            l2_type=practice.l2_type,
            elements=expected_elements,
            is_special_input=practice.is_special_input,
            frequency_days=practice.frequency_days,
        )
    except L2ElementValidationError as e:
        _raise_l2_element_validation(e)


async def _add_practice_element(db, *, practice, element_model, body):
    existing = (await db.execute(
        select(element_model).where(element_model.practice_id == practice.id)
    )).scalars().all()
    expected = [_element_row_to_in(e) for e in existing] + [body]
    await _revalidate_practice_elements(db, practice, expected)

    new_row = element_model(practice_id=practice.id, **body.model_dump())
    db.add(new_row)
    await db.commit()
    await db.refresh(new_row)
    return new_row


async def _update_practice_element(
    db, *, practice, element_model, element_id, body,
):
    element = (await db.execute(
        select(element_model).where(
            element_model.id == element_id,
            element_model.practice_id == practice.id,
        )
    )).scalar_one_or_none()
    if not element:
        raise HTTPException(status_code=404, detail="Element not found")
    siblings = (await db.execute(
        select(element_model).where(
            element_model.practice_id == practice.id,
            element_model.id != element_id,
        )
    )).scalars().all()
    expected = [_element_row_to_in(s) for s in siblings] + [body]
    await _revalidate_practice_elements(db, practice, expected)

    element.element_type = body.element_type
    element.cosh_ref = body.cosh_ref
    element.value = body.value
    element.unit_cosh_id = body.unit_cosh_id
    element.display_order = body.display_order
    await db.commit()
    await db.refresh(element)
    return element


async def _delete_practice_element(
    db, *, practice, element_model, element_id,
):
    """Validate the *remaining* element set still satisfies the rule book
    before persisting the delete. Pre-Round-2, an SE could re-save the
    whole Practice with a missing mandatory and get an immediate 422; the
    per-element delete preserves that guarantee. To wipe a mandatory
    element entirely, the SE deletes the whole Practice."""
    element = (await db.execute(
        select(element_model).where(
            element_model.id == element_id,
            element_model.practice_id == practice.id,
        )
    )).scalar_one_or_none()
    if not element:
        raise HTTPException(status_code=404, detail="Element not found")
    siblings = (await db.execute(
        select(element_model).where(
            element_model.practice_id == practice.id,
            element_model.id != element_id,
        )
    )).scalars().all()
    expected = [_element_row_to_in(s) for s in siblings]
    await _revalidate_practice_elements(db, practice, expected)

    await db.delete(element)
    await db.commit()


async def _assert_timeline_name_unique(
    db: AsyncSession, *, package_id: str, name: str,
    exclude_timeline_id: str | None = None,
) -> None:
    """Pre-check name uniqueness so we surface a friendly 422 instead
    of letting the DB unique constraint fire as a 500. `exclude_timeline_id`
    lets `update_timeline` re-check without false-positiving on its own
    row when the name isn't actually changing."""
    q = select(Timeline).where(
        Timeline.package_id == package_id,
        Timeline.name == name,
    )
    if exclude_timeline_id is not None:
        q = q.where(Timeline.id != exclude_timeline_id)
    existing = (await db.execute(q)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "timeline_name_duplicate",
                "message": f"A timeline named '{name}' already exists in this Package.",
            },
        )


def _raise_publish_blocked(e: PublishBlockedError):
    """Map a PublishBlockedError to a 422 with a complete checklist
    body. The CA portal renders one item per missing requirement so
    the expert can fix them all in a single pass."""
    raise HTTPException(
        status_code=422,
        detail={
            "code": e.code,
            "message": str(e),
            "missing": [
                {"code": m.code, "message": m.message, **(m.extra or {})}
                for m in e.missing
            ],
        },
    )


def _raise_pv_consistency(e: PVConsistencyError):
    """Map a PVConsistencyError to a 422 with both parameter sets
    surfaced so the CA portal can name precisely which parameters
    are missing/extra on this PoP vs the sibling."""
    raise HTTPException(
        status_code=422,
        detail={
            "code": e.code,
            "message": str(e),
            "violations": [
                {
                    "sibling_package_id": v.sibling_package_id,
                    "sibling_package_name": v.sibling_package_name,
                    "shared_districts": [
                        {"state_cosh_id": s, "district_cosh_id": d}
                        for s, d in v.shared_districts
                    ],
                    "this_parameter_ids": list(v.this_parameter_ids),
                    "sibling_parameter_ids": list(v.sibling_parameter_ids),
                }
                for v in e.violations
            ],
        },
    )


def _raise_pv_conflict(e: PVConflictError):
    """Map a PVConflictError to a 422 with a body the CA portal can
    surface. Each conflict carries the sibling's id+name and the
    shared districts so the portal can name them precisely."""
    raise HTTPException(
        status_code=422,
        detail={
            "code": e.code,
            "message": str(e),
            "conflicts": [
                {
                    "sibling_package_id": c.sibling_package_id,
                    "sibling_package_name": c.sibling_package_name,
                    "shared_districts": [
                        {"state_cosh_id": s, "district_cosh_id": d}
                        for s, d in c.shared_districts
                    ],
                }
                for c in e.conflicts
            ],
        },
    )
from app.services.bl17_timeline_boundary import (
    TimelineSpec, find_timeline_conflicts,
)

router = APIRouter(tags=["Advisory"])


def _raise_publish_transition(res, status_code: int = 400) -> None:
    """Convert a TransitionResult.allowed=False into an HTTPException
    carrying the stable error_code in the detail payload."""
    raise HTTPException(
        status_code=status_code,
        detail={"error_code": res.error_code, "message": res.message},
    )


def _require_client_role(current_user: User, client_id: str, *roles: ClientUserRole):
    """Check user has a valid role for this client."""
    pass  # Full role check wired in later — SA bypasses for now


# ── Packages ───────────────────────────────────────────────────────────────────

@router.get("/client/{client_id}/packages", response_model=list[PackageOut])
async def list_packages(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    result = await db.execute(
        select(Package).where(Package.client_id == client_id).order_by(Package.created_at)
    )
    return result.scalars().all()


@router.post("/client/{client_id}/packages", response_model=PackageOut, status_code=201)
async def create_package(
    client_id: str,
    request: PackageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    # CCA Step 1 membership gate (Batch 1C): the crop must be on the
    # company's conveyor belt before an expert can build a PoP for it.
    try:
        await assert_crop_on_belt(
            db, client_id=client_id, crop_cosh_id=request.crop_cosh_id,
        )
    except CropNotOnBeltError as e:
        raise HTTPException(
            status_code=422,
            detail={"code": e.code, "message": str(e)},
        )

    # CCA Step 2 / Batch 2A: range-check Annual duration (1-365);
    # Perennial is forced to 365 regardless of input. Pre-fix the live
    # route silently defaulted Annual to 180 when omitted and never
    # checked the upper bound — a CA could ship 9999-day timelines.
    try:
        duration = validate_package_duration_for_create(
            package_type=request.package_type.value,
            duration_days=request.duration_days,
        )
    except PackageValidationError as e:
        raise HTTPException(
            status_code=422,
            detail={"code": e.code, "message": e.message},
        )

    pkg = Package(
        client_id=client_id,
        crop_cosh_id=request.crop_cosh_id,
        name=request.name,
        package_type=request.package_type,
        duration_days=duration,
        start_date_label_cosh_id=request.start_date_label_cosh_id,
        description=request.description,
        created_by=current_user.id,
        status=PackageStatus.DRAFT,
    )
    db.add(pkg)
    await db.commit()
    await db.refresh(pkg)
    return pkg


@router.get("/client/{client_id}/packages/{package_id}", response_model=PackageOut)
async def get_package(
    client_id: str, package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    pkg = await _get_package(db, package_id, client_id)
    return pkg


@router.get("/client/{client_id}/packages/{package_id}/lineage")
async def get_package_lineage(
    client_id: str, package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All rows in the lineage of `package_id` — i.e. rows sharing
    (client_id, crop_cosh_id, name). Multi-row versioning (locked
    2026-05-11) keeps prior published versions as INACTIVE history
    rows; this endpoint feeds the version-history navigator on the
    CA-portal Local Package detail page.

    Ordered by version descending (newest first), with DRAFTs at
    the top regardless of their version field (DRAFTs default to
    version=1 and only get a lineage version on publish).

    Returns:
      [{id, status, version, published_at, created_at, created_via,
        source_version_id, is_current}, ...]
      where `is_current` flags the row the caller is viewing.
    """
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    await _assert_client_user_can_edit(db, current_user.id, client_id)
    pkg = await _get_package(db, package_id, client_id)

    rows = (await db.execute(
        select(Package).where(
            Package.client_id == client_id,
            Package.crop_cosh_id == pkg.crop_cosh_id,
            Package.name == pkg.name,
        ).order_by(Package.version.desc(), Package.created_at.desc())
    )).scalars().all()

    def sort_key(p: Package):
        # DRAFTs sit at the top (most recent edit cycle); within
        # PUBLISHED + INACTIVE, order by version desc then
        # created_at desc to keep the newest visible first.
        is_draft = 0 if p.status == PackageStatus.DRAFT else 1
        return (is_draft, -p.version, -p.created_at.timestamp())

    return [
        {
            "id": r.id,
            "status": r.status.value if hasattr(r.status, "value") else r.status,
            "version": r.version,
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "created_at": r.created_at.isoformat(),
            "created_via": (
                r.created_via.value if r.created_via and hasattr(r.created_via, "value")
                else r.created_via
            ),
            "source_version_id": r.source_version_id,
            "is_current": r.id == package_id,
        }
        for r in sorted(rows, key=sort_key)
    ]


@router.put("/client/{client_id}/packages/{package_id}", response_model=PackageOut)
async def update_package(
    client_id: str, package_id: str,
    request: PackageUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CCA Step 2 / Batch 2A: duration_days is range-checked on update
    and locked at 365 for Perennial packages. Pre-fix the route blindly
    setattr'd whatever was sent — a Perennial's duration could be
    flipped to 100 and break advisory alignment downstream."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    pkg = await _get_package(db, package_id, client_id)
    update_data = request.model_dump(exclude_unset=True)

    if "duration_days" in update_data:
        try:
            update_data["duration_days"] = validate_package_duration_for_update(
                package_type=pkg.package_type.value,
                current_duration=pkg.duration_days,
                new_duration=update_data["duration_days"],
            )
        except PackageValidationError as e:
            raise HTTPException(
                status_code=422,
                detail={"code": e.code, "message": e.message},
            )

    for field, value in update_data.items():
        setattr(pkg, field, value)
    await db.commit()
    await db.refresh(pkg)
    return pkg


@router.get("/client/{client_id}/packages/{package_id}/publish-readiness")
async def get_publish_readiness(
    client_id: str, package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read-only check of every gate `publish_package` runs before
    flipping a DRAFT to ACTIVE. Lets the CA portal render a live
    "what's missing" checklist on the package detail page so the SE
    sees the gap before clicking Publish, instead of bouncing off a
    422 each time.

    Returns `{ready: true, version, status}` when every gate is clear,
    or `{ready: false, status, missing: [{code, message, ...}], blocker_code}`
    when one fails. `blocker_code` is the same `code` `publish_package`
    would surface in its 422 — the missing list mirrors that 422's
    `missing` array shape so the frontend can render either response
    with the same component."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.services.crop_lifecycle import (
        CropNotOnBeltError, assert_crop_on_belt,
    )
    from app.services.pv_consistency import (
        PVConsistencyError, assert_pv_consistency_for_package,
    )
    from app.services.pv_uniqueness import (
        PVConflictError, assert_pv_unique_for_package,
    )

    pkg = await _get_package(db, package_id, client_id)

    # Subscription head-count for the publish confirmation context.
    # BL-13 versioning is *in-place* — there are no frozen older
    # version snapshots; every existing ACTIVE/WAITLISTED subscriber
    # on this package_id will see the new version's content the
    # moment it publishes. Surfacing the count here lets the CA
    # portal explain that truthfully.
    from app.modules.subscriptions.models import (
        Subscription, SubscriptionStatus,
    )
    sub_count_q = select(func.count()).select_from(Subscription).where(
        Subscription.package_id == pkg.id,
        Subscription.status.in_(
            (SubscriptionStatus.ACTIVE, SubscriptionStatus.WAITLISTED),
        ),
    )
    subscriber_count = (await db.execute(sub_count_q)).scalar() or 0

    base = {
        "version": pkg.version,
        "status": pkg.status.value,
        "published_at": pkg.published_at,
        "subscriber_count": subscriber_count,
    }

    try:
        await assert_crop_on_belt(
            db, client_id=client_id, crop_cosh_id=pkg.crop_cosh_id,
        )
    except CropNotOnBeltError as e:
        return {
            **base,
            "ready": False,
            "blocker_code": e.code,
            "missing": [{"code": e.code, "message": str(e)}],
        }

    try:
        await assert_package_publish_ready(db, package=pkg)
    except PublishBlockedError as e:
        return {
            **base,
            "ready": False,
            "blocker_code": e.code,
            "missing": [
                {"code": m.code, "message": m.message, **(m.extra or {})}
                for m in e.missing
            ],
        }

    try:
        await assert_pv_unique_for_package(db, package=pkg)
    except PVConflictError as e:
        return {
            **base,
            "ready": False,
            "blocker_code": e.code,
            "missing": [
                {"code": e.code, "message": str(e),
                 "conflicts": [c.__dict__ if hasattr(c, "__dict__") else c
                               for c in e.conflicts]},
            ],
        }

    try:
        await assert_pv_consistency_for_package(db, package=pkg)
    except PVConsistencyError as e:
        return {
            **base,
            "ready": False,
            "blocker_code": e.code,
            "missing": [
                {"code": e.code, "message": str(e),
                 "violations": [v.__dict__ if hasattr(v, "__dict__") else v
                                for v in e.violations]},
            ],
        }

    return {**base, "ready": True}


@router.post("/client/{client_id}/packages/{package_id}/publish", response_model=PackageOut)
async def publish_package(
    client_id: str, package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-13: Versioning lifecycle — publish creates new version, previous ACTIVE → INACTIVE.

    BL-13 audit (2026-05-06): version arithmetic moved to
    compute_publish_version. First publish (published_at IS NULL)
    lands at v=1; subsequent publishes increment from current.
    Pre-fix the unconditional `version + 1` produced v=2 on first
    publish for a default-version-1 row.
    """
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    pkg = await _get_package(db, package_id, client_id)

    # CCA Step 1 membership gate (Batch 1C): publish requires the
    # crop to be currently on the conveyor belt. Cascade-inactivated
    # PoPs (CA soft-removed the crop) are auto-revived to ACTIVE on
    # re-add, so this guard is the only path that prevents a publish
    # of a draft whose crop has since been removed.
    try:
        await assert_crop_on_belt(
            db, client_id=client_id, crop_cosh_id=pkg.crop_cosh_id,
        )
    except CropNotOnBeltError as e:
        raise HTTPException(
            status_code=422,
            detail={"code": e.code, "message": str(e)},
        )

    # CCA Step 2 / Batch 2C: complete-checklist publish-readiness
    # gate. Surfaces every missing mandatory field + the §4.2
    # second-PoP rule as a single consolidated 422 response so the
    # CA portal can render a checklist instead of forcing the
    # expert through fix-one-at-a-time roundtrips. Runs BEFORE the
    # 2D/2E defensive checks because a missing-fields failure is
    # the more fundamental issue — fix it first, then re-publish.
    try:
        await assert_package_publish_ready(db, package=pkg)
    except PublishBlockedError as e:
        _raise_publish_blocked(e)

    # CCA Step 2 / Batch 2D: defensive uniqueness check at publish.
    # The save-time guards on set_package_variables / locations
    # should have caught any conflict already, but if a sibling was
    # edited concurrently, or rows were inserted via SQL outside the
    # API, last-line block here.
    try:
        await assert_pv_unique_for_package(db, package=pkg)
    except PVConflictError as e:
        _raise_pv_conflict(e)
    try:
        await assert_pv_consistency_for_package(db, package=pkg)
    except PVConsistencyError as e:
        _raise_pv_consistency(e)

    current_status = pkg.status.value if hasattr(pkg.status, "value") else str(pkg.status)
    res = validate_publish_transition(current_status)
    if not res.allowed:
        _raise_publish_transition(res)

    # Inactivate current ACTIVE version for same crop in same client
    # AND migrate any subscriptions to the new ACTIVE row. Multi-row
    # versioning (locked 2026-05-11): farmers stay on the live
    # PUBLISHED row automatically, no opt-in. Snapshots on the
    # demoted row become orphaned but harmless; first /today view
    # after migration takes fresh snapshots on the new row's
    # timeline ids.
    from app.modules.subscriptions.models import Subscription

    existing_active = (await db.execute(
        select(Package).where(
            Package.client_id == client_id,
            Package.crop_cosh_id == pkg.crop_cosh_id,
            Package.status == PackageStatus.ACTIVE,
            Package.id != package_id,
        )
    )).scalars().all()
    for active in existing_active:
        active.status = PackageStatus.INACTIVE
        await db.execute(
            update(Subscription)
            .where(Subscription.package_id == active.id)
            .values(package_id=package_id)
        )

    # Multi-row versioning: version monotonically increases across
    # the lineage (rows sharing client + crop + name), not just on
    # this row. A clone-to-draft + publish of v_n+1 must show
    # max(lineage) + 1, even though the DRAFT row itself starts at
    # version=1. The legacy compute_publish_version path only saw
    # `pkg.version` and missed history rows.
    pkg.version = await _next_lineage_version(
        db, client_id=client_id, crop_cosh_id=pkg.crop_cosh_id, name=pkg.name,
    )
    pkg.status = PackageStatus.ACTIVE
    pkg.published_at = datetime.now(timezone.utc)
    pkg.published_by = current_user.id
    await db.commit()
    await db.refresh(pkg)
    return pkg


@router.get("/client/{client_id}/packages/{package_id}/locations")
async def list_package_locations(
    client_id: str, package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the state/district pairs this Package is configured to
    serve. Read-only; mutations go through PUT below."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    await _get_package(db, package_id, client_id)
    rows = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == package_id)
        .order_by(PackageLocation.state_cosh_id, PackageLocation.district_cosh_id)
    )).scalars().all()
    return [
        {
            "id": r.id,
            "state_cosh_id": r.state_cosh_id,
            "district_cosh_id": r.district_cosh_id,
        }
        for r in rows
    ]


@router.put("/client/{client_id}/packages/{package_id}/locations")
async def set_package_locations(
    client_id: str, package_id: str,
    locations: list[PackageLocationIn],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CCA Step 2 / Batch 2D: changing locations can newly create a
    shared district with a sibling that has the same P/V fingerprint.
    After the new location set is in place, run the uniqueness check
    against DRAFT/ACTIVE siblings and refuse the save if any conflict
    surfaces. Spec §4.2."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    pkg = await _get_package(db, package_id, client_id)
    existing = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == package_id)
    )).scalars().all()
    for loc in existing:
        await db.delete(loc)
    for loc in locations:
        db.add(PackageLocation(package_id=package_id, **loc.model_dump()))
    await db.flush()

    try:
        await assert_pv_unique_for_package(db, package=pkg)
    except PVConflictError as e:
        _raise_pv_conflict(e)
    try:
        await assert_pv_consistency_for_package(db, package=pkg)
    except PVConsistencyError as e:
        _raise_pv_consistency(e)

    await db.commit()
    return {"detail": f"{len(locations)} locations saved"}


# ── Package Authors (CCA Step 2 / Batch 2B) ──────────────────────────────────

@router.get(
    "/client/{client_id}/packages/{package_id}/authors",
    response_model=list[PackageAuthorOut],
)
async def list_package_authors(
    client_id: str, package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the Subject Experts credited as authors on this Package,
    ordered by `display_order`. Each row carries the User's name
    joined in for portal rendering convenience."""
    await _get_package(db, package_id, client_id)
    rows = (await db.execute(
        select(PackageAuthor, User)
        .join(User, User.id == PackageAuthor.user_id)
        .where(PackageAuthor.package_id == package_id)
        .order_by(PackageAuthor.display_order, PackageAuthor.id)
    )).all()
    return [
        PackageAuthorOut(
            id=pa.id, user_id=pa.user_id, user_name=u.name,
            # Batch D+E (2026-05-18): designation + professional_profile
            # are now per-User (set in User Management). Joined in at
            # read time so the farmer PWA gets one consistent bio per
            # author regardless of which package surfaced them.
            designation=u.designation,
            professional_profile=u.professional_profile,
            display_order=pa.display_order,
        )
        for pa, u in rows
    ]


@router.put("/client/{client_id}/packages/{package_id}/authors")
async def set_package_authors(
    client_id: str, package_id: str,
    authors: list[PackageAuthorIn],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Replace-all save of the Package's authors. Spec §4.1: each
    entry must be an ACTIVE ClientUser of this client with role
    SUBJECT_EXPERT. Empty list is allowed at save time (CA may be
    mid-edit); publish-time non-empty enforcement is Batch 2C.

    Stable error codes:
    - duplicate_author — same user_id appears twice in the request.
    - invalid_author — at least one user_id is not an ACTIVE SE
      of this client. Detail includes `invalid_user_ids` so the
      portal can highlight precisely which rows to fix.
    """
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _get_package(db, package_id, client_id)

    user_ids = [a.user_id for a in authors]
    if len(set(user_ids)) != len(user_ids):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "duplicate_author",
                "message": "An expert cannot be listed twice as an author of the same Package.",
            },
        )

    if user_ids:
        valid_se_ids = set((await db.execute(
            select(ClientUser.user_id).where(
                ClientUser.client_id == client_id,
                ClientUser.user_id.in_(user_ids),
                ClientUser.role == ClientUserRole.SUBJECT_EXPERT,
                ClientUser.status == StatusEnum.ACTIVE,
            )
        )).scalars().all())
        invalid = sorted(set(user_ids) - valid_se_ids)
        if invalid:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_author",
                    "message": (
                        "The following user_id(s) are not ACTIVE Subject "
                        "Experts of this company and cannot be assigned "
                        "as Package authors."
                    ),
                    "invalid_user_ids": invalid,
                },
            )

    existing = (await db.execute(
        select(PackageAuthor).where(PackageAuthor.package_id == package_id)
    )).scalars().all()
    for pa in existing:
        await db.delete(pa)
    for a in authors:
        db.add(PackageAuthor(
            package_id=package_id,
            user_id=a.user_id,
            display_order=a.display_order,
        ))
    await db.commit()
    return {"detail": f"{len(authors)} authors saved"}


# ── Parameters and Variables ───────────────────────────────────────────────────

@router.get("/client/{client_id}/parameters")
async def list_parameters(
    client_id: str, crop_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns the parameters visible to this client for the crop:
      (a) Cosh-mirrored Globals (source=COSH, client_id IS NULL) —
          read-only catalogue, available to every client.
      (b) This client's own Custom parameters (client_id = this).

    Deliberately EXCLUDES Global-Custom (client_id IS NULL AND
    source = CUSTOM) — those are an SA-Portal-only authoring artefact
    that should never bleed into CA. Also excludes other clients'
    Customs — per-client isolation."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from sqlalchemy import and_, or_
    from app.modules.advisory.models import ParameterSource
    result = await db.execute(
        select(Parameter).where(
            Parameter.crop_cosh_id == crop_cosh_id,
            or_(
                Parameter.client_id == client_id,
                and_(
                    Parameter.client_id == None,  # noqa: E711
                    Parameter.source == ParameterSource.COSH,
                ),
            ),
        ).order_by(Parameter.display_order)
    )
    return result.scalars().all()


@router.post("/client/{client_id}/parameters", status_code=201)
async def create_parameter(
    client_id: str,
    request: ParameterCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Atomic create — parameter + ≥ 2 variables in one transaction.
    Schema's min_length=2 on variables returns 422 if violated.
    Matches the SA portal contract from Batch 28."""
    from app.modules.advisory.models import ParameterSource
    param = Parameter(
        crop_cosh_id=request.crop_cosh_id,
        client_id=client_id,
        name=request.name,
        source=ParameterSource.CUSTOM,
        display_order=request.display_order,
    )
    db.add(param)
    await db.flush()
    for v in request.variables:
        db.add(Variable(parameter_id=param.id, name=v.name))
    await db.commit()
    await db.refresh(param)
    return param


async def _assert_ca_param_writable(
    db: AsyncSession, client_id: str, parameter_id: str,
) -> Parameter:
    """Gate for any CA write against a Parameter or its Variables.
    Refuses Cosh-mirrored parents (read-only catalogue) and
    cross-client parents (per-client isolation). Returns the
    parameter when the write is allowed."""
    from app.modules.advisory.models import ParameterSource
    param = (await db.execute(
        select(Parameter).where(Parameter.id == parameter_id)
    )).scalar_one_or_none()
    if param is None:
        raise HTTPException(status_code=404, detail="Parameter not found")
    if param.source == ParameterSource.COSH or param.client_id is None:
        raise HTTPException(status_code=422, detail={
            "code": "cosh_parameter_readonly",
            "message": "Cosh parameters are read-only. Create your own Custom parameter to extend.",
        })
    if param.client_id != client_id:
        raise HTTPException(status_code=403, detail={
            "code": "parameter_not_owned",
            "message": "This parameter belongs to a different client.",
        })
    return param


@router.get("/client/{client_id}/parameters/{parameter_id}/variables")
async def list_variables(
    client_id: str, parameter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns variables visible to this client for the parameter:
      - Cosh-mirrored parent: only Cosh-sourced variables
        (cosh_id IS NOT NULL). Any SA-added siblings stay hidden.
      - This client's own Custom parent: all variables.
      - Other clients' Custom parents: empty (defensive — shouldn't
        be reachable via list_parameters' filter)."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.modules.advisory.models import ParameterSource
    param = (await db.execute(
        select(Parameter).where(Parameter.id == parameter_id)
    )).scalar_one_or_none()
    if param is None:
        return []
    stmt = select(Variable).where(Variable.parameter_id == parameter_id)
    if param.source == ParameterSource.COSH or param.client_id is None:
        stmt = stmt.where(Variable.cosh_id.isnot(None))
    elif param.client_id != client_id:
        return []
    result = await db.execute(stmt.order_by(Variable.created_at))
    return result.scalars().all()


@router.post("/client/{client_id}/parameters/{parameter_id}/variables", status_code=201)
async def create_variable(
    client_id: str, parameter_id: str,
    request: VariableCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a variable under one of this client's Custom parameters.
    Refuses Cosh parents (read-only) and cross-client parents."""
    await _assert_ca_param_writable(db, client_id, parameter_id)
    var = Variable(parameter_id=parameter_id, name=request.name)
    db.add(var)
    await db.commit()
    await db.refresh(var)
    return var


# ── Custom Parameters: extended CRUD (status, edit, translation) ─────────────

@router.put("/client/{client_id}/parameters/{parameter_id}")
async def update_client_parameter(
    client_id: str, parameter_id: str,
    request: ParameterUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Rename a CUSTOM client parameter. Mirror of
    update_global_parameter (SA, line ~3614). Refuses on Cosh-mirrored
    parameters — those track upstream Cosh translations and shouldn't
    be edited locally."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    param = (await db.execute(
        select(Parameter).where(
            Parameter.id == parameter_id, Parameter.client_id == client_id,
        )
    )).scalar_one_or_none()
    if param is None:
        raise HTTPException(status_code=404, detail="Parameter not found")
    if param.source != ParameterSource.CUSTOM:
        raise HTTPException(status_code=422, detail={
            "code": "cosh_mirrored_parameter_readonly",
            "message": "Cosh-mirrored parameters can't be edited; Cosh is the source of truth.",
        })
    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(param, field, value)
    await db.commit()
    await db.refresh(param)
    return param


@router.delete("/client/{client_id}/parameters/{parameter_id}", status_code=204)
async def delete_client_parameter(
    client_id: str, parameter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hard-delete a CUSTOM client parameter and its variables. Mirror
    of delete_global_parameter (SA, line ~3645). Refuses if any
    PackageVariable still references it — SE must clear the
    Package signatures first. Refuses on Cosh-mirrored parameters."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    param = (await db.execute(
        select(Parameter).where(
            Parameter.id == parameter_id, Parameter.client_id == client_id,
        )
    )).scalar_one_or_none()
    if param is None:
        raise HTTPException(status_code=404, detail="Parameter not found")
    if param.source != ParameterSource.CUSTOM:
        raise HTTPException(status_code=422, detail={
            "code": "cosh_mirrored_parameter_readonly",
            "message": "Cosh-mirrored parameters can't be deleted; Cosh is the source of truth.",
        })
    in_use = (await db.execute(
        select(PackageVariable).where(
            PackageVariable.parameter_id == parameter_id,
        ).limit(1)
    )).scalar_one_or_none()
    if in_use is not None:
        raise HTTPException(status_code=422, detail={
            "code": "parameter_in_use",
            "message": "Parameter is in use by one or more Package signatures. Clear those signatures first.",
        })
    await db.execute(
        Variable.__table__.delete().where(Variable.parameter_id == parameter_id)
    )
    await db.delete(param)
    await db.commit()


@router.delete(
    "/client/{client_id}/parameters/{parameter_id}/variables/{variable_id}",
    status_code=204,
)
async def delete_client_variable(
    client_id: str, parameter_id: str, variable_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hard-delete a Variable from a client parameter. Mirror of
    delete_global_variable (SA, line ~3728). Gates on
    `variable.cosh_id` (SE-added variables on Cosh-mirrored parents
    are deletable; Cosh-mirrored variables themselves are not).
    A CUSTOM parameter must keep ≥ 2 variables (Batch 28 invariant).
    Refuses if the variable is in use by any PackageVariable."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    param = (await db.execute(
        select(Parameter).where(
            Parameter.id == parameter_id, Parameter.client_id == client_id,
        )
    )).scalar_one_or_none()
    if param is None:
        raise HTTPException(status_code=404, detail="Parameter not found")
    var = (await db.execute(
        select(Variable).where(
            Variable.id == variable_id,
            Variable.parameter_id == parameter_id,
        )
    )).scalar_one_or_none()
    if var is None:
        raise HTTPException(status_code=404, detail="Variable not found")
    if var.cosh_id is not None:
        raise HTTPException(status_code=422, detail={
            "code": "cosh_mirrored_variable_readonly",
            "message": "Cosh-mirrored variables can't be deleted; Cosh is the source of truth.",
        })
    if param.source == ParameterSource.CUSTOM:
        sibling_count = (await db.execute(
            select(func.count()).select_from(Variable).where(
                Variable.parameter_id == parameter_id,
            )
        )).scalar()
        if sibling_count <= 2:
            raise HTTPException(status_code=422, detail={
                "code": "min_two_variables",
                "message": "A CUSTOM Parameter must have at least 2 variables.",
            })
    in_use = (await db.execute(
        select(PackageVariable).where(
            PackageVariable.variable_id == variable_id,
        ).limit(1)
    )).scalar_one_or_none()
    if in_use is not None:
        raise HTTPException(status_code=422, detail={
            "code": "variable_in_use",
            "message": "Variable is in use by one or more Package signatures. Clear those signatures first.",
        })
    await db.delete(var)
    await db.commit()


@router.put("/client/{client_id}/parameters/{parameter_id}/status")
async def toggle_parameter_status(
    client_id: str, parameter_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Activate or deactivate a custom parameter. Block delete — only inactivate."""
    param = (await db.execute(
        select(Parameter).where(Parameter.id == parameter_id, Parameter.client_id == client_id)
    )).scalar_one_or_none()
    if not param:
        raise HTTPException(status_code=404, detail="Parameter not found")
    param.status = data.get("status", "INACTIVE")
    await db.commit()
    return {"id": parameter_id, "status": param.status}


@router.put("/client/{client_id}/parameters/{parameter_id}/variables/{variable_id}")
async def update_variable(
    client_id: str, parameter_id: str, variable_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit variable text. Refuses Cosh parents (read-only) and
    cross-client parents. Resets all its translations to
    PENDING_REVIEW per spec A1.4."""
    await _assert_ca_param_writable(db, client_id, parameter_id)
    var = (await db.execute(
        select(Variable).where(Variable.id == variable_id, Variable.parameter_id == parameter_id)
    )).scalar_one_or_none()
    if not var:
        raise HTTPException(status_code=404, detail="Variable not found")
    if "name" in data and data["name"] != var.name:
        var.name = data["name"]
        # Reset all translations to PENDING_REVIEW
        translations = (await db.execute(
            select(VariableTranslation).where(VariableTranslation.variable_id == variable_id)
        )).scalars().all()
        for t in translations:
            t.translation_status = TranslationStatus.PENDING
    await db.commit()
    return {"id": variable_id, "name": var.name}


@router.put("/client/{client_id}/parameters/{parameter_id}/variables/{variable_id}/status")
async def toggle_variable_status(
    client_id: str, parameter_id: str, variable_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Activate or deactivate a variable. Cannot delete once used in a published PoP."""
    var = (await db.execute(
        select(Variable).where(Variable.id == variable_id, Variable.parameter_id == parameter_id)
    )).scalar_one_or_none()
    if not var:
        raise HTTPException(status_code=404, detail="Variable not found")
    var.status = data.get("status", "INACTIVE")
    await db.commit()
    return {"id": variable_id, "status": var.status}


@router.get("/client/{client_id}/parameters/{parameter_id}/translations")
async def list_parameter_translations(
    client_id: str, parameter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all language translations for a parameter."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    translations = (await db.execute(
        select(ParameterTranslation).where(ParameterTranslation.parameter_id == parameter_id)
    )).scalars().all()
    return [{"language_code": t.language_code, "name": t.name,
             "status": t.translation_status.value} for t in translations]


@router.put("/client/{client_id}/parameters/{parameter_id}/translations/{lang_code}")
async def approve_parameter_translation(
    client_id: str, parameter_id: str, lang_code: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Approve or edit a parameter translation."""
    existing = (await db.execute(
        select(ParameterTranslation).where(
            ParameterTranslation.parameter_id == parameter_id,
            ParameterTranslation.language_code == lang_code,
        )
    )).scalar_one_or_none()
    if existing:
        if "name" in data:
            existing.name = data["name"]
        existing.translation_status = TranslationStatus.EXPERT_VALIDATED
        existing.approved_by = current_user.id
        existing.approved_at = datetime.now(timezone.utc)
    else:
        existing = ParameterTranslation(
            parameter_id=parameter_id,
            language_code=lang_code,
            name=data.get("name", ""),
            translation_status=TranslationStatus.EXPERT_VALIDATED,
            approved_by=current_user.id,
            approved_at=datetime.now(timezone.utc),
        )
        db.add(existing)
    await db.commit()
    return {"language_code": lang_code, "status": "EXPERT_VALIDATED"}


@router.get("/client/{client_id}/parameters/{parameter_id}/variables/{variable_id}/translations")
async def list_variable_translations(
    client_id: str, parameter_id: str, variable_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    translations = (await db.execute(
        select(VariableTranslation).where(VariableTranslation.variable_id == variable_id)
    )).scalars().all()
    return [{"language_code": t.language_code, "name": t.name,
             "status": t.translation_status.value} for t in translations]


@router.put("/client/{client_id}/parameters/{parameter_id}/variables/{variable_id}/translations/{lang_code}")
async def approve_variable_translation(
    client_id: str, parameter_id: str, variable_id: str, lang_code: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing = (await db.execute(
        select(VariableTranslation).where(
            VariableTranslation.variable_id == variable_id,
            VariableTranslation.language_code == lang_code,
        )
    )).scalar_one_or_none()
    if existing:
        if "name" in data:
            existing.name = data["name"]
        existing.translation_status = TranslationStatus.EXPERT_VALIDATED
    else:
        existing = VariableTranslation(
            variable_id=variable_id,
            language_code=lang_code,
            name=data.get("name", ""),
            translation_status=TranslationStatus.EXPERT_VALIDATED,
        )
        db.add(existing)
    await db.commit()
    return {"language_code": lang_code, "status": "EXPERT_VALIDATED"}


@router.get("/client/{client_id}/packages/{package_id}/variables")
async def list_package_variables(
    client_id: str, package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the parameter→variable fingerprint set on a client Package.
    Mirror of list_global_package_variables (SA, line ~3843).
    Defaults to empty when none assigned yet."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    await _get_package(db, package_id, client_id)
    rows = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id == package_id)
    )).scalars().all()
    return [
        {
            "parameter_id": pv.parameter_id,
            "variable_id": pv.variable_id,
        }
        for pv in rows
    ]


@router.put("/client/{client_id}/packages/{package_id}/variables")
async def set_package_variables(
    client_id: str, package_id: str,
    request: PackageVariableSet,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set the parameter→variable fingerprint for a Package.

    CCA Step 2 / Batch 2D (spec §4.2): after the new fingerprint is
    in place, refuse the save if any DRAFT/ACTIVE sibling under the
    same `(client, crop)` shares at least one district AND has an
    identical fingerprint. Guided elimination is non-deterministic
    otherwise — the farmer answers all the questions and ends up
    with two PoPs the system can't distinguish.
    """
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    pkg = await _get_package(db, package_id, client_id)
    # Visibility check — every assignment must reference a parameter
    # that's visible to this client (Cosh-global OR own Custom) and a
    # variable that's visible under it (Cosh-sourced on a Cosh parent,
    # or any sibling on an own Custom parent). Prevents the SE from
    # binding their package to an SA-only Global-Custom or another
    # client's Custom by guessing IDs.
    from app.modules.advisory.models import ParameterSource
    for a in request.assignments:
        param = (await db.execute(
            select(Parameter).where(Parameter.id == a["parameter_id"])
        )).scalar_one_or_none()
        if param is None:
            raise HTTPException(status_code=404, detail={
                "code": "parameter_not_found",
                "message": f"Parameter {a['parameter_id']} not found.",
            })
        param_visible = (
            param.client_id == client_id
            or (param.client_id is None and param.source == ParameterSource.COSH)
        )
        if not param_visible:
            raise HTTPException(status_code=403, detail={
                "code": "parameter_not_visible",
                "message": "Parameter not visible to this client.",
            })
        var = (await db.execute(
            select(Variable).where(
                Variable.id == a["variable_id"],
                Variable.parameter_id == a["parameter_id"],
            )
        )).scalar_one_or_none()
        if var is None:
            raise HTTPException(status_code=404, detail={
                "code": "variable_not_found",
                "message": f"Variable {a['variable_id']} not found under parameter.",
            })
        if param.client_id is None and var.cosh_id is None:
            # SA-added variable on a Cosh parent — invisible to CA.
            raise HTTPException(status_code=403, detail={
                "code": "variable_not_visible",
                "message": "Variable not visible to this client.",
            })
    existing = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id == package_id)
    )).scalars().all()
    for pv in existing:
        await db.delete(pv)
    for assignment in request.assignments:
        db.add(PackageVariable(
            package_id=package_id,
            parameter_id=assignment["parameter_id"],
            variable_id=assignment["variable_id"],
        ))
    await db.flush()

    try:
        await assert_pv_unique_for_package(db, package=pkg)
    except PVConflictError as e:
        _raise_pv_conflict(e)
    try:
        await assert_pv_consistency_for_package(db, package=pkg)
    except PVConsistencyError as e:
        _raise_pv_consistency(e)

    await db.commit()
    return {"detail": f"{len(request.assignments)} parameter-variable assignments saved"}


# ── Timelines ──────────────────────────────────────────────────────────────────

@router.get("/client/{client_id}/packages/{package_id}/timelines", response_model=list[TimelineOut])
async def list_timelines(
    client_id: str, package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    await _get_package(db, package_id, client_id)
    result = await db.execute(
        select(Timeline).where(Timeline.package_id == package_id).order_by(Timeline.display_order, Timeline.from_value)
    )
    return result.scalars().all()


@router.get("/client/{client_id}/packages/{package_id}/timelines/conflicts")
async def list_timeline_conflicts(
    client_id: str, package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-17 audit (2026-05-06): soft-warning surface for the CA
    portal. Spec says consecutive timelines must have no gaps and
    no overlaps, validated at save but not hard-blocked. Pre-audit
    the live router didn't validate this at all — a Package could
    ship with silent coverage gaps or duplicated coverage.

    The CA portal calls this endpoint after a timeline save to
    surface warnings (or after loading the package detail page).
    Returns an empty `conflicts` list when the package's timelines
    are clean. CALENDAR-typed timelines are skipped — they have no
    day-offset anchor relative to crop_start, so they can't gap or
    overlap with DAS/DBS timelines on the same number line.
    """
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    await _get_package(db, package_id, client_id)
    rows = (await db.execute(
        select(Timeline).where(Timeline.package_id == package_id)
    )).scalars().all()
    specs = [
        TimelineSpec(
            timeline_id=row.id,
            from_type=row.from_type.value if hasattr(row.from_type, "value") else str(row.from_type),
            from_value=int(row.from_value),
            to_value=int(row.to_value),
        )
        for row in rows
    ]
    conflicts = find_timeline_conflicts(specs)
    return {
        "package_id": package_id,
        "conflict_count": len(conflicts),
        "conflicts": [
            {
                "timeline_a_id": c.timeline_a_id,
                "timeline_b_id": c.timeline_b_id,
                "kind": c.kind,
                "detail": c.detail,
            }
            for c in conflicts
        ],
    }


@router.post("/client/{client_id}/packages/{package_id}/timelines", response_model=TimelineOut, status_code=201)
async def create_timeline(
    client_id: str, package_id: str,
    request: TimelineCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CCA Step 3 / Batch 3-Hardening: validates type/direction/sign
    against the parent Package's type, plus pre-checks name
    uniqueness within the Package so duplicate names surface as a
    friendly 422 instead of a 500 from the DB unique constraint."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    pkg = await _get_package(db, package_id, client_id)

    try:
        validate_timeline(
            package_type=pkg.package_type.value,
            from_type=request.from_type.value,
            from_value=request.from_value,
            to_value=request.to_value,
        )
    except TimelineValidationError as e:
        _raise_timeline_validation(e)

    await _assert_timeline_name_unique(
        db, package_id=package_id, name=request.name,
    )

    tl = Timeline(package_id=package_id, **request.model_dump())
    db.add(tl)
    await db.commit()
    await db.refresh(tl)
    return tl


@router.put("/client/{client_id}/packages/{package_id}/timelines/{timeline_id}", response_model=TimelineOut)
async def update_timeline(
    client_id: str, package_id: str, timeline_id: str,
    request: TimelineUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CCA Step 3 / Batch 3-Hardening: name-uniqueness pre-check on
    rename + direction + sign re-validation post-update.

    `from_type` is intentionally not exposed in `TimelineUpdate`, so
    type ↔ package consistency is fixed at create time and the
    update path doesn't need to re-check it.
    """
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    pkg = await _get_package(db, package_id, client_id)
    tl = await _get_timeline(db, timeline_id, package_id)

    update_data = request.model_dump(exclude_unset=True)
    if "name" in update_data and update_data["name"] != tl.name:
        await _assert_timeline_name_unique(
            db, package_id=package_id, name=update_data["name"],
            exclude_timeline_id=timeline_id,
        )

    new_from = update_data.get("from_value", tl.from_value)
    new_to = update_data.get("to_value", tl.to_value)
    try:
        validate_timeline(
            package_type=pkg.package_type.value,
            from_type=tl.from_type.value,
            from_value=new_from, to_value=new_to,
        )
    except TimelineValidationError as e:
        _raise_timeline_validation(e)

    # Batch 28: validate status if provided. ACTIVE / INACTIVE only.
    if "status" in update_data:
        if update_data["status"] not in ("ACTIVE", "INACTIVE"):
            raise HTTPException(status_code=422, detail={
                "code": "invalid_status",
                "message": "Timeline status must be ACTIVE or INACTIVE.",
            })

    for field, value in update_data.items():
        setattr(tl, field, value)
    await db.commit()
    await db.refresh(tl)
    return tl


@router.delete("/client/{client_id}/packages/{package_id}/timelines/{timeline_id}", status_code=204)
async def delete_timeline(
    client_id: str, package_id: str, timeline_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    tl = await _get_timeline(db, timeline_id, package_id)
    await db.delete(tl)
    await db.commit()


@router.post("/client/{client_id}/packages/{package_id}/timelines/import", response_model=TimelineOut, status_code=201)
async def import_timeline(
    client_id: str, package_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Copy a timeline (with all practices and elements) from any package into this one.
    The copy is completely independent after save — changes to either do not affect the other.
    """
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    source_id = data.get("source_timeline_id")
    new_name = (data.get("new_name") or "").strip()
    if not source_id:
        raise HTTPException(status_code=422, detail="source_timeline_id required")
    if not new_name:
        raise HTTPException(status_code=422, detail="new_name required — imported timelines must be renamed")

    # Load source timeline
    src_tl = (await db.execute(select(Timeline).where(Timeline.id == source_id))).scalar_one_or_none()
    if not src_tl:
        raise HTTPException(status_code=404, detail="Source timeline not found")

    # CCA Step 3 / Batch 3-Hardening: validate the imported timeline
    # against the TARGET package's type — source might be Annual+DAS
    # and target might be Perennial; that combo is a type mismatch.
    # Plus the standard direction + sign checks. Plus name uniqueness
    # in the target package (the unique constraint would otherwise
    # surface as a 500).
    target_pkg = await _get_package(db, package_id, client_id)
    try:
        validate_timeline(
            package_type=target_pkg.package_type.value,
            from_type=src_tl.from_type.value,
            from_value=src_tl.from_value,
            to_value=src_tl.to_value,
        )
    except TimelineValidationError as e:
        _raise_timeline_validation(e)

    await _assert_timeline_name_unique(
        db, package_id=package_id, name=new_name,
    )

    # Create new timeline in target package
    new_tl = Timeline(
        package_id=package_id,
        name=new_name,
        from_type=src_tl.from_type,
        from_value=src_tl.from_value,
        to_value=src_tl.to_value,
        display_order=data.get("display_order", 0),
    )
    db.add(new_tl)
    await db.flush()

    # Copy practices
    src_practices = (await db.execute(
        select(Practice).where(Practice.timeline_id == src_tl.id).order_by(Practice.display_order)
    )).scalars().all()

    for src_p in src_practices:
        new_p = Practice(
            timeline_id=new_tl.id,
            l0_type=src_p.l0_type,
            l1_type=src_p.l1_type,
            l2_type=src_p.l2_type,
            display_order=src_p.display_order,
            is_special_input=src_p.is_special_input,
            common_name_cosh_id=src_p.common_name_cosh_id,
            frequency_days=src_p.frequency_days,
        )
        db.add(new_p)
        await db.flush()

        # Copy elements
        src_elements = (await db.execute(
            select(Element).where(Element.practice_id == src_p.id).order_by(Element.display_order)
        )).scalars().all()
        for src_el in src_elements:
            db.add(Element(
                practice_id=new_p.id,
                element_type=src_el.element_type,
                cosh_ref=src_el.cosh_ref,
                value=src_el.value,
                unit_cosh_id=src_el.unit_cosh_id,
                display_order=src_el.display_order,
            ))

    await db.commit()
    await db.refresh(new_tl)
    return new_tl


# ── Practices ──────────────────────────────────────────────────────────────────

@router.get("/client/{client_id}/timelines/{timeline_id}/practices", response_model=list[PracticeWithElementsOut])
async def list_practices(
    client_id: str, timeline_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Same shape as the global list (Batch 32) — practices bundled
    with their elements + server-resolved English labels."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    practices = (await db.execute(
        select(Practice).where(Practice.timeline_id == timeline_id).order_by(Practice.display_order)
    )).scalars().all()
    return await _attach_elements_with_labels(db, practices)


@router.post("/client/{client_id}/timelines/{timeline_id}/practices", response_model=PracticeOut, status_code=201)
async def create_practice(
    client_id: str, timeline_id: str,
    request: PracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CCA Step 4 / Batch 4C-i.D: validates the proposed element list
    against the L2 rule book before persisting. Returns 422 with the
    full error list on rule violations (mandatory fields, cascade
    integrity, special-input / frequency-based / plant-wise invariants).

    Batch 30: l2_type is now required (no more "No sub-type" shells)."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    if not request.l2_type:
        raise HTTPException(status_code=422, detail={
            "code": "l2_type_required",
            "message": "L2 type is required when creating a Practice.",
        })
    try:
        await assert_l2_elements_valid(
            db,
            l2_type=request.l2_type,
            elements=request.elements,
            is_special_input=request.is_special_input,
            frequency_days=request.frequency_days,
        )
    except L2ElementValidationError as e:
        _raise_l2_element_validation(e)
    # Batch 34: frequency_too_long gate.
    await _assert_interval_fits_timeline(
        db, l2_type=request.l2_type,
        elements=request.elements, timeline_id=timeline_id,
    )
    _validate_brand_lock(request)

    practice = Practice(
        timeline_id=timeline_id,
        l0_type=request.l0_type,
        l1_type=request.l1_type,
        l2_type=request.l2_type,
        display_order=request.display_order,
        is_special_input=request.is_special_input,
        is_brand_locked=request.is_brand_locked,
        frequency_days=request.frequency_days,
    )
    db.add(practice)
    await db.flush()

    # Batch 39C-bugfix2 (2026-05-15): stamp display_order from the
    # request position so the read-only practice card mirrors the
    # rule-book / modal order. The client sends elements in
    # rule-book order; the schema default of 0 was making every
    # row tie and the read sort fell back to insertion order.
    for idx, elem in enumerate(request.elements):
        data = elem.model_dump()
        data["display_order"] = idx
        db.add(Element(practice_id=practice.id, **data))

    await db.commit()
    await db.refresh(practice)
    return practice


@router.put("/client/{client_id}/timelines/{timeline_id}/practices/{practice_id}", response_model=PracticeOut)
async def update_practice(
    client_id: str, timeline_id: str, practice_id: str,
    request: PracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Atomic Practice replace — same shape as the global PUT
    (Batch 33). Runs the L2 validator before swapping elements."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    if not request.l2_type:
        raise HTTPException(status_code=422, detail={
            "code": "l2_type_required",
            "message": "L2 type is required when updating a Practice.",
        })
    practice = (await db.execute(
        select(Practice).where(
            Practice.id == practice_id, Practice.timeline_id == timeline_id,
        )
    )).scalar_one_or_none()
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")

    try:
        await assert_l2_elements_valid(
            db,
            l2_type=request.l2_type,
            elements=request.elements,
            is_special_input=request.is_special_input,
            frequency_days=request.frequency_days,
        )
    except L2ElementValidationError as e:
        _raise_l2_element_validation(e)
    # Batch 34: frequency_too_long gate.
    await _assert_interval_fits_timeline(
        db, l2_type=request.l2_type,
        elements=request.elements, timeline_id=timeline_id,
    )
    _validate_brand_lock(request)

    practice.l0_type = request.l0_type
    practice.l1_type = request.l1_type
    practice.l2_type = request.l2_type
    practice.display_order = request.display_order
    practice.is_special_input = request.is_special_input
    practice.is_brand_locked = request.is_brand_locked
    practice.frequency_days = request.frequency_days

    await db.execute(
        Element.__table__.delete().where(Element.practice_id == practice_id)
    )
    await db.flush()
    # Batch 39C-bugfix2: stamp display_order from the request position.
    for idx, elem in enumerate(request.elements):
        data = elem.model_dump()
        data["display_order"] = idx
        db.add(Element(practice_id=practice_id, **data))
    await db.commit()
    await db.refresh(practice)
    return practice


@router.delete("/client/{client_id}/timelines/{timeline_id}/practices/{practice_id}", status_code=204)
async def delete_practice(
    client_id: str, timeline_id: str, practice_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    result = await db.execute(select(Practice).where(Practice.id == practice_id, Practice.timeline_id == timeline_id))
    practice = result.scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")
    await db.delete(practice)
    await db.commit()


# ── CCA per-element CRUD (Round 2) ─────────────────────────────────────────

async def _load_cca_practice(db, *, timeline_id: str, practice_id: str):
    practice = (await db.execute(
        select(Practice).where(
            Practice.id == practice_id,
            Practice.timeline_id == timeline_id,
        )
    )).scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")
    return practice


@router.post(
    "/client/{client_id}/timelines/{timeline_id}/practices/{practice_id}/elements",
    status_code=201,
)
async def add_cca_element(
    client_id: str, timeline_id: str, practice_id: str,
    body: ElementIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    practice = await _load_cca_practice(db, timeline_id=timeline_id, practice_id=practice_id)
    new = await _add_practice_element(
        db, practice=practice, element_model=Element, body=body,
    )
    return _element_row_to_out(new)


@router.put(
    "/client/{client_id}/timelines/{timeline_id}/practices/{practice_id}/elements/{element_id}",
)
async def update_cca_element(
    client_id: str, timeline_id: str, practice_id: str, element_id: str,
    body: ElementIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    practice = await _load_cca_practice(db, timeline_id=timeline_id, practice_id=practice_id)
    updated = await _update_practice_element(
        db, practice=practice, element_model=Element,
        element_id=element_id, body=body,
    )
    return _element_row_to_out(updated)


@router.delete(
    "/client/{client_id}/timelines/{timeline_id}/practices/{practice_id}/elements/{element_id}",
    status_code=204,
)
async def delete_cca_element(
    client_id: str, timeline_id: str, practice_id: str, element_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    practice = await _load_cca_practice(db, timeline_id=timeline_id, practice_id=practice_id)
    await _delete_practice_element(
        db, practice=practice, element_model=Element, element_id=element_id,
    )


# ── Relations ──────────────────────────────────────────────────────────────────

@router.post("/client/{client_id}/timelines/{timeline_id}/relations", status_code=201)
async def create_relation(
    client_id: str, timeline_id: str,
    request: RelationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CCA Step 4 / Batch 4A: validates AND/OR structure against
    spec §6.4 + §10.2 + user clarification 2026-05-07. Builds the
    Part/Option/Position structure, runs every save-time rule, and
    persists Practice.relation_id + Practice.relation_role on success.

    Returns 422 with `code = relation_validation_failed` and
    `errors: [...]` containing the complete list of rule violations
    so the CA portal can render a single checklist.

    `request.parts` is a 3-D list (parts × options × positions) of
    practice_ids — see RelationCreate docstring for the shape.
    """
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    # Flatten + dedupe practice_ids while preserving the structure.
    distinct_practice_ids: set[str] = set()
    for opts in request.parts:
        for positions in opts:
            for pid in positions:
                distinct_practice_ids.add(pid)

    if not distinct_practice_ids:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "relation_empty",
                "message": "A Relation must reference at least one Practice.",
            },
        )

    practices = (await db.execute(
        select(Practice).where(Practice.id.in_(distinct_practice_ids))
    )).scalars().all()
    by_id = {p.id: p for p in practices}

    missing = sorted(distinct_practice_ids - set(by_id.keys()))
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "relation_practice_not_found",
                "message": f"Practice(s) not found: {missing}.",
                "missing_practice_ids": missing,
            },
        )

    # Load the COMMON_NAME element value per practice so the
    # combinatorial-duplicate checks have a stable input identity.
    common_name_rows = (await db.execute(
        select(Element.practice_id, Element.cosh_ref).where(
            Element.practice_id.in_(distinct_practice_ids),
            Element.element_type == "COMMON_NAME",
        )
    )).all()
    cn_by_practice = {row[0]: row[1] for row in common_name_rows}

    practice_refs_by_id = {
        pid: PracticeRef(
            practice_id=pid,
            common_name_cosh_id=cn_by_practice.get(pid) or p.common_name_cosh_id,
            is_special_input=p.is_special_input,
            role="",  # to be filled by build_structure_from_parts
            l2_type=p.l2_type,
        )
        for pid, p in by_id.items()
    }

    practice_meta = {
        pid: {
            "l0_type": p.l0_type.value if p.l0_type else None,
            "l1_type": p.l1_type,
            "timeline_id": p.timeline_id,
            "relation_id": p.relation_id,
        }
        for pid, p in by_id.items()
    }

    # CCA Step 4 / Batch 4B cross-check: refuse if any incoming
    # practice has an existing PracticeConditional. The link is
    # bound to the practice as an independent unit; once it joins
    # a Relation, the conditional must move (or be cleared) before
    # the relation save proceeds.
    existing_pcs = (await db.execute(
        select(PracticeConditional).where(
            PracticeConditional.practice_id.in_(distinct_practice_ids)
        )
    )).scalars().all()
    if existing_pcs:
        try:
            assert_practices_have_no_independent_conditional(
                practices_with_conditional=[
                    {"practice_id": pc.practice_id, "question_id": pc.question_id}
                    for pc in existing_pcs
                ],
            )
        except ConditionalValidationError as e:
            _raise_conditional_validation(e)

    try:
        structure = validate_relation_save(
            relation_type=request.relation_type.value,
            target_timeline_id=timeline_id,
            parts=request.parts,
            practice_refs_by_id=practice_refs_by_id,
            practice_meta=practice_meta,
        )
    except RelationValidationFailed as e:
        _raise_relation_validation(e)

    relation = Relation(
        timeline_id=timeline_id,
        relation_type=request.relation_type,
        expression=request.expression,
    )
    db.add(relation)
    await db.flush()

    # Persist relation_id + relation_role on each PracticeRef in the
    # structure. The role is the encoded PART/OPT/POS string.
    for part in structure.parts:
        for opt in part.options:
            for pref in opt.practices:
                pkg_practice = by_id[pref.practice_id]
                pkg_practice.relation_id = relation.id
                pkg_practice.relation_role = pref.role

    await db.commit()
    await db.refresh(relation)
    return {
        "id": relation.id,
        "relation_type": relation.relation_type.value,
        "expression": relation.expression,
    }


# ── Conditional Questions ──────────────────────────────────────────────────────

@router.post("/client/{client_id}/timelines/{timeline_id}/conditional-questions", status_code=201)
async def create_conditional_question(
    client_id: str, timeline_id: str,
    request: ConditionalQuestionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    q = ConditionalQuestion(timeline_id=timeline_id, **request.model_dump())
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return q


@router.post("/client/{client_id}/practices/{practice_id}/conditionals", status_code=201)
async def link_practice_conditional(
    client_id: str, practice_id: str,
    request: PracticeConditionalCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CCA Step 4 / Batch 4B: validates that
    1) the Practice exists,
    2) the Practice is NOT in a saved Relation (refuse — use the
       link_relation_conditional endpoint instead),
    3) the Practice is not already linked to a different
       Conditional Question.
    Same `(practice_id, question_id)` repeats are idempotent — the
    existing row is returned without modification.
    """
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    practice = (await db.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one_or_none()
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")

    existing_pc = (await db.execute(
        select(PracticeConditional).where(
            PracticeConditional.practice_id == practice_id
        )
    )).scalar_one_or_none()
    existing_q_id = existing_pc.question_id if existing_pc else None

    try:
        assert_practice_can_be_linked_to_conditional(
            practice_id=practice_id,
            practice_relation_id=practice.relation_id,
            target_question_id=request.question_id,
            existing_question_id_for_practice=existing_q_id,
        )
    except ConditionalValidationError as e:
        _raise_conditional_validation(e)

    if existing_pc is not None and existing_pc.question_id == request.question_id:
        # Idempotent: same link already exists. Update answer if different.
        existing_pc.answer = request.answer
        await db.commit()
        await db.refresh(existing_pc)
        return existing_pc

    pc = PracticeConditional(
        practice_id=practice_id,
        question_id=request.question_id,
        answer=request.answer,
    )
    db.add(pc)
    await db.commit()
    await db.refresh(pc)
    return pc


@router.post("/client/{client_id}/relations/{relation_id}/conditionals", status_code=201)
async def link_relation_conditional(
    client_id: str, relation_id: str,
    request: PracticeConditionalCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CCA Step 4 / Batch 4B Path A: when the practices in a Relation
    need to be conditionally applied, the link binds to the Relation
    rather than each individual Practice (spec §6.4 + user
    clarification 2026-05-07). The PracticeConditional table is
    reserved for INDEPENDENT practices.

    Refuses if:
    - The Relation doesn't exist (404).
    - The Relation is already linked to a different Conditional
      Question (422 `relation_already_in_conditional`).
    Same `(relation_id, question_id)` repeats are idempotent.

    Note: `request.practice_id` (inherited from PracticeConditionalCreate)
    is ignored on this endpoint — the resource is the Relation
    identified in the URL.
    """
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    relation = (await db.execute(
        select(Relation).where(Relation.id == relation_id)
    )).scalar_one_or_none()
    if relation is None:
        raise HTTPException(status_code=404, detail="Relation not found")

    existing_rc = (await db.execute(
        select(RelationConditional).where(
            RelationConditional.relation_id == relation_id
        )
    )).scalar_one_or_none()
    existing_q_id = existing_rc.question_id if existing_rc else None

    try:
        assert_relation_can_be_linked_to_conditional(
            relation_id=relation_id,
            target_question_id=request.question_id,
            existing_question_id_for_relation=existing_q_id,
        )
    except ConditionalValidationError as e:
        _raise_conditional_validation(e)

    if existing_rc is not None and existing_rc.question_id == request.question_id:
        existing_rc.answer = request.answer
        await db.commit()
        await db.refresh(existing_rc)
        return existing_rc

    rc = RelationConditional(
        relation_id=relation_id,
        question_id=request.question_id,
        answer=request.answer,
    )
    db.add(rc)
    await db.commit()
    await db.refresh(rc)
    return rc


# ── CA-side GET / PUT / DELETE for Relations + CQs (Batch N1, 2026-05-18) ────
#
# Until this batch, CA-side only had the four POST endpoints above.
# The SA-frontend's RelationsSection.tsx and CQsSection.tsx (Batches
# 39P-b / 39P-c) shipped a full GET / PUT / DELETE surface against
# `/advisory/global/...` URLs. Adding the CA mirrors here lets the
# same shared components mount on CA-CCA (and later CA-PG / CA-SP /
# CA-QA in batch N2) by passing a different endpoints object.
#
# Implementation: the SA-side pipe-agnostic helpers
# (`_list_relations_for_global_timeline`, `_list_cqs_for_global_timeline`)
# don't actually scope by Global — the calling endpoint does. We
# reuse them here directly after verifying the timeline belongs to
# the path's client.


async def _assert_timeline_belongs_to_client(
    db: AsyncSession, timeline_id: str, client_id: str,
) -> Timeline:
    """Resolve a Timeline and confirm one of its parent FKs
    (package_id / pg_recommendation_id / sp_recommendation_id /
    standard_response_id) points to a parent owned by `client_id`.
    Refuses 404 otherwise — same response shape whether the timeline
    is unknown or cross-tenant (no info leak)."""
    from app.modules.farmpundit.models import StandardResponse

    tl = (await db.execute(
        select(Timeline).where(Timeline.id == timeline_id)
    )).scalar_one_or_none()
    if tl is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    if tl.package_id is not None:
        pkg = (await db.execute(
            select(Package).where(Package.id == tl.package_id)
        )).scalar_one_or_none()
        if pkg and pkg.client_id == client_id:
            return tl
    if tl.pg_recommendation_id is not None:
        pg = (await db.execute(
            select(PGRecommendation).where(PGRecommendation.id == tl.pg_recommendation_id)
        )).scalar_one_or_none()
        if pg and pg.client_id == client_id:
            return tl
    if tl.sp_recommendation_id is not None:
        sp = (await db.execute(
            select(SPRecommendation).where(SPRecommendation.id == tl.sp_recommendation_id)
        )).scalar_one_or_none()
        if sp and sp.client_id == client_id:
            return tl
    if tl.standard_response_id is not None:
        sr = (await db.execute(
            select(StandardResponse).where(StandardResponse.id == tl.standard_response_id)
        )).scalar_one_or_none()
        if sr and sr.client_id == client_id:
            return tl
    raise HTTPException(status_code=404, detail="Timeline not found")


async def _get_relation_for_client(
    db: AsyncSession, relation_id: str, client_id: str,
) -> Relation:
    """Resolve a Relation and confirm its Timeline belongs to
    `client_id`. 404 on any mismatch."""
    rel = (await db.execute(
        select(Relation).where(Relation.id == relation_id)
    )).scalar_one_or_none()
    if rel is None:
        raise HTTPException(status_code=404, detail="Relation not found")
    await _assert_timeline_belongs_to_client(db, rel.timeline_id, client_id)
    return rel


async def _get_cq_for_client(
    db: AsyncSession, question_id: str, client_id: str,
) -> ConditionalQuestion:
    """Resolve a ConditionalQuestion and confirm its Timeline belongs
    to `client_id`. 404 on any mismatch."""
    cq = (await db.execute(
        select(ConditionalQuestion).where(ConditionalQuestion.id == question_id)
    )).scalar_one_or_none()
    if cq is None:
        raise HTTPException(status_code=404, detail="Conditional Question not found")
    await _assert_timeline_belongs_to_client(db, cq.timeline_id, client_id)
    return cq


@router.get("/client/{client_id}/timelines/{timeline_id}/relations")
async def list_client_relations(
    client_id: str, timeline_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List Relations on a client-side Timeline. Pipe-agnostic
    (works for CCA / PG / SP / QA timelines). View-guard applies
    so non-SE / non-CA roles get a 403."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    await _assert_timeline_belongs_to_client(db, timeline_id, client_id)
    return await _list_relations_for_global_timeline(
        db, timeline_id=timeline_id,
    )


@router.delete("/client/{client_id}/relations/{relation_id}", status_code=204)
async def delete_client_relation(
    client_id: str, relation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Drop a Relation on a client-side Timeline. Clears
    relation_id / relation_role on every Practice in this Relation
    and removes any RelationConditional binding. Mirror of
    delete_global_relation."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _get_relation_for_client(db, relation_id, client_id)

    practices = (await db.execute(
        select(Practice).where(Practice.relation_id == relation_id)
    )).scalars().all()
    for p in practices:
        p.relation_id = None
        p.relation_role = None

    rcs = (await db.execute(
        select(RelationConditional).where(
            RelationConditional.relation_id == relation_id,
        )
    )).scalars().all()
    for rc in rcs:
        await db.delete(rc)

    rel = (await db.execute(
        select(Relation).where(Relation.id == relation_id)
    )).scalar_one()
    await db.delete(rel)
    await db.commit()


@router.get("/client/{client_id}/timelines/{timeline_id}/conditional-questions")
async def list_client_conditional_questions(
    client_id: str, timeline_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List CQs on a client-side Timeline with YES/NO attachments
    bundled. Pipe-agnostic. View-guard applies."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    await _assert_timeline_belongs_to_client(db, timeline_id, client_id)
    return await _list_cqs_for_global_timeline(
        db, timeline_id=timeline_id,
    )


@router.put("/client/{client_id}/conditional-questions/{question_id}")
async def update_client_conditional_question(
    client_id: str, question_id: str,
    request: CQReplace,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Atomic replace: update the CQ text + rebind YES / NO sides
    in one transaction. Mirror of update_global_conditional_question.
    Reuses the same assert_*_can_be_linked_to_conditional checks."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    cq = await _get_cq_for_client(db, question_id, client_id)

    if not request.question_text.strip():
        raise HTTPException(status_code=422, detail={
            "code": "cq_question_text_required",
            "message": "Question text is required.",
        })

    # Clear existing attachments for THIS CQ first.
    await db.execute(
        PracticeConditional.__table__.delete().where(
            PracticeConditional.question_id == question_id
        )
    )
    await db.execute(
        RelationConditional.__table__.delete().where(
            RelationConditional.question_id == question_id
        )
    )
    await db.flush()

    async def _bind(att, answer_value: str):
        if att is None:
            return
        ans = ConditionalAnswer(answer_value)
        if att.kind == "practice":
            practice = (await db.execute(
                select(Practice).where(Practice.id == att.id)
            )).scalar_one_or_none()
            if practice is None:
                raise HTTPException(status_code=404, detail=f"Practice {att.id} not found")
            existing_pc = (await db.execute(
                select(PracticeConditional).where(
                    PracticeConditional.practice_id == att.id,
                )
            )).scalar_one_or_none()
            existing_q_id = existing_pc.question_id if existing_pc else None
            try:
                assert_practice_can_be_linked_to_conditional(
                    practice_id=att.id,
                    practice_relation_id=practice.relation_id,
                    target_question_id=question_id,
                    existing_question_id_for_practice=existing_q_id,
                )
            except ConditionalValidationError as e:
                _raise_conditional_validation(e)
            db.add(PracticeConditional(
                practice_id=att.id, question_id=question_id, answer=ans,
            ))
        elif att.kind == "relation":
            relation = (await db.execute(
                select(Relation).where(Relation.id == att.id)
            )).scalar_one_or_none()
            if relation is None:
                raise HTTPException(status_code=404, detail=f"Relation {att.id} not found")
            existing_rc = (await db.execute(
                select(RelationConditional).where(
                    RelationConditional.relation_id == att.id,
                )
            )).scalar_one_or_none()
            existing_q_id = existing_rc.question_id if existing_rc else None
            try:
                assert_relation_can_be_linked_to_conditional(
                    relation_id=att.id,
                    target_question_id=question_id,
                    existing_question_id_for_relation=existing_q_id,
                )
            except ConditionalValidationError as e:
                _raise_conditional_validation(e)
            db.add(RelationConditional(
                relation_id=att.id, question_id=question_id, answer=ans,
            ))
        else:
            raise HTTPException(status_code=422, detail={
                "code": "cq_attachment_unknown_kind",
                "message": f"Unknown attachment kind: {att.kind!r}",
            })

    await _bind(request.yes, "YES")
    await _bind(request.no, "NO")

    cq.question_text = request.question_text.strip()
    await db.commit()
    await db.refresh(cq)
    return {
        "id": cq.id,
        "timeline_id": cq.timeline_id,
        "question_text": cq.question_text,
        "display_order": cq.display_order,
    }


@router.delete("/client/{client_id}/conditional-questions/{question_id}", status_code=204)
async def delete_client_conditional_question(
    client_id: str, question_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Drop a CQ on a client-side Timeline. Cascades through any
    PracticeConditional / RelationConditional rows that bound to
    this CQ. Mirror of delete_global_conditional_question."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    cq = await _get_cq_for_client(db, question_id, client_id)
    await db.execute(
        PracticeConditional.__table__.delete().where(
            PracticeConditional.question_id == question_id
        )
    )
    await db.execute(
        RelationConditional.__table__.delete().where(
            RelationConditional.question_id == question_id
        )
    )
    await db.delete(cq)
    await db.commit()


# ── Global Relations (Batch 39A, 2026-05-15) ──────────────────────────────────
#
# SA-portal authoring of Practice Relations on Global Packages. Mirrors the
# four client-scoped endpoints above but binds to a Global Package (client_id
# IS NULL) and verifies every URL segment composes the way the SE expects.
# Reuses validate_relation_save / assert_*_can_be_linked_to_conditional from
# the same services the client-side flow uses — single source of truth.


async def _get_global_timeline(
    db: AsyncSession, pkg_id: str, timeline_id: str,
) -> tuple[Package, Timeline]:
    """Resolve (Package, Timeline) for a Global authoring URL. 404 if either
    is missing, mismatched, or the Package is client-scoped."""
    pkg = (await db.execute(
        select(Package).where(
            Package.id == pkg_id, Package.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if pkg is None:
        raise HTTPException(status_code=404, detail="Global package not found")
    tl = (await db.execute(
        select(Timeline).where(
            Timeline.id == timeline_id, Timeline.package_id == pkg_id,
        )
    )).scalar_one_or_none()
    if tl is None:
        raise HTTPException(status_code=404, detail="Timeline not found on this Global package")
    return pkg, tl


async def _get_global_practice(db: AsyncSession, practice_id: str) -> Practice:
    """Resolve a Practice and confirm its Timeline is rooted on a Global
    parent (Package / PG, both with `client_id IS NULL`). Extended in
    Batch 39P-c (2026-05-16) from package-only to UCAT-wide so the
    practice→CQ binding endpoint works for any pipe."""
    practice = (await db.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one_or_none()
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")
    tl = (await db.execute(
        select(Timeline).where(Timeline.id == practice.timeline_id)
    )).scalar_one_or_none()
    if tl is None:
        raise HTTPException(status_code=404, detail="Practice has no parent timeline")
    if tl.package_id is not None:
        pkg = (await db.execute(
            select(Package).where(Package.id == tl.package_id)
        )).scalar_one_or_none()
        if pkg and pkg.client_id is None:
            return practice
    if tl.pg_recommendation_id is not None:
        pg = (await db.execute(
            select(PGRecommendation).where(PGRecommendation.id == tl.pg_recommendation_id)
        )).scalar_one_or_none()
        if pg and pg.client_id is None:
            return practice
    raise HTTPException(
        status_code=404, detail="Practice is not on a Global parent",
    )


async def _get_global_relation(db: AsyncSession, relation_id: str) -> Relation:
    """Resolve a Relation and confirm its Timeline is rooted on a Global
    parent (Package / PG / SP / QA, all with `client_id IS NULL`).

    Batch 39P-b (2026-05-16) — extended from package-only to UCAT-wide
    after Batch 39O merged the four pipes into a single `timelines`
    table. The Relation table itself was always pipe-agnostic (FK to
    `timelines.id` only); the Global-scoping check now walks via
    Timeline → whichever parent FK is set."""
    relation = (await db.execute(
        select(Relation).where(Relation.id == relation_id)
    )).scalar_one_or_none()
    if relation is None:
        raise HTTPException(status_code=404, detail="Relation not found")
    tl = (await db.execute(
        select(Timeline).where(Timeline.id == relation.timeline_id)
    )).scalar_one_or_none()
    if tl is None:
        raise HTTPException(status_code=404, detail="Relation has no parent timeline")
    # CCA Global Package.
    if tl.package_id is not None:
        pkg = (await db.execute(
            select(Package).where(Package.id == tl.package_id)
        )).scalar_one_or_none()
        if pkg and pkg.client_id is None:
            return relation
    # CHA Global PG.
    if tl.pg_recommendation_id is not None:
        pg = (await db.execute(
            select(PGRecommendation).where(PGRecommendation.id == tl.pg_recommendation_id)
        )).scalar_one_or_none()
        if pg and pg.client_id is None:
            return relation
    raise HTTPException(
        status_code=404, detail="Relation is not on a Global parent",
    )


async def _create_relation_for_global_timeline(
    db: AsyncSession, *, timeline_id: str, request: "RelationCreate",
) -> dict:
    """Pipe-agnostic Relation create. Caller is responsible for the
    Global-scope auth check before calling. Body shared between
    `create_global_relation` (CCA Package) and
    `create_global_pg_relation` (CHA-PG) — extracted in Batch 39P-b
    so both pipes go through the same validator + persistence path
    after UCAT unification (Batch 39O)."""
    distinct_practice_ids: set[str] = set()
    for opts in request.parts:
        for positions in opts:
            for pid in positions:
                distinct_practice_ids.add(pid)
    if not distinct_practice_ids:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "relation_empty",
                "message": "A Relation must reference at least one Practice.",
            },
        )

    practices = (await db.execute(
        select(Practice).where(Practice.id.in_(distinct_practice_ids))
    )).scalars().all()
    by_id = {p.id: p for p in practices}
    missing = sorted(distinct_practice_ids - set(by_id.keys()))
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "relation_practice_not_found",
                "message": f"Practice(s) not found: {missing}.",
                "missing_practice_ids": missing,
            },
        )

    common_name_rows = (await db.execute(
        select(Element.practice_id, Element.cosh_ref).where(
            Element.practice_id.in_(distinct_practice_ids),
            Element.element_type == "COMMON_NAME",
        )
    )).all()
    cn_by_practice = {row[0]: row[1] for row in common_name_rows}

    practice_refs_by_id = {
        pid: PracticeRef(
            practice_id=pid,
            common_name_cosh_id=cn_by_practice.get(pid) or p.common_name_cosh_id,
            is_special_input=p.is_special_input,
            role="",
            l2_type=p.l2_type,
        )
        for pid, p in by_id.items()
    }
    practice_meta = {
        pid: {
            "l0_type": p.l0_type.value if p.l0_type else None,
            "l1_type": p.l1_type,
            "timeline_id": p.timeline_id,
            "relation_id": p.relation_id,
        }
        for pid, p in by_id.items()
    }

    # If any incoming practice carries an independent PracticeConditional,
    # refuse — the conditional moves to the Relation (Path A) once the
    # practice joins one. Same rule as the client-scoped endpoint.
    existing_pcs = (await db.execute(
        select(PracticeConditional).where(
            PracticeConditional.practice_id.in_(distinct_practice_ids),
        )
    )).scalars().all()
    if existing_pcs:
        try:
            assert_practices_have_no_independent_conditional(
                practices_with_conditional=[
                    {"practice_id": pc.practice_id, "question_id": pc.question_id}
                    for pc in existing_pcs
                ],
            )
        except ConditionalValidationError as e:
            _raise_conditional_validation(e)

    try:
        structure = validate_relation_save(
            relation_type=request.relation_type.value,
            target_timeline_id=timeline_id,
            parts=request.parts,
            practice_refs_by_id=practice_refs_by_id,
            practice_meta=practice_meta,
        )
    except RelationValidationFailed as e:
        _raise_relation_validation(e)

    relation = Relation(
        timeline_id=timeline_id,
        relation_type=request.relation_type,
        expression=request.expression,
    )
    db.add(relation)
    await db.flush()

    for part in structure.parts:
        for opt in part.options:
            for pref in opt.practices:
                pkg_practice = by_id[pref.practice_id]
                pkg_practice.relation_id = relation.id
                pkg_practice.relation_role = pref.role

    await db.commit()
    await db.refresh(relation)
    return {
        "id": relation.id,
        "relation_type": relation.relation_type.value,
        "expression": relation.expression,
    }


@router.post(
    "/advisory/global/packages/{pkg_id}/timelines/{timeline_id}/relations",
    status_code=201,
)
async def create_global_relation(
    pkg_id: str, timeline_id: str,
    request: RelationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Authoring-time save of a Practice Relation on a Global CCA
    Timeline. Body shape, validation, and persistence shared with the
    CHA-PG sibling via `_create_relation_for_global_timeline`."""
    await _assert_sa_or_cm(db, current_user)
    await _get_global_timeline(db, pkg_id, timeline_id)
    return await _create_relation_for_global_timeline(
        db, timeline_id=timeline_id, request=request,
    )


async def _list_relations_for_global_timeline(
    db: AsyncSession, *, timeline_id: str,
) -> list[dict]:
    """Pipe-agnostic Relation list. Caller pre-validates Global
    scope. Returns each Relation with its 3-D `parts` shape
    reconstructed from per-Practice `PART_n__OPT_m__POS_p` roles +
    any bound RelationConditional folded in under `conditional`."""
    relations = (await db.execute(
        select(Relation).where(Relation.timeline_id == timeline_id)
        .order_by(Relation.created_at)
    )).scalars().all()
    if not relations:
        return []

    relation_ids = [r.id for r in relations]
    practices = (await db.execute(
        select(Practice).where(Practice.relation_id.in_(relation_ids))
    )).scalars().all()
    practices_by_rel: dict[str, list[Practice]] = {}
    for p in practices:
        practices_by_rel.setdefault(p.relation_id, []).append(p)

    rcs = (await db.execute(
        select(RelationConditional).where(
            RelationConditional.relation_id.in_(relation_ids),
        )
    )).scalars().all()
    rc_by_rel = {rc.relation_id: rc for rc in rcs}

    q_ids = {rc.question_id for rc in rcs}
    questions_by_id: dict[str, ConditionalQuestion] = {}
    if q_ids:
        qs = (await db.execute(
            select(ConditionalQuestion).where(ConditionalQuestion.id.in_(q_ids))
        )).scalars().all()
        questions_by_id = {q.id: q for q in qs}

    out = []
    for rel in relations:
        # Reconstruct parts[part_idx][option_idx] = [practice_id, ...]
        by_part: dict[int, dict[int, list[tuple[int, str]]]] = {}
        for p in practices_by_rel.get(rel.id, []):
            if not p.relation_role:
                continue
            coords = decode_role(p.relation_role)
            by_part.setdefault(coords.part, {}).setdefault(coords.option, []).append(
                (coords.position, p.id)
            )
        parts: list[list[list[str]]] = []
        for part_idx in sorted(by_part.keys()):
            options: list[list[str]] = []
            for opt_idx in sorted(by_part[part_idx].keys()):
                pos_list = sorted(by_part[part_idx][opt_idx], key=lambda x: x[0])
                options.append([pid for (_, pid) in pos_list])
            parts.append(options)

        conditional = None
        rc = rc_by_rel.get(rel.id)
        if rc is not None:
            q = questions_by_id.get(rc.question_id)
            conditional = {
                "question_id": rc.question_id,
                "question_text": q.question_text if q else None,
                "answer": rc.answer.value,
            }

        out.append({
            "id": rel.id,
            "relation_type": rel.relation_type.value,
            "expression": rel.expression,
            "parts": parts,
            "conditional": conditional,
        })
    return out


@router.get(
    "/advisory/global/packages/{pkg_id}/timelines/{timeline_id}/relations",
)
async def list_global_relations_for_timeline(
    pkg_id: str, timeline_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List Relations on a Global CCA Timeline. Body shared with the
    CHA-PG sibling via `_list_relations_for_global_timeline`."""
    await _get_global_timeline(db, pkg_id, timeline_id)
    return await _list_relations_for_global_timeline(
        db, timeline_id=timeline_id,
    )


# ── PG-side Relation endpoints (Batch 39P-b, 2026-05-16) ───────────────
# Same shape as CCA above — the create/list bodies are extracted into
# pipe-agnostic helpers (`_create_relation_for_global_timeline`,
# `_list_relations_for_global_timeline`) so adding more pipes is just
# the auth-prefix wrapper.

async def _get_global_pg_timeline(
    db: AsyncSession, pg_id: str, timeline_id: str,
) -> tuple["PGRecommendation", Timeline]:
    """Resolve (PGRecommendation, Timeline) for a Global PG authoring
    URL. 404 on any mismatch or if the PG is client-scoped."""
    pg = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.id == pg_id,
            PGRecommendation.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if pg is None:
        raise HTTPException(status_code=404, detail="Global PG not found")
    tl = (await db.execute(
        select(Timeline).where(
            Timeline.id == timeline_id,
            Timeline.pg_recommendation_id == pg_id,
        )
    )).scalar_one_or_none()
    if tl is None:
        raise HTTPException(
            status_code=404,
            detail="Timeline not found on this Global PG",
        )
    return pg, tl


@router.post(
    "/advisory/global/pg-recommendations/{pg_id}/timelines/{timeline_id}/relations",
    status_code=201,
)
async def create_global_pg_relation(
    pg_id: str, timeline_id: str,
    request: RelationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Authoring-time save of a Practice Relation on a Global CHA-PG
    Timeline. Shares the body validator + persister with the CCA
    sibling — see `_create_relation_for_global_timeline`."""
    await _assert_sa_or_cm(db, current_user)
    await _get_global_pg_timeline(db, pg_id, timeline_id)
    return await _create_relation_for_global_timeline(
        db, timeline_id=timeline_id, request=request,
    )


@router.get(
    "/advisory/global/pg-recommendations/{pg_id}/timelines/{timeline_id}/relations",
)
async def list_global_pg_relations(
    pg_id: str, timeline_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List Relations on a Global CHA-PG Timeline."""
    await _get_global_pg_timeline(db, pg_id, timeline_id)
    return await _list_relations_for_global_timeline(
        db, timeline_id=timeline_id,
    )


@router.delete(
    "/advisory/global/relations/{relation_id}",
    status_code=204,
)
async def delete_global_relation(
    relation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove a Relation. Clears `relation_id` and `relation_role` on every
    Practice that was part of it (the practices stay in place — only the
    Relation grouping is dropped) and deletes any RelationConditional
    binding. Edit-saved-Relation is deferred to V1.1; the SE deletes and
    rebuilds instead."""
    await _assert_sa_or_cm(db, current_user)
    await _get_global_relation(db, relation_id)

    # Clear relation_id/role on practices in this relation.
    practices = (await db.execute(
        select(Practice).where(Practice.relation_id == relation_id)
    )).scalars().all()
    for p in practices:
        p.relation_id = None
        p.relation_role = None

    # Drop any RelationConditional binding(s) for this relation.
    rcs = (await db.execute(
        select(RelationConditional).where(
            RelationConditional.relation_id == relation_id,
        )
    )).scalars().all()
    for rc in rcs:
        await db.delete(rc)

    # Finally, drop the Relation itself.
    relation = (await db.execute(
        select(Relation).where(Relation.id == relation_id)
    )).scalar_one()
    await db.delete(relation)
    await db.commit()


async def _create_cq_for_global_timeline(
    db: AsyncSession, *,
    timeline_id: str, request: "ConditionalQuestionCreate",
) -> ConditionalQuestion:
    """Pipe-agnostic CQ create. Caller pre-validates Global scope.
    Extracted Batch 39P-c (2026-05-16); body shared between CCA
    Package + CHA-PG endpoints."""
    q = ConditionalQuestion(timeline_id=timeline_id, **request.model_dump())
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return q


async def _list_cqs_for_global_timeline(
    db: AsyncSession, *, timeline_id: str,
) -> list[dict]:
    """Pipe-agnostic CQ list. Returns each CQ with its YES- and NO-side
    attachments bundled (Path A relation or Path B practice). Caller
    pre-validates Global scope."""
    qs = (await db.execute(
        select(ConditionalQuestion).where(
            ConditionalQuestion.timeline_id == timeline_id,
        ).order_by(ConditionalQuestion.display_order, ConditionalQuestion.created_at)
    )).scalars().all()
    if not qs:
        return []
    q_ids = [q.id for q in qs]
    pcs = (await db.execute(
        select(PracticeConditional).where(PracticeConditional.question_id.in_(q_ids))
    )).scalars().all()
    rcs = (await db.execute(
        select(RelationConditional).where(RelationConditional.question_id.in_(q_ids))
    )).scalars().all()
    # Bucket attachments by (question_id, answer). User's model
    # (2026-05-15) says at most one entity per (CQ, answer). The
    # backend doesn't enforce that, so if multiple rows somehow exist
    # we take the first deterministically and the UI surfaces only
    # one — pre-existing data corruption would be visible elsewhere.
    pc_by_key: dict[tuple[str, str], PracticeConditional] = {}
    for pc in pcs:
        key = (pc.question_id, pc.answer.value)
        pc_by_key.setdefault(key, pc)
    rc_by_key: dict[tuple[str, str], RelationConditional] = {}
    for rc in rcs:
        key = (rc.question_id, rc.answer.value)
        rc_by_key.setdefault(key, rc)
    out: list[dict] = []
    for q in qs:
        def side(answer: str) -> dict:
            rc = rc_by_key.get((q.id, answer))
            if rc is not None:
                return {"kind": "relation", "id": rc.relation_id}
            pc = pc_by_key.get((q.id, answer))
            if pc is not None:
                return {"kind": "practice", "id": pc.practice_id}
            return None  # type: ignore
        out.append({
            "id": q.id,
            "timeline_id": q.timeline_id,
            "question_text": q.question_text,
            "display_order": q.display_order,
            "yes": side("YES"),
            "no":  side("NO"),
        })
    return out


@router.post(
    "/advisory/global/packages/{pkg_id}/timelines/{timeline_id}/conditional-questions",
    status_code=201,
)
async def create_global_conditional_question(
    pkg_id: str, timeline_id: str,
    request: ConditionalQuestionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a CQ on a Global CCA Timeline. Body shared with the
    CHA-PG sibling via `_create_cq_for_global_timeline`."""
    await _assert_sa_or_cm(db, current_user)
    await _get_global_timeline(db, pkg_id, timeline_id)
    return await _create_cq_for_global_timeline(
        db, timeline_id=timeline_id, request=request,
    )


@router.get(
    "/advisory/global/packages/{pkg_id}/timelines/{timeline_id}/conditional-questions",
)
async def list_global_conditional_questions(
    pkg_id: str, timeline_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List CQs on a Global CCA Timeline with YES/NO attachments
    bundled. Shared with CHA-PG via `_list_cqs_for_global_timeline`."""
    await _get_global_timeline(db, pkg_id, timeline_id)
    return await _list_cqs_for_global_timeline(
        db, timeline_id=timeline_id,
    )


# ── PG-side CQ endpoints (Batch 39P-c, 2026-05-16) ─────────────────────

@router.post(
    "/advisory/global/pg-recommendations/{pg_id}/timelines/{timeline_id}/conditional-questions",
    status_code=201,
)
async def create_global_pg_conditional_question(
    pg_id: str, timeline_id: str,
    request: ConditionalQuestionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a CQ on a Global CHA-PG Timeline. Same body validator +
    persister as CCA, plumbed via the shared helper."""
    await _assert_sa_or_cm(db, current_user)
    await _get_global_pg_timeline(db, pg_id, timeline_id)
    return await _create_cq_for_global_timeline(
        db, timeline_id=timeline_id, request=request,
    )


@router.get(
    "/advisory/global/pg-recommendations/{pg_id}/timelines/{timeline_id}/conditional-questions",
)
async def list_global_pg_conditional_questions(
    pg_id: str, timeline_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List CQs on a Global CHA-PG Timeline. PUT + DELETE on these CQs
    use the existing pipe-agnostic `/advisory/global/conditional-
    questions/{id}` URLs."""
    await _get_global_pg_timeline(db, pg_id, timeline_id)
    return await _list_cqs_for_global_timeline(
        db, timeline_id=timeline_id,
    )


async def _assert_cq_is_on_global_timeline(
    db: AsyncSession, cq: ConditionalQuestion,
) -> None:
    """Confirm the CQ's parent Timeline is rooted on a Global parent
    (CCA Package or CHA-PG, both with `client_id IS NULL`). Used by
    every CQ mutating endpoint (PUT / DELETE) so PG-rooted CQs work
    through the same shared URLs as CCA. Batch 39P-c, 2026-05-16."""
    tl = (await db.execute(
        select(Timeline).where(Timeline.id == cq.timeline_id)
    )).scalar_one_or_none()
    if tl is None:
        raise HTTPException(
            status_code=404,
            detail="Conditional Question has no parent timeline",
        )
    if tl.package_id is not None:
        pkg = (await db.execute(
            select(Package).where(Package.id == tl.package_id)
        )).scalar_one_or_none()
        if pkg and pkg.client_id is None:
            return
    if tl.pg_recommendation_id is not None:
        pg = (await db.execute(
            select(PGRecommendation).where(PGRecommendation.id == tl.pg_recommendation_id)
        )).scalar_one_or_none()
        if pg and pg.client_id is None:
            return
    raise HTTPException(
        status_code=404,
        detail="Conditional Question is not on a Global parent",
    )


@router.put(
    "/advisory/global/conditional-questions/{question_id}",
)
async def update_global_conditional_question(
    question_id: str,
    request: CQReplace,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Atomic replace: update the question text and rebind the YES /
    NO sides in one transaction. Mirrors the create-time semantics —
    each side is a Practice (Path B) or a Relation (Path A) or None.

    Reuses the existing assert_*_can_be_linked_to_conditional checks
    so the same conflict rules apply (an entity bound to a different
    CQ is rejected; same-CQ rebinds are allowed because we clear all
    of THIS CQ's existing rows first)."""
    await _assert_sa_or_cm(db, current_user)
    cq = (await db.execute(
        select(ConditionalQuestion).where(ConditionalQuestion.id == question_id)
    )).scalar_one_or_none()
    if cq is None:
        raise HTTPException(status_code=404, detail="Conditional Question not found")
    await _assert_cq_is_on_global_timeline(db, cq)

    if not request.question_text.strip():
        raise HTTPException(status_code=422, detail={
            "code": "cq_question_text_required",
            "message": "Question text is required.",
        })

    # Clear existing attachments for THIS CQ first, so a rebind to the
    # same entity on the same CQ doesn't trip the
    # "already in another conditional" guard. Entities bound to a
    # DIFFERENT CQ are still rejected by the assert_* checks below.
    await db.execute(
        PracticeConditional.__table__.delete().where(
            PracticeConditional.question_id == question_id
        )
    )
    await db.execute(
        RelationConditional.__table__.delete().where(
            RelationConditional.question_id == question_id
        )
    )
    await db.flush()

    # Helper that validates + inserts one side. Mirrors the create-time
    # link endpoints (Path B for practice, Path A for relation).
    async def _bind(att, answer_value: str):
        if att is None:
            return
        ans = ConditionalAnswer(answer_value)
        if att.kind == "practice":
            practice = (await db.execute(
                select(Practice).where(Practice.id == att.id)
            )).scalar_one_or_none()
            if practice is None:
                raise HTTPException(status_code=404, detail=f"Practice {att.id} not found")
            existing_pc = (await db.execute(
                select(PracticeConditional).where(
                    PracticeConditional.practice_id == att.id,
                )
            )).scalar_one_or_none()
            existing_q_id = existing_pc.question_id if existing_pc else None
            try:
                assert_practice_can_be_linked_to_conditional(
                    practice_id=att.id,
                    practice_relation_id=practice.relation_id,
                    target_question_id=question_id,
                    existing_question_id_for_practice=existing_q_id,
                )
            except ConditionalValidationError as e:
                _raise_conditional_validation(e)
            db.add(PracticeConditional(
                practice_id=att.id, question_id=question_id, answer=ans,
            ))
        elif att.kind == "relation":
            relation = (await db.execute(
                select(Relation).where(Relation.id == att.id)
            )).scalar_one_or_none()
            if relation is None:
                raise HTTPException(status_code=404, detail=f"Relation {att.id} not found")
            existing_rc = (await db.execute(
                select(RelationConditional).where(
                    RelationConditional.relation_id == att.id,
                )
            )).scalar_one_or_none()
            existing_q_id = existing_rc.question_id if existing_rc else None
            try:
                assert_relation_can_be_linked_to_conditional(
                    relation_id=att.id,
                    target_question_id=question_id,
                    existing_question_id_for_relation=existing_q_id,
                )
            except ConditionalValidationError as e:
                _raise_conditional_validation(e)
            db.add(RelationConditional(
                relation_id=att.id, question_id=question_id, answer=ans,
            ))
        else:
            raise HTTPException(status_code=422, detail={
                "code": "cq_attachment_unknown_kind",
                "message": f"Unknown attachment kind: {att.kind!r}",
            })

    await _bind(request.yes, "YES")
    await _bind(request.no, "NO")

    cq.question_text = request.question_text.strip()
    await db.commit()
    await db.refresh(cq)
    return {
        "id": cq.id,
        "timeline_id": cq.timeline_id,
        "question_text": cq.question_text,
        "display_order": cq.display_order,
    }


@router.delete(
    "/advisory/global/conditional-questions/{question_id}",
    status_code=204,
)
async def delete_global_conditional_question(
    question_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Drop a Conditional Question on a Global Timeline. Cascades by
    deleting every PracticeConditional / RelationConditional row that
    bound to this CQ (the practices / relations themselves stay in
    place — only the gating goes). Path-Global mirror of the existing
    client-scoped deletes. Pipe-agnostic after Batch 39P-c."""
    await _assert_sa_or_cm(db, current_user)
    cq = (await db.execute(
        select(ConditionalQuestion).where(ConditionalQuestion.id == question_id)
    )).scalar_one_or_none()
    if cq is None:
        raise HTTPException(status_code=404, detail="Conditional Question not found")
    await _assert_cq_is_on_global_timeline(db, cq)
    # Drop attachments first to satisfy any FK ordering.
    await db.execute(
        PracticeConditional.__table__.delete().where(
            PracticeConditional.question_id == question_id
        )
    )
    await db.execute(
        RelationConditional.__table__.delete().where(
            RelationConditional.question_id == question_id
        )
    )
    await db.delete(cq)
    await db.commit()


@router.post(
    "/advisory/global/practices/{practice_id}/conditionals",
    status_code=201,
)
async def link_global_practice_conditional(
    practice_id: str,
    request: PracticeConditionalCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bind a CQ to an independent Practice on a Global Timeline (Path B —
    the practice is not part of any saved Relation). Same idempotency
    and conflict rules as the client-scoped endpoint."""
    await _assert_sa_or_cm(db, current_user)
    practice = await _get_global_practice(db, practice_id)

    existing_pc = (await db.execute(
        select(PracticeConditional).where(
            PracticeConditional.practice_id == practice_id,
        )
    )).scalar_one_or_none()
    existing_q_id = existing_pc.question_id if existing_pc else None

    try:
        assert_practice_can_be_linked_to_conditional(
            practice_id=practice_id,
            practice_relation_id=practice.relation_id,
            target_question_id=request.question_id,
            existing_question_id_for_practice=existing_q_id,
        )
    except ConditionalValidationError as e:
        _raise_conditional_validation(e)

    if existing_pc is not None and existing_pc.question_id == request.question_id:
        existing_pc.answer = request.answer
        await db.commit()
        await db.refresh(existing_pc)
        return existing_pc

    pc = PracticeConditional(
        practice_id=practice_id,
        question_id=request.question_id,
        answer=request.answer,
    )
    db.add(pc)
    await db.commit()
    await db.refresh(pc)
    return pc


@router.post(
    "/advisory/global/relations/{relation_id}/conditionals",
    status_code=201,
)
async def link_global_relation_conditional(
    relation_id: str,
    request: PracticeConditionalCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bind a CQ to a saved Relation on a Global Timeline (Path A —
    the whole Relation is conditional). `request.practice_id` is ignored;
    the resource is the Relation in the URL."""
    await _assert_sa_or_cm(db, current_user)
    await _get_global_relation(db, relation_id)

    existing_rc = (await db.execute(
        select(RelationConditional).where(
            RelationConditional.relation_id == relation_id,
        )
    )).scalar_one_or_none()
    existing_q_id = existing_rc.question_id if existing_rc else None

    try:
        assert_relation_can_be_linked_to_conditional(
            relation_id=relation_id,
            target_question_id=request.question_id,
            existing_question_id_for_relation=existing_q_id,
        )
    except ConditionalValidationError as e:
        _raise_conditional_validation(e)

    if existing_rc is not None and existing_rc.question_id == request.question_id:
        existing_rc.answer = request.answer
        await db.commit()
        await db.refresh(existing_rc)
        return existing_rc

    rc = RelationConditional(
        relation_id=relation_id,
        question_id=request.question_id,
        answer=request.answer,
    )
    db.add(rc)
    await db.commit()
    await db.refresh(rc)
    return rc


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_package(db: AsyncSession, package_id: str, client_id: str) -> Package:
    result = await db.execute(
        select(Package).where(Package.id == package_id, Package.client_id == client_id)
    )
    pkg = result.scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found")
    return pkg


async def _get_timeline(db: AsyncSession, timeline_id: str, package_id: str) -> Timeline:
    result = await db.execute(
        select(Timeline).where(Timeline.id == timeline_id, Timeline.package_id == package_id)
    )
    tl = result.scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")
    return tl


# ── Global CCA Packages ────────────────────────────────────────────────────────

@router.get("/advisory/global/packages", response_model=list[PackageOut])
async def list_global_packages(
    include_drafts: bool = False,
    crop_cosh_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Global packages visible for fork into client scope. Defaults to
    ACTIVE only — DRAFT and INACTIVE rows are hidden from CA-portal
    SEs. CMs can pass `include_drafts=true` to see everything in their
    own admin views.

    `crop_cosh_id` filter feeds the SA-portal four-screen hub
    drill-down from Crops → Packages.
    """
    q = select(Package).where(Package.client_id == None)  # noqa: E711
    if not include_drafts:
        q = q.where(Package.status == PackageStatus.ACTIVE)
    if crop_cosh_id:
        q = q.where(Package.crop_cosh_id == crop_cosh_id)
    q = q.order_by(Package.created_at.desc())
    result = await db.execute(q)
    return result.scalars().all()


# ── SA-portal four-screen hub (Cosh-crop universe + Global cross-cuts) ─────

@router.get("/advisory/global/cca/crops")
async def global_cca_list_crops(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA-portal Crops screen — the full Cosh crop universe with
    per-crop Global Package status counts. Mirrors the CA-portal's
    `/client/{cid}/cca/crops` but scoped to Global (client_id IS NULL)
    and sourced from every Cosh-classified crop, not a per-client
    belt subset.

    Per the locked 2026-05-11 model, CMs have access to every Cosh
    crop for CCA authoring — even crops with zero Global Packages
    show up so the CM can start one. Crops with no packages get an
    empty `package_counts` dict.
    """
    from app.services.cosh_crop_view import list_crops as _list_cosh_crops

    cosh_crops = await _list_cosh_crops(db)

    counts_q = (await db.execute(
        select(Package.crop_cosh_id, Package.status, func.count())
        .where(Package.client_id == None)  # noqa: E711
        .group_by(Package.crop_cosh_id, Package.status)
    )).all()
    counts: dict[str, dict[str, int]] = {}
    for crop_cosh_id, status, n in counts_q:
        counts.setdefault(crop_cosh_id, {})[
            status.value if hasattr(status, "value") else str(status)
        ] = n

    last_q = (await db.execute(
        select(Package.crop_cosh_id, func.max(Package.updated_at))
        .where(Package.client_id == None)  # noqa: E711
        .group_by(Package.crop_cosh_id)
    )).all()
    last_edited = {crop_cosh_id: ts for crop_cosh_id, ts in last_q}

    return [
        {
            "crop_cosh_id": c.get("cosh_id"),
            "name_en": c.get("name_en") or c.get("cosh_id"),
            "package_counts": counts.get(c.get("cosh_id"), {}),
            "last_edited": last_edited.get(c.get("cosh_id")),
        }
        for c in cosh_crops
    ]


@router.get("/advisory/global/cca/timelines")
async def global_cca_list_timelines(
    crop_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cross-Global-Package timeline list for the SA-portal Timelines
    screen. Mirror of CA's `/client/{cid}/cca/timelines` but scoped
    to `Package.client_id IS NULL`.
    """
    q = (
        select(Timeline, Package)
        .join(Package, Timeline.package_id == Package.id)
        .where(Package.client_id == None)  # noqa: E711
    )
    if crop_cosh_id:
        q = q.where(Package.crop_cosh_id == crop_cosh_id)
    if package_id:
        q = q.where(Timeline.package_id == package_id)
    q = q.order_by(Package.name, Timeline.display_order, Timeline.from_value)

    rows = (await db.execute(q)).all()
    if not rows:
        return []

    tl_ids = [tl.id for tl, _ in rows]
    practice_counts = dict((await db.execute(
        select(Practice.timeline_id, func.count())
        .where(Practice.timeline_id.in_(tl_ids))
        .group_by(Practice.timeline_id)
    )).all())

    crop_ids = {p.crop_cosh_id for _, p in rows}
    crop_names = await _crop_names_by_cosh_id(db, crop_ids)

    return [
        {
            "id": tl.id,
            "name": tl.name,
            "from_type": tl.from_type.value if hasattr(tl.from_type, "value") else str(tl.from_type),
            "from_value": tl.from_value,
            "to_value": tl.to_value,
            "display_order": tl.display_order,
            "package_id": pkg.id,
            "package_name": pkg.name,
            "package_status": pkg.status.value,
            "crop_cosh_id": pkg.crop_cosh_id,
            "crop_name_en": crop_names.get(pkg.crop_cosh_id, pkg.crop_cosh_id),
            "practice_count": practice_counts.get(tl.id, 0),
        }
        for tl, pkg in rows
    ]


@router.get("/advisory/global/cca/practices")
async def global_cca_list_practices(
    crop_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    timeline_id: Optional[str] = None,
    l0: Optional[str] = None,
    l1: Optional[str] = None,
    l2: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cross-Global-Timeline practice list for the SA-portal Practices
    screen. Mirror of CA's `/client/{cid}/cca/practices` but scoped
    to `Package.client_id IS NULL`. Filter chips on every level
    plus L0/L1/L2. Paginated (default 100).
    """
    if limit < 1 or limit > 500:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_limit", "message": "limit must be 1..500"},
        )
    if offset < 0:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_offset", "message": "offset must be >= 0"},
        )

    q = (
        select(Practice, Timeline, Package)
        .join(Timeline, Practice.timeline_id == Timeline.id)
        .join(Package, Timeline.package_id == Package.id)
        .where(Package.client_id == None)  # noqa: E711
    )
    if crop_cosh_id:
        q = q.where(Package.crop_cosh_id == crop_cosh_id)
    if package_id:
        q = q.where(Timeline.package_id == package_id)
    if timeline_id:
        q = q.where(Practice.timeline_id == timeline_id)
    if l0:
        q = q.where(Practice.l0_type == l0)
    if l1:
        q = q.where(Practice.l1_type == l1)
    if l2:
        q = q.where(Practice.l2_type == l2)

    total = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar() or 0

    q = q.order_by(Package.name, Timeline.display_order, Practice.display_order)
    q = q.offset(offset).limit(limit)

    rows = (await db.execute(q)).all()
    if not rows:
        return {"items": [], "total": total, "limit": limit, "offset": offset}

    practice_ids = [pr.id for pr, _, _ in rows]
    elements_by_practice: dict[str, list[Element]] = {}
    if practice_ids:
        elem_rows = (await db.execute(
            select(Element)
            .where(Element.practice_id.in_(practice_ids))
            .order_by(Element.display_order)
        )).scalars().all()
        for e in elem_rows:
            elements_by_practice.setdefault(e.practice_id, []).append(e)

    crop_ids = {p.crop_cosh_id for _, _, p in rows}
    crop_names = await _crop_names_by_cosh_id(db, crop_ids)

    items = []
    for pr, tl, pkg in rows:
        elems = elements_by_practice.get(pr.id, [])
        items.append({
            "id": pr.id,
            "l0_type": pr.l0_type.value if hasattr(pr.l0_type, "value") else str(pr.l0_type),
            "l1_type": pr.l1_type,
            "l2_type": pr.l2_type,
            "display_order": pr.display_order,
            "is_special_input": pr.is_special_input,
            "frequency_days": pr.frequency_days,
            "timeline_id": tl.id,
            "timeline_name": tl.name,
            "package_id": pkg.id,
            "package_name": pkg.name,
            "package_status": pkg.status.value,
            "crop_cosh_id": pkg.crop_cosh_id,
            "crop_name_en": crop_names.get(pkg.crop_cosh_id, pkg.crop_cosh_id),
            "element_summary": [
                {
                    "element_type": e.element_type,
                    "value": e.value,
                    "unit_cosh_id": e.unit_cosh_id,
                    "cosh_ref": e.cosh_ref,
                }
                for e in elems
            ],
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/advisory/global/packages", response_model=PackageOut, status_code=201)
async def create_global_package(
    request: PackageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_sa_or_cm(db, current_user)
    pkg = Package(
        client_id=None,
        crop_cosh_id=request.crop_cosh_id,
        name=request.name,
        package_type=request.package_type,
        duration_days=request.duration_days or 120,
        start_date_label_cosh_id=request.start_date_label_cosh_id,
        description=request.description,
        created_by=current_user.id,
    )
    db.add(pkg)
    await db.commit()
    await db.refresh(pkg)
    return pkg


@router.get("/advisory/global/packages/{pkg_id}", response_model=PackageOut)
async def get_global_package(
    pkg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Package).where(Package.id == pkg_id, Package.client_id == None)  # noqa: E711
    )
    pkg = result.scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=404, detail="Global package not found")
    return pkg


@router.put("/advisory/global/packages/{pkg_id}", response_model=PackageOut)
async def update_global_package(
    pkg_id: str,
    request: PackageUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit a Global Package's name / duration / start-date label /
    description after creation. `package_type` and `crop_cosh_id`
    stay immutable — changing crop on a published template would
    orphan content semantics; package_type drives duration rules.

    Mirrors the client-scoped `update_package` validator: duration
    range-checked for ANNUAL, locked at 365 for PERENNIAL.
    """
    await _assert_sa_or_cm(db, current_user)
    pkg = (await db.execute(
        select(Package).where(
            Package.id == pkg_id, Package.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=404, detail="Global package not found")
    update_data = request.model_dump(exclude_unset=True)

    if "duration_days" in update_data:
        try:
            update_data["duration_days"] = validate_package_duration_for_update(
                package_type=pkg.package_type.value,
                current_duration=pkg.duration_days,
                new_duration=update_data["duration_days"],
            )
        except PackageValidationError as e:
            raise HTTPException(
                status_code=422,
                detail={"code": e.code, "message": e.message},
            )

    # Batch 28: status toggle. SE can flip ACTIVE ↔ INACTIVE and may
    # discard a DRAFT (DRAFT → INACTIVE), but cannot promote DRAFT →
    # ACTIVE directly — that path goes through the publish endpoint
    # which runs lineage migration. INACTIVE → ACTIVE is allowed so
    # the SE can resurrect a retired package.
    if "status" in update_data:
        new_status_str = update_data.pop("status")
        try:
            new_status = PackageStatus(new_status_str)
        except ValueError:
            raise HTTPException(status_code=422, detail={
                "code": "invalid_status",
                "message": f"status must be one of {[s.value for s in PackageStatus]}",
            })
        if new_status != pkg.status:
            if pkg.status == PackageStatus.DRAFT and new_status == PackageStatus.ACTIVE:
                raise HTTPException(status_code=422, detail={
                    "code": "publish_required",
                    "message": "DRAFT → ACTIVE must go through the publish endpoint, not the status toggle.",
                })
            pkg.status = new_status

    for field, value in update_data.items():
        setattr(pkg, field, value)
    await db.commit()
    await db.refresh(pkg)
    return pkg


@router.get("/advisory/global/packages/{pkg_id}/push-status")
async def get_global_package_push_status(
    pkg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA-portal helper: for each client this CM can edit, report
    whether the Global Package has been pushed and (if so) what
    state the client's lineage is in.

    Shape (per entry):
      client_id, client_name: identifying.
      already_pushed: bool — any Local row exists for
        (client_id, parent_global_id=pkg_id).
      pushed_at: ISO timestamp of the earliest Local row in the
        lineage (i.e., first contact). NULL when not yet pushed.
      latest_local_published_at: ISO timestamp of the most
        recently published Local row in the lineage. NULL when
        the SE hasn't published anything yet (e.g., the CM-push
        DRAFT is still sitting in DRAFT). The CM compares this
        to the Global's `published_at` to spot clients that
        haven't pulled a fresh version.
      has_pending_draft: bool — true if any DRAFT exists in the
        client's lineage right now (typically an in-flight SE
        edit or SE pull that hasn't been published yet).

    Auth: caller must be a CM (any active CMClientAssignment).
    Returns 404 if the Global Package doesn't exist. Returns
    empty list if the CM has no active assignments — the SA-portal
    push surface is then non-actionable for this Global.
    """
    src = (await db.execute(
        select(Package).where(
            Package.id == pkg_id, Package.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if not src:
        raise HTTPException(status_code=404, detail="Global package not found")

    from app.modules.clients.models import (
        Client, CMClientAssignment, CMRights,
    )
    from app.modules.platform.models import StatusEnum

    assignments = (await db.execute(
        select(CMClientAssignment).where(
            CMClientAssignment.cm_user_id == current_user.id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
            CMClientAssignment.rights == CMRights.EDIT,
        )
    )).scalars().all()
    if not assignments:
        return []

    client_ids = [a.client_id for a in assignments]
    clients_by_id = {
        c.id: c for c in (await db.execute(
            select(Client).where(Client.id.in_(client_ids))
        )).scalars().all()
    }

    # Lineage-aware lookup (Batch 39N-a, 2026-05-16). The "already
    # pushed" flag must reflect the whole Global lineage — a Local
    # row whose `parent_global_id` points at any historical Global
    # row in the same (client_id=NULL, crop, name) lineage still
    # counts as pushed. Without this, a new Global v_{N+1} would
    # incorrectly report all clients as "not yet pushed."
    lineage_ids = list((await db.execute(
        select(Package.id).where(
            Package.client_id == None,  # noqa: E711
            Package.crop_cosh_id == src.crop_cosh_id,
            Package.name == src.name,
        )
    )).scalars().all())

    local_rows = (await db.execute(
        select(Package).where(
            Package.parent_global_id.in_(lineage_ids),
            Package.client_id.in_(client_ids),
        )
    )).scalars().all()
    rows_by_client: dict[str, list[Package]] = {}
    for r in local_rows:
        rows_by_client.setdefault(r.client_id, []).append(r)

    out = []
    for cid in client_ids:
        client = clients_by_id.get(cid)
        if not client:
            continue
        rows = rows_by_client.get(cid, [])
        pushed_at = None
        latest_pub = None
        has_pending_draft = False
        for r in rows:
            if pushed_at is None or r.created_at < pushed_at:
                pushed_at = r.created_at
            if r.published_at is not None:
                if latest_pub is None or r.published_at > latest_pub:
                    latest_pub = r.published_at
            if r.status == PackageStatus.DRAFT:
                has_pending_draft = True
        out.append({
            "client_id": cid,
            "client_name": (
                client.display_name or client.full_name or client.short_name
            ),
            "already_pushed": bool(rows),
            "pushed_at": pushed_at.isoformat() if pushed_at else None,
            "latest_local_published_at": (
                latest_pub.isoformat() if latest_pub else None
            ),
            "has_pending_draft": has_pending_draft,
        })
    # Stable ordering: not-yet-pushed clients first (actionable
    # for the CM), then by client name.
    out.sort(key=lambda e: (e["already_pushed"], e["client_name"]))
    return out


# ── Global Parameters / Variables / PackageVariables (Batch 9, 2026-05-11) ──
#
# Globals carry their own PV signature so the CM can distinguish multiple
# Packages for the same crop (e.g. "Tomato — Drip" vs "Tomato — Flood").
# On push, the Local PackageVariable rows reference these Global Parameter
# and Variable rows directly — no cloning, since Parameters with
# client_id=NULL are visible across all clients. The client-side §4.2
# sibling check then sees the inherited fingerprint and behaves correctly.

@router.get("/advisory/global/parameters")
async def list_global_parameters(
    crop_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List Global Parameters for a crop. Mirrors Cosh-side
    `package_parameters` for this crop into the local table on
    first read (Cosh shipped `crops_parameters_variables` Connect
    on 2026-05-12), then returns the combined Cosh + CUSTOM set
    visible to all clients via `client_id IS NULL`.
    """
    from app.services.cosh_pv_view import ensure_local_parameters_for_crop
    await ensure_local_parameters_for_crop(db, crop_cosh_id)
    result = await db.execute(
        select(Parameter).where(
            Parameter.crop_cosh_id == crop_cosh_id,
            Parameter.client_id == None,  # noqa: E711
        ).order_by(Parameter.display_order, Parameter.name)
    )
    return result.scalars().all()


@router.post("/advisory/global/parameters", status_code=201)
async def create_global_parameter(
    request: ParameterCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a CUSTOM Global Parameter together with its initial
    variables. Atomic per Batch 28 (user 2026-05-14): a Parameter
    must have ≥ 2 variables at creation; the schema's `min_length=2`
    enforces this, returning 422 with field details when violated.

    Visible to every Local Package via FK from PackageVariable; the
    SA-portal authors them, Local Packages reference them after push."""
    await _assert_sa_or_cm(db, current_user)
    param = Parameter(
        crop_cosh_id=request.crop_cosh_id,
        client_id=None,
        name=request.name,
        source=ParameterSource.CUSTOM,
        display_order=request.display_order,
    )
    db.add(param)
    await db.flush()  # need param.id for the variable rows
    for v in request.variables:
        db.add(Variable(parameter_id=param.id, name=v.name))
    await db.commit()
    await db.refresh(param)
    return param


@router.put("/advisory/global/parameters/{parameter_id}")
async def update_global_parameter(
    parameter_id: str,
    request: ParameterUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Rename or re-order a CUSTOM Global Parameter (Batch 28).
    Refuses on Cosh-mirrored parameters — those track upstream Cosh
    translations and shouldn't be edited locally."""
    await _assert_sa_or_cm(db, current_user)
    param = (await db.execute(
        select(Parameter).where(
            Parameter.id == parameter_id, Parameter.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if param is None:
        raise HTTPException(status_code=404, detail="Global parameter not found")
    if param.source != ParameterSource.CUSTOM:
        raise HTTPException(status_code=422, detail={
            "code": "cosh_mirrored_parameter_readonly",
            "message": "Cosh-mirrored parameters can't be edited; Cosh is the source of truth.",
        })
    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(param, field, value)
    await db.commit()
    await db.refresh(param)
    return param


@router.delete("/advisory/global/parameters/{parameter_id}", status_code=204)
async def delete_global_parameter(
    parameter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a CUSTOM Global Parameter and its variables. Refuses if
    any PackageVariable still references it — SE must clear the
    Package signature first. Refuses on Cosh-mirrored parameters."""
    await _assert_sa_or_cm(db, current_user)
    from app.modules.advisory.models import PackageVariable
    param = (await db.execute(
        select(Parameter).where(
            Parameter.id == parameter_id, Parameter.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if param is None:
        raise HTTPException(status_code=404, detail="Global parameter not found")
    if param.source != ParameterSource.CUSTOM:
        raise HTTPException(status_code=422, detail={
            "code": "cosh_mirrored_parameter_readonly",
            "message": "Cosh-mirrored parameters can't be deleted; Cosh is the source of truth.",
        })
    in_use = (await db.execute(
        select(PackageVariable).where(
            PackageVariable.parameter_id == parameter_id,
        ).limit(1)
    )).scalar_one_or_none()
    if in_use is not None:
        raise HTTPException(status_code=422, detail={
            "code": "parameter_in_use",
            "message": "Parameter is in use by one or more Package signatures. Clear those signatures first.",
        })
    # Cascade delete variables (no PackageVariable refs since we checked).
    await db.execute(
        Variable.__table__.delete().where(Variable.parameter_id == parameter_id)
    )
    await db.delete(param)
    await db.commit()


@router.put("/advisory/global/parameters/{parameter_id}/variables/{variable_id}", response_model=VariableOut)
async def update_global_variable(
    parameter_id: str, variable_id: str,
    request: VariableUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Rename a Variable under any Global Parameter. Batch 35
    (2026-05-14): the gate moved from the parent Parameter's source
    to THIS Variable's source. SE-added variables (Variable.cosh_id
    NULL) are editable on either CUSTOM or COSH-mirrored parents —
    only the Cosh-mirrored variables themselves (cosh_id non-NULL)
    are read-only."""
    await _assert_sa_or_cm(db, current_user)
    param = (await db.execute(
        select(Parameter).where(
            Parameter.id == parameter_id, Parameter.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if param is None:
        raise HTTPException(status_code=404, detail="Global parameter not found")
    var = (await db.execute(
        select(Variable).where(
            Variable.id == variable_id,
            Variable.parameter_id == parameter_id,
        )
    )).scalar_one_or_none()
    if var is None:
        raise HTTPException(status_code=404, detail="Variable not found")
    if var.cosh_id is not None:
        raise HTTPException(status_code=422, detail={
            "code": "cosh_mirrored_variable_readonly",
            "message": "Cosh-mirrored variables can't be edited; Cosh is the source of truth.",
        })
    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(var, field, value)
    await db.commit()
    await db.refresh(var)
    return var


@router.delete(
    "/advisory/global/parameters/{parameter_id}/variables/{variable_id}",
    status_code=204,
)
async def delete_global_variable(
    parameter_id: str, variable_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a Variable from a Global Parameter. Batch 35 gates on
    `variable.cosh_id` (SE-added variables on Cosh-mirrored parents
    are deletable; Cosh-mirrored variables themselves are not).

    Min-2 invariant from Batch 28 applies only to CUSTOM parents
    (a CUSTOM Parameter must keep ≥ 2 active variables). For
    COSH-mirrored parents Cosh decides the floor, so SE deletes of
    locally-added variables don't trip min-2.

    Refuses if the variable is in use by any PackageVariable."""
    await _assert_sa_or_cm(db, current_user)
    from app.modules.advisory.models import PackageVariable
    param = (await db.execute(
        select(Parameter).where(
            Parameter.id == parameter_id, Parameter.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if param is None:
        raise HTTPException(status_code=404, detail="Global parameter not found")
    var = (await db.execute(
        select(Variable).where(
            Variable.id == variable_id,
            Variable.parameter_id == parameter_id,
        )
    )).scalar_one_or_none()
    if var is None:
        raise HTTPException(status_code=404, detail="Variable not found")
    if var.cosh_id is not None:
        raise HTTPException(status_code=422, detail={
            "code": "cosh_mirrored_variable_readonly",
            "message": "Cosh-mirrored variables can't be deleted; Cosh is the source of truth.",
        })

    if param.source == ParameterSource.CUSTOM:
        sibling_count = (await db.execute(
            select(func.count()).select_from(Variable).where(
                Variable.parameter_id == parameter_id,
            )
        )).scalar()
        if sibling_count <= 2:
            raise HTTPException(status_code=422, detail={
                "code": "min_two_variables",
                "message": "A CUSTOM Parameter must have at least 2 variables.",
            })

    in_use = (await db.execute(
        select(PackageVariable).where(
            PackageVariable.variable_id == variable_id,
        ).limit(1)
    )).scalar_one_or_none()
    if in_use is not None:
        raise HTTPException(status_code=422, detail={
            "code": "variable_in_use",
            "message": "Variable is in use by one or more Package signatures. Clear those signatures first.",
        })

    await db.delete(var)
    await db.commit()


@router.get("/advisory/global/parameters/{parameter_id}/variables", response_model=list[VariableOut])
async def list_global_variables(
    parameter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List Variables for a Global Parameter."""
    # Defence: refuse to list variables off a client-scoped Parameter
    # through this endpoint — keep the global/local separation clear.
    param = (await db.execute(
        select(Parameter).where(
            Parameter.id == parameter_id, Parameter.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if param is None:
        raise HTTPException(status_code=404, detail="Global parameter not found")
    result = await db.execute(
        select(Variable).where(Variable.parameter_id == parameter_id)
        .order_by(Variable.created_at)
    )
    return result.scalars().all()


@router.post("/advisory/global/parameters/{parameter_id}/variables", status_code=201, response_model=VariableOut)
async def create_global_variable(
    parameter_id: str,
    request: VariableCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a Variable to a Global Parameter."""
    await _assert_sa_or_cm(db, current_user)
    param = (await db.execute(
        select(Parameter).where(
            Parameter.id == parameter_id, Parameter.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if param is None:
        raise HTTPException(status_code=404, detail="Global parameter not found")
    var = Variable(parameter_id=parameter_id, name=request.name)
    db.add(var)
    await db.commit()
    await db.refresh(var)
    return var


@router.get("/advisory/global/packages/{pkg_id}/variables")
async def list_global_package_variables(
    pkg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the parameter→variable fingerprint set on a Global Package.
    Defaults to empty when none assigned yet."""
    pkg = (await db.execute(
        select(Package).where(
            Package.id == pkg_id, Package.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if pkg is None:
        raise HTTPException(status_code=404, detail="Global package not found")
    rows = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id == pkg_id)
    )).scalars().all()
    return [
        {
            "parameter_id": pv.parameter_id,
            "variable_id": pv.variable_id,
        }
        for pv in rows
    ]


@router.put("/advisory/global/packages/{pkg_id}/variables")
async def set_global_package_variables(
    pkg_id: str,
    request: PackageVariableSet,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set the parameter→variable fingerprint on a Global Package.

    No §4.2 sibling check here — that's a client-side rule (multiple
    PoPs sharing districts at the same client). At Global scope,
    two Tomato PoPs co-existing is the *intent*: different farmer
    profiles. The CM uses PVs to make them distinguishable on push.
    """
    await _assert_sa_or_cm(db, current_user)
    pkg = (await db.execute(
        select(Package).where(
            Package.id == pkg_id, Package.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if pkg is None:
        raise HTTPException(status_code=404, detail="Global package not found")
    existing = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id == pkg_id)
    )).scalars().all()
    for pv in existing:
        await db.delete(pv)
    # Flush before adding the new rows — otherwise SQLAlchemy may
    # batch the INSERTs ahead of the DELETEs in the same flush and
    # the (package_id, parameter_id) unique constraint trips when
    # the new assignment reuses a previously-set parameter_id.
    await db.flush()
    for assignment in request.assignments:
        db.add(PackageVariable(
            package_id=pkg_id,
            parameter_id=assignment["parameter_id"],
            variable_id=assignment["variable_id"],
        ))
    await db.commit()
    return {"detail": f"{len(request.assignments)} parameter-variable assignments saved"}


@router.post("/advisory/global/packages/{pkg_id}/publish", response_model=PackageOut)
async def publish_global_package(
    pkg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_sa_or_cm(db, current_user)
    result = await db.execute(
        select(Package).where(Package.id == pkg_id, Package.client_id == None)  # noqa: E711
    )
    pkg = result.scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=404, detail="Global package not found")
    current_status = pkg.status.value if hasattr(pkg.status, "value") else str(pkg.status)
    res = validate_publish_transition(current_status)
    if not res.allowed:
        _raise_publish_transition(res)
    # Batch 39L-a (2026-05-16) — content gate: ≥1 active Timeline,
    # every active Timeline ≥1 Practice. Raises PublishBlockedError
    # mapped to a 422 with the full failure list.
    try:
        await assert_global_package_publish_ready(db, package=pkg)
    except PublishBlockedError as e:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "publish_blocked",
                "message": "Package is not ready for publish.",
                "missing": [
                    {"code": m.code, "message": m.message, "extra": m.extra}
                    for m in e.missing
                ],
            },
        )
    pkg.version = compute_publish_version(
        current_version=pkg.version, was_published=pkg.published_at is not None,
    )
    pkg.status = PackageStatus.ACTIVE
    pkg.published_at = datetime.now(timezone.utc)
    pkg.published_by = current_user.id
    await db.commit()
    await db.refresh(pkg)
    return pkg


@router.post(
    "/advisory/global/packages/{pkg_id}/clone-to-draft",
    response_model=PackageOut, status_code=201,
)
async def clone_global_to_draft(
    pkg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Batch 39L-b (2026-05-16) — CM starts a new edit cycle from
    a Global Package. Takes any row in the lineage (ACTIVE or
    INACTIVE) as the source and deep-copies its content into a
    new DRAFT row. Publishing that DRAFT becomes v_{max + 1} per
    the existing compute_publish_version logic.

    Single-DRAFT invariant: any existing DRAFT in the same Global
    lineage (client_id IS NULL + crop + name) is auto-flipped to
    INACTIVE so the CM only sees one editable row at a time.

    Source may be ACTIVE (the typical "edit the current version"
    flow) or INACTIVE (the "go back and edit a previous version"
    flow per CM 2026-05-16) — DRAFT rows are rejected to keep
    the path obvious.
    """
    await _assert_sa_or_cm(db, current_user)
    src = (await db.execute(
        select(Package).where(
            Package.id == pkg_id, Package.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if src is None:
        raise HTTPException(status_code=404, detail="Global package not found")
    if src.status == PackageStatus.DRAFT:
        raise HTTPException(status_code=422, detail={
            "code": "clone_source_is_draft",
            "message": (
                "clone-to-draft requires an ACTIVE or INACTIVE row of "
                "the lineage as the source. The current row is already "
                "a DRAFT — edit it in place."
            ),
        })

    existing_draft = (await db.execute(
        select(Package).where(
            Package.client_id == None,  # noqa: E711
            Package.crop_cosh_id == src.crop_cosh_id,
            Package.name == src.name,
            Package.status == PackageStatus.DRAFT,
        )
    )).scalar_one_or_none()
    if existing_draft:
        existing_draft.status = PackageStatus.INACTIVE

    new_draft = Package(
        client_id=None,
        parent_global_id=src.parent_global_id,
        crop_cosh_id=src.crop_cosh_id,
        name=src.name,
        package_type=src.package_type,
        duration_days=src.duration_days,
        start_date_label_cosh_id=src.start_date_label_cosh_id,
        description=src.description,
        created_by=current_user.id,
        status=PackageStatus.DRAFT,
        # The new DRAFT carries version=1 until published, at which
        # point compute_publish_version stamps max(lineage) + 1.
        version=1,
        source_version_id=src.id,
        created_via=PackageCreatedVia.SE_EDIT_DRAFT,
    )
    db.add(new_draft)
    await db.flush()
    await _deep_copy_package_content(db, src.id, new_draft.id)

    # Carry Locations / Authors / PackageVariables / Parameters
    # too — they're CA-side concepts but Global packages also use
    # them (SE assigns Authors, sets PVs, etc.). Mirror the client
    # clone-to-draft semantics.
    locations = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == src.id)
    )).scalars().all()
    for loc in locations:
        db.add(PackageLocation(
            package_id=new_draft.id,
            state_cosh_id=loc.state_cosh_id,
            district_cosh_id=loc.district_cosh_id,
        ))
    authors = (await db.execute(
        select(PackageAuthor).where(PackageAuthor.package_id == src.id)
    )).scalars().all()
    for a in authors:
        db.add(PackageAuthor(
            package_id=new_draft.id,
            user_id=a.user_id,
            display_order=a.display_order,
        ))
    pvs = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id == src.id)
    )).scalars().all()
    for pv in pvs:
        db.add(PackageVariable(
            package_id=new_draft.id,
            parameter_id=pv.parameter_id,
            variable_id=pv.variable_id,
        ))

    await db.commit()
    await db.refresh(new_draft)
    return new_draft


@router.get(
    "/advisory/global/packages/{pkg_id}/lineage",
)
async def get_global_package_lineage(
    pkg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every row in this Global Package's lineage — rows sharing
    (client_id IS NULL, crop_cosh_id, name). Feeds the SA portal's
    version-history navigator (Batch 39L-c) and the "Continue
    v_{N+1} draft" detection on the detail page (Batch 39L-b)."""
    pkg = (await db.execute(
        select(Package).where(
            Package.id == pkg_id, Package.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if pkg is None:
        raise HTTPException(status_code=404, detail="Global package not found")
    rows = (await db.execute(
        select(Package).where(
            Package.client_id == None,  # noqa: E711
            Package.crop_cosh_id == pkg.crop_cosh_id,
            Package.name == pkg.name,
        ).order_by(Package.version.desc(), Package.created_at.desc())
    )).scalars().all()

    def sort_key(p: Package):
        is_draft = 0 if p.status == PackageStatus.DRAFT else 1
        return (is_draft, -p.version, -p.created_at.timestamp())

    return [
        {
            "id": r.id,
            "status": r.status.value if hasattr(r.status, "value") else r.status,
            "version": r.version,
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "created_at": r.created_at.isoformat(),
            "created_via": (
                r.created_via.value if r.created_via and hasattr(r.created_via, "value")
                else r.created_via
            ),
            "source_version_id": r.source_version_id,
            "is_current": r.id == pkg_id,
        }
        for r in sorted(rows, key=sort_key)
    ]


# ── Practice taxonomy + element specs (2026-05-11) ─────────────────────────
# L0 → L1 → L2 hierarchy + per-L2 element rules. Pure-data
# endpoints used by both the SA and CA portals to render
# cascading Practice dropdowns and element forms. No auth gate
# — the taxonomy itself is non-sensitive reference data shared
# across all roles.

@router.get("/practice-taxonomy")
async def get_practice_taxonomy_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.practice_taxonomy import get_practice_taxonomy
    return get_practice_taxonomy()


@router.get("/practice-taxonomy/elements/{l2_type}")
async def get_l2_element_spec(
    l2_type: str,
    crop_cosh_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return element spec for an L2. When `crop_cosh_id` is
    supplied, the plant-wise extras (VOLUME_PER_PLANT + UNIT)
    are appended only if the crop is classified PLANT_WISE in
    Cosh. AREA_WISE / unclassified crops, or callers that omit
    the param, never see those fields.
    """
    from app.services.cosh_crop_view import get_measure_for_biological_name
    from app.services.practice_taxonomy import get_l2_meta, list_l2_elements

    measure = None
    if crop_cosh_id:
        measure = await get_measure_for_biological_name(db, crop_cosh_id)

    elements = list_l2_elements(l2_type, crop_measure=measure)
    if elements is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "unknown_l2_type",
                "message": f"L2 type {l2_type!r} is not in the rule book.",
            },
        )
    meta = get_l2_meta(l2_type) or {}
    return {
        "l2_type": l2_type,
        "crop_measure": measure,
        "elements": elements,
        # L2-level flags from the rule book — the frontend uses
        # these to decide which UI affordances to surface (e.g.
        # Special Input checkbox shows only when is_special_input
        # is True, frequency-days input shows only when
        # frequency_based is True, …).
        "is_special_input": meta.get("is_special_input", False),
        "frequency_based": meta.get("frequency_based", False),
        "plant_wise_extras": meta.get("plant_wise_extras", False),
    }


# ── Cosh input options + cascades (2026-05-14) ─────────────────────────────
#
# Backs the Add Practice modal's per-L2 dropdowns + the four-stage brand
# cascade (Common Name → Trade Name + Manufacturer → Formulation + a.i.).
# All seven endpoints read through to Cosh-side Connects via
# `app.services.cosh_options_view` — no local mirror.

@router.get("/cosh/options/common-names")
async def cosh_common_names_for_l2(
    l2: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.cosh_options_view import list_common_names_for_l2
    return await list_common_names_for_l2(db, l2)


@router.get("/cosh/options/application-methods")
async def cosh_application_methods_for_l2(
    l2: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.cosh_options_view import list_application_methods_for_l2
    return await list_application_methods_for_l2(db, l2)


@router.get("/cosh/options/units")
async def cosh_units_for_l2(
    l2: str,
    unit_type: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """`unit_type` is the rule-book slug (e.g. `dosage_unit`,
    `volume_unit`, `time_unit`). Maps to a set of Cosh `unit_types`
    UUIDs and filters the L2's units by that set."""
    from app.services.cosh_options_view import list_units_for_l2
    return await list_units_for_l2(db, l2, unit_type)


@router.get("/cosh/options/trade-names")
async def cosh_trade_names_for_common_name(
    common_name: str,
    manufacturer: Optional[str] = None,
    l2: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trade names for a common name. Optional `manufacturer` narrows
    the list to brands made by that manufacturer — used by the
    bidirectional MFR ↔ TN filter on the Add Practice modal.

    Optional `l2` opts into the Batch 39D completeness filter."""
    from app.services.cosh_options_view import list_trade_names_for_common_name
    return await list_trade_names_for_common_name(
        db, common_name, manufacturer_cosh_id=manufacturer, l2_type=l2,
    )


@router.get("/cosh/options/manufacturers")
async def cosh_manufacturers_for_common_name(
    common_name: str,
    trade_name: Optional[str] = None,
    l2: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manufacturers in scope for a common name. Optional `trade_name`
    narrows the list to the single manufacturer that makes that
    brand — used by the bidirectional MFR ↔ TN filter on the Add
    Practice modal.

    Optional `l2` opts into the Batch 39D completeness filter."""
    from app.services.cosh_options_view import list_manufacturers_for_common_name
    return await list_manufacturers_for_common_name(
        db, common_name, trade_name_cosh_id=trade_name, l2_type=l2,
    )


@router.get("/cosh/options/formulations")
async def cosh_formulations(
    common_name: Optional[str] = None,
    trade_name: Optional[str] = None,
    l2: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """When `trade_name` is supplied, narrows to formulations tied to
    that one trade name; otherwise spans all trade names sharing the
    given `common_name`.

    Optional `l2` opts into the Batch 39D completeness filter."""
    from app.services.cosh_options_view import list_formulations
    return await list_formulations(
        db, common_name_cosh_id=common_name, trade_name_cosh_id=trade_name,
        l2_type=l2,
    )


@router.get("/cosh/options/incomplete-report")
async def cosh_incomplete_report(
    l2: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA-facing audit of Cosh-side data gaps under the given L2's
    completeness spec. Returns every CN under this L2 with each TN's
    list of missing required connects. Use it to spot what needs to
    be filled in on the Cosh side so the authoring dropdowns surface
    more CNs."""
    from app.services.cosh_options_view import list_incomplete_cosh_data_for_l2
    return await list_incomplete_cosh_data_for_l2(db, l2)


@router.get("/cosh/options/ai-concentrations")
async def cosh_ai_concentrations(
    common_name: Optional[str] = None,
    trade_name: Optional[str] = None,
    l2: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Optional `l2` opts into the Batch 39D completeness filter."""
    from app.services.cosh_options_view import list_ai_concentrations
    return await list_ai_concentrations(
        db, common_name_cosh_id=common_name, trade_name_cosh_id=trade_name,
        l2_type=l2,
    )


# ── Non-input flat lookups (2026-05-16) ──────────────────────────────
# Three Cosh cores wired to Non-input L0 element forms. Flat reads —
# no L2 filter, no cascade.

@router.get("/cosh/options/planting-materials")
async def cosh_planting_materials(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Options for the PLANTING_MATERIAL element under L2
    PLANTING_MATERIAL_QUANTITY (Non-input L0)."""
    from app.services.cosh_options_view import list_planting_materials
    return await list_planting_materials(db)


@router.get("/cosh/options/itks")
async def cosh_itks(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Options for the ITK_NAME element under L2 ITKS (Non-input L0).
    Cosh ships these under core_type `itk_data`."""
    from app.services.cosh_options_view import list_itks
    return await list_itks(db)


@router.get("/cosh/options/maturity-indices")
async def cosh_maturity_indices(
    crop: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Options for the MATURITY_INDEX element under L2 HARVESTING_MANUAL
    (Non-input L0). The L2 is labelled 'Manual Harvesting' in the UX
    but stored as HARVESTING_MANUAL in the taxonomy. `crop` is the
    package's biological_names cosh_id — only maturity indices linked
    to that crop via Cosh's `maturity_index_crops` Connect surface."""
    from app.services.cosh_options_view import list_maturity_indices_for_crop
    return await list_maturity_indices_for_crop(db, crop_cosh_id=crop)


# Diagnosis lookups moved to `app/modules/diagnosis/router.py` in
# Batch 23 (2026-05-14). See that module for `/diagnosis/*` endpoints.


@router.get("/advisory/global/packages/{pkg_id}/timelines", response_model=list[TimelineOut])
async def list_global_timelines(
    pkg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Timeline).where(Timeline.package_id == pkg_id).order_by(Timeline.display_order, Timeline.from_value)
    )
    return result.scalars().all()


@router.post("/advisory/global/packages/{pkg_id}/timelines", response_model=TimelineOut, status_code=201)
async def create_global_timeline(
    pkg_id: str,
    request: TimelineCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mirrors `create_timeline` for the Global scope. Runs the same
    type/direction/sign validation against the parent Package's
    package_type, plus pre-checks name uniqueness so duplicates
    surface as a friendly 422 instead of a 500 from the DB unique
    constraint.
    """
    await _assert_sa_or_cm(db, current_user)
    pkg = (await db.execute(
        select(Package).where(Package.id == pkg_id, Package.client_id == None)  # noqa: E711
    )).scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=404, detail="Global package not found")

    try:
        validate_timeline(
            package_type=pkg.package_type.value,
            from_type=request.from_type.value,
            from_value=request.from_value,
            to_value=request.to_value,
        )
    except TimelineValidationError as e:
        _raise_timeline_validation(e)

    await _assert_timeline_name_unique(
        db, package_id=pkg_id, name=request.name,
    )

    tl = Timeline(package_id=pkg_id, **request.model_dump())
    db.add(tl)
    await db.commit()
    await db.refresh(tl)
    return tl


@router.put(
    "/advisory/global/packages/{pkg_id}/timelines/{tl_id}",
    response_model=TimelineOut,
)
async def update_global_timeline(
    pkg_id: str, tl_id: str,
    request: TimelineUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit a Global Timeline (Batch 28). Accepts name, from_value,
    to_value, status. `from_type` is intentionally not editable —
    type ↔ package consistency is fixed at create time. Mirrors the
    client-scoped handler at /client/{client_id}/packages/.../timelines/{tl_id}
    including name-uniqueness and direction validation."""
    await _assert_sa_or_cm(db, current_user)
    pkg = (await db.execute(
        select(Package).where(
            Package.id == pkg_id, Package.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if pkg is None:
        raise HTTPException(status_code=404, detail="Global package not found")
    tl = await _get_timeline(db, tl_id, pkg_id)

    update_data = request.model_dump(exclude_unset=True)
    if "name" in update_data and update_data["name"] != tl.name:
        await _assert_timeline_name_unique(
            db, package_id=pkg_id, name=update_data["name"],
            exclude_timeline_id=tl_id,
        )

    new_from = update_data.get("from_value", tl.from_value)
    new_to = update_data.get("to_value", tl.to_value)
    try:
        validate_timeline(
            package_type=pkg.package_type.value,
            from_type=tl.from_type.value,
            from_value=new_from, to_value=new_to,
        )
    except TimelineValidationError as e:
        _raise_timeline_validation(e)

    if "status" in update_data:
        if update_data["status"] not in ("ACTIVE", "INACTIVE"):
            raise HTTPException(status_code=422, detail={
                "code": "invalid_status",
                "message": "Timeline status must be ACTIVE or INACTIVE.",
            })

    for field, value in update_data.items():
        setattr(tl, field, value)
    await db.commit()
    await db.refresh(tl)
    return tl


@router.delete("/advisory/global/packages/{pkg_id}/timelines/{tl_id}", status_code=204)
async def delete_global_timeline(
    pkg_id: str, tl_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_sa_or_cm(db, current_user)
    tl = await _get_timeline(db, tl_id, pkg_id)
    await db.delete(tl)
    await db.commit()


async def _attach_elements_with_labels(
    db: AsyncSession, practices: list[Practice],
) -> list[dict]:
    """Bundle each Practice's element rows with rule-book labels +
    server-resolved English display values (Batch 32). One pass over
    the elements, one batched CoshCoreItem fetch for cosh_ref UUIDs.

    `display_value` priority:
      - cosh_ref: English name from CoshCoreItem.translations
      - value: passes through verbatim (free-text, numeric, etc.)
    """
    from app.modules.advisory.models import Element
    from app.modules.sync.models import CoshCoreItem
    from app.services.practice_taxonomy import _label_from_id

    if not practices:
        return []
    practice_ids = [p.id for p in practices]
    elements = (await db.execute(
        select(Element).where(Element.practice_id.in_(practice_ids))
        .order_by(Element.practice_id, Element.display_order)
    )).scalars().all()

    # Batch-resolve every cosh_ref UUID to its English label.
    cosh_ids = {e.cosh_ref for e in elements if e.cosh_ref}
    en_by_id: dict[str, str] = {}
    if cosh_ids:
        cores = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.cosh_id.in_(cosh_ids),
                CoshCoreItem.status == "active",
            )
        )).scalars().all()
        for c in cores:
            t = c.translations or {}
            en = t.get("en") or t.get("English")
            if en:
                en_by_id[c.cosh_id] = en

    by_practice: dict[str, list[dict]] = {p.id: [] for p in practices}
    for e in elements:
        if e.cosh_ref:
            display = en_by_id.get(e.cosh_ref) or e.cosh_ref
        else:
            display = e.value
        by_practice.setdefault(e.practice_id, []).append({
            "element_type": e.element_type,
            "label": _label_from_id(e.element_type),
            "cosh_ref": e.cosh_ref,
            "value": e.value,
            "unit_cosh_id": e.unit_cosh_id,
            "display_value": display,
            "display_order": e.display_order,
        })

    out: list[dict] = []
    for p in practices:
        out.append({
            "id": p.id, "timeline_id": p.timeline_id,
            "l0_type": p.l0_type, "l1_type": p.l1_type, "l2_type": p.l2_type,
            "display_order": p.display_order,
            "is_special_input": p.is_special_input,
            "is_brand_locked": p.is_brand_locked,
            "relation_id": p.relation_id,
            "created_at": p.created_at,
            "elements": by_practice.get(p.id, []),
        })
    return out


@router.get("/advisory/global/packages/{pkg_id}/timelines/{tl_id}/practices", response_model=list[PracticeWithElementsOut])
async def list_global_practices(
    pkg_id: str, tl_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Practice index with elements + resolved English labels (Batch 32).
    The SA portal expands a Practice row inline to show element values
    without a second fetch."""
    practices = (await db.execute(
        select(Practice).where(Practice.timeline_id == tl_id).order_by(Practice.display_order)
    )).scalars().all()
    return await _attach_elements_with_labels(db, practices)


async def _create_practice_at_global_timeline(
    db: AsyncSession, *,
    timeline_id: str, request,
) -> Practice:
    """Pipe-agnostic atomic Practice create. Caller pre-validates the
    Timeline belongs to a Global parent. Body shape accepted: any
    Pydantic model exposing the Practice fields + `elements`. Used
    by CCA Package + CHA-PG Global endpoints (Batch 39P-e,
    2026-05-16). Validators (`assert_l2_elements_valid`,
    `_assert_interval_fits_timeline`, `_validate_brand_lock`) are
    identical across pipes per UCAT."""
    if not request.l2_type:
        raise HTTPException(status_code=422, detail={
            "code": "l2_type_required",
            "message": "L2 type is required when creating a Practice.",
        })
    try:
        await assert_l2_elements_valid(
            db,
            l2_type=request.l2_type,
            elements=request.elements,
            is_special_input=request.is_special_input,
            frequency_days=request.frequency_days,
        )
    except L2ElementValidationError as e:
        _raise_l2_element_validation(e)
    await _assert_interval_fits_timeline(
        db, l2_type=request.l2_type,
        elements=request.elements, timeline_id=timeline_id,
    )
    _validate_brand_lock(request)

    practice = Practice(
        timeline_id=timeline_id,
        l0_type=request.l0_type,
        l1_type=request.l1_type,
        l2_type=request.l2_type,
        display_order=request.display_order,
        is_special_input=request.is_special_input,
        is_brand_locked=getattr(request, 'is_brand_locked', False),
        frequency_days=request.frequency_days,
    )
    db.add(practice)
    await db.flush()
    for idx, elem in enumerate(request.elements):
        data = elem.model_dump()
        data["display_order"] = idx
        db.add(Element(practice_id=practice.id, **data))
    await db.commit()
    await db.refresh(practice)
    return practice


async def _update_practice_at_global_timeline(
    db: AsyncSession, *,
    timeline_id: str, practice_id: str, request,
) -> Practice:
    """Pipe-agnostic atomic Practice update. Caller pre-validates the
    Timeline belongs to a Global parent. Same validators as create.
    Replaces the Element set in one transaction; Practice.id stays
    stable so downstream refs (relation_id) survive."""
    if not request.l2_type:
        raise HTTPException(status_code=422, detail={
            "code": "l2_type_required",
            "message": "L2 type is required when updating a Practice.",
        })
    practice = (await db.execute(
        select(Practice).where(
            Practice.id == practice_id, Practice.timeline_id == timeline_id,
        )
    )).scalar_one_or_none()
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")

    try:
        await assert_l2_elements_valid(
            db,
            l2_type=request.l2_type,
            elements=request.elements,
            is_special_input=request.is_special_input,
            frequency_days=request.frequency_days,
        )
    except L2ElementValidationError as e:
        _raise_l2_element_validation(e)
    await _assert_interval_fits_timeline(
        db, l2_type=request.l2_type,
        elements=request.elements, timeline_id=timeline_id,
    )
    _validate_brand_lock(request)

    practice.l0_type = request.l0_type
    practice.l1_type = request.l1_type
    practice.l2_type = request.l2_type
    practice.display_order = request.display_order
    practice.is_special_input = request.is_special_input
    practice.is_brand_locked = getattr(request, 'is_brand_locked', False)
    practice.frequency_days = request.frequency_days

    await db.execute(
        Element.__table__.delete().where(Element.practice_id == practice_id)
    )
    await db.flush()
    for idx, elem in enumerate(request.elements):
        data = elem.model_dump()
        data["display_order"] = idx
        db.add(Element(practice_id=practice_id, **data))
    await db.commit()
    await db.refresh(practice)
    return practice


@router.post("/advisory/global/packages/{pkg_id}/timelines/{tl_id}/practices", response_model=PracticeOut, status_code=201)
async def create_global_practice(
    pkg_id: str, tl_id: str,
    request: PracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a Practice on a Global CCA Timeline. Body shared with
    CHA-PG sibling via `_create_practice_at_global_timeline`."""
    await _assert_sa_or_cm(db, current_user)
    return await _create_practice_at_global_timeline(
        db, timeline_id=tl_id, request=request,
    )


@router.put("/advisory/global/packages/{pkg_id}/timelines/{tl_id}/practices/{practice_id}", response_model=PracticeOut)
async def update_global_practice(
    pkg_id: str, tl_id: str, practice_id: str,
    request: PracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Atomic Practice update on a Global CCA Timeline. Body shared
    with CHA-PG sibling via `_update_practice_at_global_timeline`."""
    await _assert_sa_or_cm(db, current_user)
    return await _update_practice_at_global_timeline(
        db, timeline_id=tl_id, practice_id=practice_id, request=request,
    )


async def _delete_practice_at_global_timeline(
    db: AsyncSession, *, timeline_id: str, practice_id: str,
) -> None:
    """Pipe-agnostic Practice delete. Explicitly removes child Elements
    first (the FK isn't `ON DELETE CASCADE`; `db.delete(practice)`
    alone would orphan the Elements and trip the NOT NULL on
    `element.practice_id`)."""
    practice = (await db.execute(
        select(Practice).where(
            Practice.id == practice_id, Practice.timeline_id == timeline_id,
        )
    )).scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")
    await db.execute(
        Element.__table__.delete().where(Element.practice_id == practice_id)
    )
    await db.delete(practice)
    await db.commit()


@router.delete("/advisory/global/packages/{pkg_id}/timelines/{tl_id}/practices/{practice_id}", status_code=204)
async def delete_global_practice(
    pkg_id: str, tl_id: str, practice_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a Practice on a Global CCA Timeline. Pipe-agnostic body
    via `_delete_practice_at_global_timeline`."""
    await _assert_sa_or_cm(db, current_user)
    await _delete_practice_at_global_timeline(
        db, timeline_id=tl_id, practice_id=practice_id,
    )


# ── CHA global-Practice per-element CRUD (Round 2) ─────────────────────────

@router.post(
    "/advisory/global/packages/{pkg_id}/timelines/{tl_id}/practices/{practice_id}/elements",
    status_code=201,
)
async def add_global_cca_element(
    pkg_id: str, tl_id: str, practice_id: str,
    body: ElementIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_sa_or_cm(db, current_user)
    practice = await _load_cca_practice(db, timeline_id=tl_id, practice_id=practice_id)
    new = await _add_practice_element(
        db, practice=practice, element_model=Element, body=body,
    )
    return _element_row_to_out(new)


@router.put(
    "/advisory/global/packages/{pkg_id}/timelines/{tl_id}/practices/{practice_id}/elements/{element_id}",
)
async def update_global_cca_element(
    pkg_id: str, tl_id: str, practice_id: str, element_id: str,
    body: ElementIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_sa_or_cm(db, current_user)
    practice = await _load_cca_practice(db, timeline_id=tl_id, practice_id=practice_id)
    updated = await _update_practice_element(
        db, practice=practice, element_model=Element,
        element_id=element_id, body=body,
    )
    return _element_row_to_out(updated)


@router.delete(
    "/advisory/global/packages/{pkg_id}/timelines/{tl_id}/practices/{practice_id}/elements/{element_id}",
    status_code=204,
)
async def delete_global_cca_element(
    pkg_id: str, tl_id: str, practice_id: str, element_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_sa_or_cm(db, current_user)
    practice = await _load_cca_practice(db, timeline_id=tl_id, practice_id=practice_id)
    await _delete_practice_element(
        db, practice=practice, element_model=Element, element_id=element_id,
    )


async def _deep_copy_advisory_content(
    db: AsyncSession, *,
    src_parent_attr: str, src_parent_id: str,
    dst_parent_attr: str, dst_parent_id: str,
) -> None:
    """Pipe-agnostic deep-copy of the UCAT Timeline → Practice →
    Element → Relation → ConditionalQuestion sub-tree from one parent
    advisory row to another.

    `*_parent_attr` is the name of the Timeline's parent FK column —
    `'package_id'`, `'pg_recommendation_id'`, `'sp_recommendation_id'`,
    or `'standard_response_id'`. Source and destination usually use
    the same attr (same pipe) but the API supports cross-pipe copies
    just in case.

    Does NOT touch parent-level fields (Package name / duration /
    locations / authors / PVs; PG-Recommendation problem_group_cosh_id
    / area_or_plant) — caller sets those on the destination row before
    invoking. Commits are not issued here; the caller controls the
    transaction boundary.

    Batch 39P-d (2026-05-16) — generalised from `_deep_copy_package_
    content` so PG / SP / QA clone-to-draft flows can reuse the
    same machinery the CCA clone uses (Brand Lock, frequency,
    common_name, relations + bindings, CQs + bindings all flow
    through).
    """
    practice_id_map: dict[str, str] = {}        # src_practice_id → new_practice_id
    timeline_id_map: dict[str, str] = {}        # src_timeline_id → new_timeline_id
    relation_id_map: dict[str, str] = {}        # src_relation_id → new_relation_id
    cq_id_map: dict[str, str] = {}              # src_cq_id → new_cq_id
    # src_practice → (src_relation_id, role) so we can wire the new
    # practice rows to the new relation rows after both have flushed.
    practice_relation_links: dict[str, tuple[str, str]] = {}

    src_attr_col = getattr(Timeline, src_parent_attr)
    tl_result = await db.execute(
        select(Timeline).where(src_attr_col == src_parent_id)
        .order_by(Timeline.display_order)
    )
    src_timelines = tl_result.scalars().all()
    for src_tl in src_timelines:
        new_tl = Timeline(
            name=src_tl.name,
            from_type=src_tl.from_type,
            from_value=src_tl.from_value,
            to_value=src_tl.to_value,
            display_order=src_tl.display_order,
            status=src_tl.status,
        )
        setattr(new_tl, dst_parent_attr, dst_parent_id)
        db.add(new_tl)
        await db.flush()
        timeline_id_map[src_tl.id] = new_tl.id

        p_result = await db.execute(
            select(Practice).where(Practice.timeline_id == src_tl.id)
            .order_by(Practice.display_order)
        )
        for src_p in p_result.scalars().all():
            new_p = Practice(
                timeline_id=new_tl.id,
                l0_type=src_p.l0_type,
                l1_type=src_p.l1_type,
                l2_type=src_p.l2_type,
                display_order=src_p.display_order,
                is_special_input=src_p.is_special_input,
                is_brand_locked=src_p.is_brand_locked,
                common_name_cosh_id=src_p.common_name_cosh_id,
                frequency_days=src_p.frequency_days,
                # relation_id + relation_role wired in a second pass
                # once Relations have been cloned.
            )
            db.add(new_p)
            await db.flush()
            practice_id_map[src_p.id] = new_p.id
            if src_p.relation_id and src_p.relation_role:
                practice_relation_links[src_p.id] = (src_p.relation_id, src_p.relation_role)

            el_result = await db.execute(
                select(Element).where(Element.practice_id == src_p.id)
                .order_by(Element.display_order)
            )
            for src_el in el_result.scalars().all():
                db.add(Element(
                    practice_id=new_p.id,
                    element_type=src_el.element_type,
                    cosh_ref=src_el.cosh_ref,
                    value=src_el.value,
                    unit_cosh_id=src_el.unit_cosh_id,
                    display_order=src_el.display_order,
                ))

    # Relations — clone, then wire the practice rows to their new
    # Relation via relation_id + relation_role.
    for src_tl in src_timelines:
        rels = (await db.execute(
            select(Relation).where(Relation.timeline_id == src_tl.id)
        )).scalars().all()
        for src_rel in rels:
            new_rel = Relation(
                timeline_id=timeline_id_map[src_tl.id],
                relation_type=src_rel.relation_type,
                expression=src_rel.expression,
            )
            db.add(new_rel)
            await db.flush()
            relation_id_map[src_rel.id] = new_rel.id
    if practice_relation_links:
        # Second-pass: now that Relations exist on the destination,
        # walk the new Practice rows and set their relation_id / role.
        for src_pid, (src_rel_id, role) in practice_relation_links.items():
            new_pid = practice_id_map.get(src_pid)
            new_rel_id = relation_id_map.get(src_rel_id)
            if not (new_pid and new_rel_id):
                continue
            new_p = (await db.execute(
                select(Practice).where(Practice.id == new_pid)
            )).scalar_one()
            new_p.relation_id = new_rel_id
            new_p.relation_role = role

    # Conditional Questions — clone per timeline.
    for src_tl in src_timelines:
        cqs = (await db.execute(
            select(ConditionalQuestion).where(
                ConditionalQuestion.timeline_id == src_tl.id
            ).order_by(ConditionalQuestion.display_order)
        )).scalars().all()
        for src_cq in cqs:
            new_cq = ConditionalQuestion(
                timeline_id=timeline_id_map[src_tl.id],
                question_text=src_cq.question_text,
                display_order=src_cq.display_order,
            )
            db.add(new_cq)
            await db.flush()
            cq_id_map[src_cq.id] = new_cq.id

    # PracticeConditional + RelationConditional bindings.
    if cq_id_map:
        pcs = (await db.execute(
            select(PracticeConditional).where(
                PracticeConditional.question_id.in_(list(cq_id_map.keys()))
            )
        )).scalars().all()
        for pc in pcs:
            new_p = practice_id_map.get(pc.practice_id)
            new_cq = cq_id_map.get(pc.question_id)
            if new_p and new_cq:
                db.add(PracticeConditional(
                    practice_id=new_p, question_id=new_cq, answer=pc.answer,
                ))
        rcs = (await db.execute(
            select(RelationConditional).where(
                RelationConditional.question_id.in_(list(cq_id_map.keys()))
            )
        )).scalars().all()
        for rc in rcs:
            new_r = relation_id_map.get(rc.relation_id)
            new_cq = cq_id_map.get(rc.question_id)
            if new_r and new_cq:
                db.add(RelationConditional(
                    relation_id=new_r, question_id=new_cq, answer=rc.answer,
                ))


async def _deep_copy_package_content(
    db: AsyncSession, src_id: str, dst_id: str,
) -> None:
    """Backward-compatible CCA shim over `_deep_copy_advisory_content`
    (Batch 39P-d, 2026-05-16). Existing callers (push, pull, SE
    clone-to-draft, SE rollback-publish) keep their two-arg shape."""
    await _deep_copy_advisory_content(
        db,
        src_parent_attr="package_id", src_parent_id=src_id,
        dst_parent_attr="package_id", dst_parent_id=dst_id,
    )


async def _assert_client_user_can_edit(
    db: AsyncSession, user_id: str, client_id: str,
) -> None:
    """Authorisation gate for SE-side actions on a Client's local
    Packages (pull from Global; future clone-to-draft etc).

    Today's eligible roles: any ACTIVE ClientUser whose role can
    edit advisory content for the client. V1 is permissive — every
    ClientUser status=ACTIVE qualifies, since onboarding is small
    and role-level granularity comes in V2 alongside the wider
    `_require_client_role` audit.

    Raises 403 with stable code `client_user_required`.
    """
    from app.modules.clients.models import ClientUser
    from app.modules.platform.models import StatusEnum

    # Existence check, not a single-row constraint. The DB allows
    # multiple ClientUser rows per (user, client) keyed on role, so
    # `.first()` is the correct primitive here.
    cu = (await db.execute(
        select(ClientUser.id).where(
            ClientUser.user_id == user_id,
            ClientUser.client_id == client_id,
            ClientUser.status == StatusEnum.ACTIVE,
        ).limit(1)
    )).scalar_one_or_none()
    if cu is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "client_user_required",
                "message": (
                    "Only an active staff member of this client may "
                    "pull a new version of a Global Package."
                ),
            },
        )


async def _load_global_active_or_422(
    db: AsyncSession, pkg_id: str,
) -> Package:
    """Common prologue for push + pull: fetch Global Package with
    client_id=NULL and status=ACTIVE. 404 if not Global; 422 if not
    ACTIVE (DRAFT = CM-WIP; INACTIVE = superseded)."""
    src = (await db.execute(
        select(Package).where(
            Package.id == pkg_id, Package.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if not src:
        raise HTTPException(status_code=404, detail="Global package not found")
    if src.status != PackageStatus.ACTIVE:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "global_package_not_published",
                "message": (
                    "This global package has not been published (or has "
                    "been deactivated). Ask the CM to publish it before "
                    "pushing or pulling."
                ),
                "current_status": (
                    src.status.value if hasattr(src.status, "value") else src.status
                ),
            },
        )
    return src


@router.post("/client/{client_id}/packages/{pkg_id}/push", response_model=PackageOut, status_code=201)
async def push_global_package(
    client_id: str,
    pkg_id: str,
    request: PackagePushRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """**CM push — once per Global lineage, per client (Batch 39N-a,
    2026-05-16).** Ram (the CM) picks the client-specific framing at
    push time — Name, Description, Start Date Label, Locations,
    P-V signature, Authors — and deep-copies the Global's content
    (timelines, practices, elements, relations, CQs) into the
    client's scope as a DRAFT.

    Auth:
      - 403 `cm_assignment_required` — caller has no active EDIT
        CMClientAssignment for `client_id`.

    Source gates:
      - 404 — Global package not found.
      - 422 `global_package_not_published` — Global is DRAFT/INACTIVE.

    Once-per-package gate (lineage-aware):
      - 409 `package_already_pushed` — any row in the Global lineage
        (same client_id=NULL, crop_cosh_id, name) has already been
        pushed to this client. Subsequent updates are made by the CM
        directly within the client's scope (V1 model — Pull is
        deferred to V1.x).

    Field validations (each surfaces a structured 422):
      - `duplicate_package_name` — name already in use within
        (client, crop) on a DRAFT or ACTIVE row.
      - `location_not_onboarded` — at least one (state, district)
        is not on the client's ACTIVE ClientLocation list.
      - `custom_pv_not_allowed_on_push` — at least one parameter is
        not a catalogue row (client_id IS NULL).
      - `invalid_pv_assignment` — variable doesn't belong to its
        parameter, or row doesn't exist.
      - `pv_crop_mismatch` — parameter belongs to a different crop
        than the Global.
      - `invalid_author` — at least one user_id is not an ACTIVE
        SUBJECT_EXPERT ClientUser on this client.
      - `duplicate_author` — same user_id appears twice in
        `author_ids`.
      - PV uniqueness §4.2 — `pv_signature_conflict` (mapped via
        `_raise_pv_conflict`) names the sibling Package + shared
        districts so Ram can fix and retry.
    """
    await _assert_cm_can_edit_client(db, current_user.id, client_id)
    src = await _load_global_active_or_422(db, pkg_id)

    # Lineage-aware once-per-package gate. The Global lineage is the
    # set of rows sharing (client_id=NULL, crop_cosh_id, name) — same
    # lineage key the SA portal uses for Global versioning (Batch 39L-b).
    lineage_ids = list((await db.execute(
        select(Package.id).where(
            Package.client_id == None,  # noqa: E711
            Package.crop_cosh_id == src.crop_cosh_id,
            Package.name == src.name,
        )
    )).scalars().all())
    existing = (await db.execute(
        select(Package).where(
            Package.client_id == client_id,
            Package.parent_global_id.in_(lineage_ids),
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        tl_count = (await db.execute(
            select(func.count()).select_from(Timeline).where(
                Timeline.package_id == existing.id,
            )
        )).scalar() or 0
        raise HTTPException(
            status_code=409,
            detail={
                "code": "package_already_pushed",
                "message": (
                    "This Global Package has already been pushed to this "
                    "client. First contact happens once per client; "
                    "subsequent updates are made by the CM directly on "
                    "the client's local Package."
                ),
                "existing": {
                    "package_id": existing.id,
                    "timeline_count": tl_count,
                },
            },
        )

    # Name uniqueness within (client, crop) on DRAFT/ACTIVE rows.
    dupe_name = (await db.execute(
        select(Package).where(
            Package.client_id == client_id,
            Package.crop_cosh_id == src.crop_cosh_id,
            Package.name == request.name,
            Package.status.in_([PackageStatus.DRAFT, PackageStatus.ACTIVE]),
        ).limit(1)
    )).scalar_one_or_none()
    if dupe_name:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "duplicate_package_name",
                "message": (
                    f"A {dupe_name.status.value if hasattr(dupe_name.status, 'value') else dupe_name.status} "
                    f"package named '{request.name}' already exists for "
                    "this crop on this client. Pick a different name."
                ),
                "existing_package_id": dupe_name.id,
            },
        )

    # Locations must be ACTIVE onboarded locations on this client.
    requested_locs = [
        (loc.state_cosh_id, loc.district_cosh_id) for loc in request.locations
    ]
    onboarded = set(
        (loc.state_cosh_id, loc.district_cosh_id)
        for loc in (await db.execute(
            select(ClientLocation).where(
                ClientLocation.client_id == client_id,
                ClientLocation.status == StatusEnum.ACTIVE,
            )
        )).scalars().all()
    )
    bad_locs = sorted(set(requested_locs) - onboarded)
    if bad_locs:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "location_not_onboarded",
                "message": (
                    "One or more locations are not on this client's "
                    "onboarded list. Ask the client admin to add the "
                    "location(s) before pushing this package."
                ),
                "invalid_locations": [
                    {"state_cosh_id": s, "district_cosh_id": d}
                    for s, d in bad_locs
                ],
            },
        )

    # PV assignments: catalogue-only (client_id IS NULL); variable
    # belongs to its parameter; parameter belongs to this crop.
    parameter_ids = [pv.parameter_id for pv in request.pv_assignments]
    variable_ids = [pv.variable_id for pv in request.pv_assignments]
    catalogue_params = {
        p.id: p for p in (await db.execute(
            select(Parameter).where(
                Parameter.id.in_(parameter_ids),
                Parameter.client_id == None,  # noqa: E711
            )
        )).scalars().all()
    }
    custom_params = sorted(set(parameter_ids) - catalogue_params.keys())
    if custom_params:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "custom_pv_not_allowed_on_push",
                "message": (
                    "Custom parameters cannot be used at push time. Pick "
                    "Parameters from the global catalogue; the client SE "
                    "can add custom P-V later from their own scope."
                ),
                "invalid_parameter_ids": custom_params,
            },
        )
    cross_crop = sorted(
        pid for pid, p in catalogue_params.items()
        if p.crop_cosh_id != src.crop_cosh_id
    )
    if cross_crop:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "pv_crop_mismatch",
                "message": (
                    "One or more Parameters belong to a different crop "
                    "than this Global Package."
                ),
                "invalid_parameter_ids": cross_crop,
            },
        )
    catalogue_variables = {
        (v.parameter_id, v.id)
        for v in (await db.execute(
            select(Variable).where(Variable.id.in_(variable_ids))
        )).scalars().all()
    }
    bad_pvs = sorted(
        {(pv.parameter_id, pv.variable_id) for pv in request.pv_assignments}
        - catalogue_variables
    )
    if bad_pvs:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_pv_assignment",
                "message": (
                    "One or more variable→parameter pairings are invalid "
                    "(variable doesn't belong to its parameter, or row "
                    "doesn't exist)."
                ),
                "invalid_pairs": [
                    {"parameter_id": p, "variable_id": v} for p, v in bad_pvs
                ],
            },
        )

    # Authors: ACTIVE SUBJECT_EXPERT ClientUsers on this client; no
    # duplicates in the request.
    if len(set(request.author_ids)) != len(request.author_ids):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "duplicate_author",
                "message": (
                    "An expert cannot be listed twice as an author of "
                    "the same Package."
                ),
            },
        )
    valid_se_ids = set((await db.execute(
        select(ClientUser.user_id).where(
            ClientUser.client_id == client_id,
            ClientUser.user_id.in_(request.author_ids),
            ClientUser.role == ClientUserRole.SUBJECT_EXPERT,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalars().all())
    invalid_authors = sorted(set(request.author_ids) - valid_se_ids)
    if invalid_authors:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_author",
                "message": (
                    "One or more authors are not ACTIVE Subject Experts "
                    "of this client."
                ),
                "invalid_user_ids": invalid_authors,
            },
        )

    # All input-side validations passed. Build the Local DRAFT.
    copy = Package(
        client_id=client_id,
        parent_global_id=src.id,
        crop_cosh_id=src.crop_cosh_id,
        name=request.name,
        package_type=src.package_type,
        duration_days=src.duration_days,
        start_date_label_cosh_id=request.start_date_label_cosh_id,
        description=request.description,
        created_by=current_user.id,
        status=PackageStatus.DRAFT,
        created_via=PackageCreatedVia.CM_PUSH,
    )
    db.add(copy)
    await db.flush()

    for loc in request.locations:
        db.add(PackageLocation(
            package_id=copy.id,
            state_cosh_id=loc.state_cosh_id,
            district_cosh_id=loc.district_cosh_id,
        ))
    for pv in request.pv_assignments:
        db.add(PackageVariable(
            package_id=copy.id,
            parameter_id=pv.parameter_id,
            variable_id=pv.variable_id,
        ))
    for i, uid in enumerate(request.author_ids):
        db.add(PackageAuthor(
            package_id=copy.id,
            user_id=uid,
            display_order=i,
        ))
    await db.flush()

    # §4.2 district-overlap PV uniqueness check against existing
    # client-side siblings. Naming the sibling + shared districts in
    # the response gives Ram a precise fix path: change PVs, change
    # locations, or both.
    try:
        await assert_pv_unique_for_package(db, package=copy)
    except PVConflictError as e:
        await db.rollback()
        _raise_pv_conflict(e)

    # Deep-copy content (timelines, practices, elements, relations,
    # CQs, brand-lock — extended in Batch 39L-b).
    await _deep_copy_package_content(db, src.id, copy.id)

    await db.commit()
    await db.refresh(copy)
    return copy


@router.post("/client/{client_id}/packages/{pkg_id}/pull", response_model=PackageOut, status_code=201)
async def pull_global_package(
    client_id: str,
    pkg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """**Deferred to V1.x (Batch 39N-a, 2026-05-16).** Per the user's
    V1 decision, pull is not shipping: Ram (the CM) has direct EDIT
    rights on the client's Local Package and updates content there
    directly, rather than the SE pulling a fresh DRAFT from Global.

    The endpoint stays registered so existing clients see a clear 501
    code (`pull_deferred`) rather than a 404. Re-enable when real-world
    feedback shapes the refresh model.
    """
    raise HTTPException(
        status_code=501,
        detail={
            "code": "pull_deferred",
            "message": (
                "Pull from Global is deferred in V1. Ask the CM to make "
                "any content updates directly on the client's local "
                "Package — they have EDIT rights on the client's scope."
            ),
        },
    )


async def _deep_copy_package_metadata(
    db: AsyncSession, src_id: str, dst_id: str,
) -> None:
    """Copy locations + authors + package_variables. Used by
    clone-to-draft and rollback-publish where the destination row
    must be a fully-formed Local Package (locations/authors are
    required by the publish-readiness gate). Push/pull deliberately
    skip this since they originate from Global (no client-scoped
    metadata to copy)."""
    for loc in (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == src_id)
    )).scalars().all():
        db.add(PackageLocation(
            package_id=dst_id,
            state_cosh_id=loc.state_cosh_id,
            district_cosh_id=loc.district_cosh_id,
        ))
    for a in (await db.execute(
        select(PackageAuthor).where(PackageAuthor.package_id == src_id)
    )).scalars().all():
        db.add(PackageAuthor(
            package_id=dst_id, user_id=a.user_id,
            display_order=a.display_order,
        ))
    for pv in (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id == src_id)
    )).scalars().all():
        db.add(PackageVariable(
            package_id=dst_id, parameter_id=pv.parameter_id,
            variable_id=pv.variable_id,
        ))
    await db.flush()


async def _next_lineage_version(
    db: AsyncSession, *, client_id: str, crop_cosh_id: str, name: str,
) -> int:
    """Return max(version) + 1 across rows in the lineage that
    have been published at least once (`published_at IS NOT NULL`).

    Filtering on `published_at` rather than status lets fresh
    DRAFTs / never-published rows out of the calculation regardless
    of their `version` default. Old tests that seed
    `status=ACTIVE, published_at=NULL` rows (a quirk of the
    factory) continue to land at v=1 on their first real publish.

    Cases this gets right:
      • Fresh DRAFT first publish — no published siblings → 0+1=1.
      • In-place republish (legacy BL-13) — pkg.published_at set
        on first publish; second publish sees pkg in lineage at
        v=1 → v=2.
      • INACTIVE republish — pkg.published_at set, v=3 → v=4.
      • Multi-row v_n publish — lineage of n-1 published rows
        (predecessor + history) → v=n.
      • Rollback-publish — same lineage; new row not yet in DB.
    """
    max_v = (await db.execute(
        select(func.max(Package.version)).where(
            Package.client_id == client_id,
            Package.crop_cosh_id == crop_cosh_id,
            Package.name == name,
            Package.published_at.is_not(None),
        )
    )).scalar() or 0
    return int(max_v) + 1


@router.post(
    "/client/{client_id}/packages/{package_id}/clone-to-draft",
    response_model=PackageOut, status_code=201,
)
async def clone_to_draft(
    client_id: str,
    package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """**SE starts a new edit cycle**. Takes the current ACTIVE row
    in a lineage and creates a new DRAFT with deep-copied content
    + locations + authors + PVs. The DRAFT is the SE's working
    surface for the next publish.

    Source must be ACTIVE and Local. Historical INACTIVE rows are
    handled by `rollback-publish` (creates a PUBLISHED row directly,
    no DRAFT step) per the user's locked model.

    Single-DRAFT invariant: any existing DRAFT in the same lineage
    (client + crop + name) is auto-flipped to INACTIVE.
    """
    await _assert_client_user_can_edit(db, current_user.id, client_id)
    src = await _get_package(db, package_id, client_id)
    if src.status != PackageStatus.ACTIVE:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "clone_source_not_active",
                "message": (
                    "clone-to-draft requires the current ACTIVE row of "
                    "the lineage as the source. To republish historical "
                    "content, use rollback-publish."
                ),
                "current_status": (
                    src.status.value if hasattr(src.status, "value")
                    else src.status
                ),
            },
        )

    existing_draft = (await db.execute(
        select(Package).where(
            Package.client_id == client_id,
            Package.crop_cosh_id == src.crop_cosh_id,
            Package.name == src.name,
            Package.status == PackageStatus.DRAFT,
        )
    )).scalar_one_or_none()
    if existing_draft:
        existing_draft.status = PackageStatus.INACTIVE

    new_draft = Package(
        client_id=client_id,
        parent_global_id=src.parent_global_id,
        crop_cosh_id=src.crop_cosh_id,
        name=src.name,
        package_type=src.package_type,
        duration_days=src.duration_days,
        start_date_label_cosh_id=src.start_date_label_cosh_id,
        description=src.description,
        created_by=current_user.id,
        status=PackageStatus.DRAFT,
        created_via=PackageCreatedVia.SE_EDIT_DRAFT,
    )
    db.add(new_draft)
    await db.flush()
    await _deep_copy_package_content(db, src.id, new_draft.id)
    await _deep_copy_package_metadata(db, src.id, new_draft.id)
    await db.commit()
    await db.refresh(new_draft)
    return new_draft


@router.post(
    "/client/{client_id}/packages/{package_id}/rollback-publish",
    response_model=PackageOut, status_code=201,
)
async def rollback_publish(
    client_id: str,
    package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """**SE republishes a historical row's content as a new ACTIVE
    version**. Locked 2026-05-11:
        > "If he goes back to his older versions and publishes one
        >  of them, then a new version is created. The new draft
        >  becomes inactive (just like the others)."

    Demotes the current ACTIVE in the lineage to INACTIVE, discards
    any in-flight DRAFT (flips to INACTIVE), and creates a new
    ACTIVE row with `created_via=SE_ROLLBACK_PUBLISH` and
    `source_version_id` pointing at the historical source.
    Subscriptions migrate to the new row (BL-13 farmer-side spirit
    preserved).

    Source can be any Local row at this client (PUBLISHED ACTIVE or
    INACTIVE history). Skips the publish-readiness gate since the
    source content was already validated when it was first
    published.
    """
    await _assert_client_user_can_edit(db, current_user.id, client_id)
    src = await _get_package(db, package_id, client_id)
    if src.client_id is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "rollback_source_must_be_local",
                "message": "Cannot rollback-publish a Global Package.",
            },
        )

    from app.modules.subscriptions.models import Subscription

    prior_actives = (await db.execute(
        select(Package).where(
            Package.client_id == client_id,
            Package.crop_cosh_id == src.crop_cosh_id,
            Package.name == src.name,
            Package.status == PackageStatus.ACTIVE,
        )
    )).scalars().all()
    drafts = (await db.execute(
        select(Package).where(
            Package.client_id == client_id,
            Package.crop_cosh_id == src.crop_cosh_id,
            Package.name == src.name,
            Package.status == PackageStatus.DRAFT,
        )
    )).scalars().all()
    for prior in prior_actives:
        prior.status = PackageStatus.INACTIVE
    for d in drafts:
        d.status = PackageStatus.INACTIVE

    new_version = await _next_lineage_version(
        db, client_id=client_id,
        crop_cosh_id=src.crop_cosh_id, name=src.name,
    )

    new_active = Package(
        client_id=client_id,
        parent_global_id=src.parent_global_id,
        crop_cosh_id=src.crop_cosh_id,
        name=src.name,
        package_type=src.package_type,
        duration_days=src.duration_days,
        start_date_label_cosh_id=src.start_date_label_cosh_id,
        description=src.description,
        created_by=current_user.id,
        status=PackageStatus.ACTIVE,
        version=new_version,
        published_at=datetime.now(timezone.utc),
        published_by=current_user.id,
        created_via=PackageCreatedVia.SE_ROLLBACK_PUBLISH,
        source_version_id=src.id,
    )
    db.add(new_active)
    await db.flush()
    await _deep_copy_package_content(db, src.id, new_active.id)
    await _deep_copy_package_metadata(db, src.id, new_active.id)

    for prior in prior_actives:
        await db.execute(
            update(Subscription)
            .where(Subscription.package_id == prior.id)
            .values(package_id=new_active.id)
        )

    await db.commit()
    await db.refresh(new_active)
    return new_active


# ── Global PG Recommendations ──────────────────────────────────────────────────

@router.get("/advisory/global/problem-groups")
async def list_global_problem_groups(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA-portal helper — Problem Groups a CM can author Global
    recommendations against. Reads from Cosh's `problem_groups` Core
    (Batch 39Q-frontend, 2026-05-16) — the legacy hardcoded list in
    `app/services/cha_problem_groups.py` still backs the CA-portal
    helpers but the SA picker now ties into the same data that
    drives `/diagnosis/pg-crops` so PGRecs created here can resolve
    their applicable-crop chips against `sp_pg_crops` Connect.

    Returns `[{cosh_id, name_en, status}]` sorted by `name_en`.
    Only `active` Core items surface."""
    from app.services.cosh_constants import COSH_PROBLEM_GROUPS_CORE
    rows = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == COSH_PROBLEM_GROUPS_CORE,
            CoshCoreItem.status == "active",
        )
    )).scalars().all()
    items = []
    for r in rows:
        t = r.translations or {}
        items.append({
            "cosh_id": r.cosh_id,
            "name_en": t.get("en") or t.get("English") or r.cosh_id,
            "status": "active",
        })
    items.sort(key=lambda x: x["name_en"].casefold())
    return items


@router.get("/advisory/global/pg-recommendations", response_model=list[PGRecommendationOut])
async def list_global_pg(
    include_drafts: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Global PG recommendations visible for import into client scope.
    Defaults to ACTIVE only — DRAFT and INACTIVE rows are hidden from
    CA-portal SEs. CMs can pass `include_drafts=true` to see
    everything in their own admin views."""
    q = select(PGRecommendation).where(PGRecommendation.client_id == None)  # noqa: E711
    if not include_drafts:
        q = q.where(PGRecommendation.status == "ACTIVE")
    q = q.order_by(PGRecommendation.created_at.desc())
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/advisory/global/pg-recommendations", response_model=PGRecommendationOut, status_code=201)
async def create_global_pg(
    request: PGRecommendationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_sa_or_cm(db, current_user)
    pg = PGRecommendation(
        problem_group_cosh_id=request.problem_group_cosh_id,
        client_id=None,
        area_or_plant=request.area_or_plant,
    )
    db.add(pg)
    await db.commit()
    await db.refresh(pg)
    return pg


@router.get("/advisory/global/pg-recommendations/{pg_id}", response_model=PGRecommendationOut)
async def get_global_pg(
    pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pg = (await db.execute(
        select(PGRecommendation).where(PGRecommendation.id == pg_id, PGRecommendation.client_id == None)  # noqa: E711
    )).scalar_one_or_none()
    if not pg:
        raise HTTPException(status_code=404, detail="Global PG recommendation not found")
    return pg


@router.get("/advisory/global/pg-recommendations/{pg_id}/timelines")
async def list_global_pg_timelines(
    pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA-portal helper (Batch 39P-a, 2026-05-16) — list Timelines on
    a Global PG recommendation with their practices nested. Mirrors
    the CA-side `/client/{cid}/pg-recommendations/{pg_id}/timelines`
    shape so the same front-end renderer can drive both surfaces."""
    result = await db.execute(
        select(Timeline).where(Timeline.pg_recommendation_id == pg_id)
        .order_by(Timeline.display_order, Timeline.from_value)
    )
    timelines = result.scalars().all()
    out = []
    for tl in timelines:
        p_res = await db.execute(
            select(Practice).where(Practice.timeline_id == tl.id)
            .order_by(Practice.display_order)
        )
        from_type_value = (
            tl.from_type.value if hasattr(tl.from_type, "value") else tl.from_type
        )
        out.append({
            "id": tl.id,
            "pg_recommendation_id": tl.pg_recommendation_id,
            "name": tl.name,
            "from_type": from_type_value,
            "from_value": tl.from_value,
            "to_value": tl.to_value,
            "status": tl.status,
            "practices": [
                {
                    "id": p.id,
                    "l0_type": p.l0_type.value if hasattr(p.l0_type, "value") else p.l0_type,
                    "l1_type": p.l1_type,
                    "l2_type": p.l2_type,
                    "display_order": p.display_order,
                    "is_special_input": p.is_special_input,
                }
                for p in p_res.scalars().all()
            ],
        })
    return out


@router.get(
    "/advisory/global/pg-recommendations/{pg_id}/timelines/{tl_id}/practices",
    response_model=list[PracticeWithElementsOut],
)
async def list_global_pg_practices(
    pg_id: str, tl_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Practice index for a Global CHA-PG Timeline — same shape as the
    CCA sibling at /advisory/global/packages/{pkg}/timelines/{tl}/practices.
    Returns practices with elements + server-resolved English labels so
    the SA-portal PG editor can expand a Practice row inline to show
    element values, and the Edit modal can pre-fill correctly.

    The earlier nested-in-timelines GET (line ~5599) ships practices
    WITHOUT elements; that was a bug surfaced by the user 2026-05-17.
    Frontend should call this endpoint on timeline expand + after each
    Add/Edit, mirroring the CCA pattern."""
    practices = (await db.execute(
        select(Practice).where(Practice.timeline_id == tl_id)
        .order_by(Practice.display_order)
    )).scalars().all()
    return await _attach_elements_with_labels(db, practices)


@router.post("/advisory/global/pg-recommendations/{pg_id}/timelines", status_code=201)
async def add_global_pg_timeline(
    pg_id: str,
    request: PGTimelineCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_sa_or_cm(db, current_user)
    pg = (await db.execute(
        select(PGRecommendation).where(PGRecommendation.id == pg_id)
    )).scalar_one_or_none()
    if not pg:
        raise HTTPException(status_code=404, detail="PG recommendation not found")
    tl = Timeline(
        pg_recommendation_id=pg_id,
        name=request.name,
        from_type=request.from_type,
        from_value=request.from_value,
        to_value=request.to_value,
    )
    db.add(tl)
    await db.commit()
    await db.refresh(tl)
    return tl


@router.post("/advisory/global/pg-recommendations/{pg_id}/timelines/{tl_id}/practices", status_code=201)
async def add_global_pg_practice(
    pg_id: str,
    tl_id: str,
    request: PGPracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a Practice on a Global CHA-PG Timeline. Body validator,
    Brand Lock + frequency rules, and persistence shared with the CCA
    sibling via `_create_practice_at_global_timeline` (Batch 39P-e,
    2026-05-16)."""
    await _assert_sa_or_cm(db, current_user)
    await _get_global_pg_timeline(db, pg_id, tl_id)
    return await _create_practice_at_global_timeline(
        db, timeline_id=tl_id, request=request,
    )


@router.put(
    "/advisory/global/pg-recommendations/{pg_id}/timelines/{tl_id}/practices/{practice_id}",
    response_model=PracticeOut,
)
async def update_global_pg_practice(
    pg_id: str, tl_id: str, practice_id: str,
    request: PGPracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Atomic Practice update on a Global CHA-PG Timeline. Mirror of
    the CCA sibling (Batch 39P-e, 2026-05-16)."""
    await _assert_sa_or_cm(db, current_user)
    await _get_global_pg_timeline(db, pg_id, tl_id)
    return await _update_practice_at_global_timeline(
        db, timeline_id=tl_id, practice_id=practice_id, request=request,
    )


@router.delete(
    "/advisory/global/pg-recommendations/{pg_id}/timelines/{tl_id}/practices/{practice_id}",
    status_code=204,
)
async def delete_global_pg_practice(
    pg_id: str, tl_id: str, practice_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a Practice on a Global CHA-PG Timeline. Pipe-agnostic
    body via `_delete_practice_at_global_timeline`."""
    await _assert_sa_or_cm(db, current_user)
    await _get_global_pg_timeline(db, pg_id, tl_id)
    await _delete_practice_at_global_timeline(
        db, timeline_id=tl_id, practice_id=practice_id,
    )


# ── CHA global-PG per-element CRUD (Round 2) ───────────────────────────────

async def _load_pg_practice_by_timeline(db, *, timeline_id: str, practice_id: str):
    practice = (await db.execute(
        select(Practice).where(
            Practice.id == practice_id,
            Practice.timeline_id == timeline_id,
        )
    )).scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")
    return practice


@router.post(
    "/advisory/global/pg-recommendations/{pg_id}/timelines/{tl_id}/practices/{practice_id}/elements",
    status_code=201,
)
async def add_global_pg_element(
    pg_id: str, tl_id: str, practice_id: str,
    body: ElementIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_sa_or_cm(db, current_user)
    practice = await _load_pg_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    new = await _add_practice_element(
        db, practice=practice, element_model=Element, body=body,
    )
    return _element_row_to_out(new)


@router.put(
    "/advisory/global/pg-recommendations/{pg_id}/timelines/{tl_id}/practices/{practice_id}/elements/{element_id}",
)
async def update_global_pg_element(
    pg_id: str, tl_id: str, practice_id: str, element_id: str,
    body: ElementIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_sa_or_cm(db, current_user)
    practice = await _load_pg_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    updated = await _update_practice_element(
        db, practice=practice, element_model=Element,
        element_id=element_id, body=body,
    )
    return _element_row_to_out(updated)


@router.delete(
    "/advisory/global/pg-recommendations/{pg_id}/timelines/{tl_id}/practices/{practice_id}/elements/{element_id}",
    status_code=204,
)
async def delete_global_pg_element(
    pg_id: str, tl_id: str, practice_id: str, element_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_sa_or_cm(db, current_user)
    practice = await _load_pg_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    await _delete_practice_element(
        db, practice=practice, element_model=Element, element_id=element_id,
    )


@router.delete("/advisory/global/pg-recommendations/{pg_id}/timelines/{tl_id}", status_code=204)
async def delete_global_pg_timeline(
    pg_id: str,
    tl_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_sa_or_cm(db, current_user)
    tl = (await db.execute(
        select(Timeline).where(Timeline.id == tl_id, Timeline.pg_recommendation_id == pg_id)
    )).scalar_one_or_none()
    if tl:
        await db.delete(tl)
        await db.commit()


@router.put("/advisory/global/pg-recommendations/{pg_id}", response_model=PGRecommendationOut)
async def update_global_pg(
    pg_id: str,
    request: PGRecommendationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Batch 39R — Global PG status toggle. ACTIVE ↔ INACTIVE flips
    freely; DRAFT → INACTIVE is allowed (discards a draft); DRAFT →
    ACTIVE must go through the publish endpoint (lineage migration)."""
    await _assert_sa_or_cm(db, current_user)
    pg = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.id == pg_id,
            PGRecommendation.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if not pg:
        raise HTTPException(status_code=404, detail="Global PG recommendation not found")

    if request.status is not None:
        new_status = request.status
        if new_status not in ("ACTIVE", "INACTIVE"):
            raise HTTPException(status_code=422, detail={
                "code": "invalid_status",
                "message": "status must be ACTIVE or INACTIVE.",
            })
        if pg.status == "DRAFT" and new_status == "ACTIVE":
            raise HTTPException(status_code=422, detail={
                "code": "publish_required",
                "message": "DRAFT → ACTIVE must go through the publish endpoint, not the status toggle.",
            })
        pg.status = new_status

    await db.commit()
    await db.refresh(pg)
    return pg


@router.put("/advisory/global/pg-recommendations/{pg_id}/timelines/{tl_id}")
async def update_global_pg_timeline(
    pg_id: str,
    tl_id: str,
    request: PGTimelineUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Batch 39R — Global PG Timeline status toggle + minor field
    edits (name, from/to). `from_type` stays immutable.

    Returns a plain dict (NOT response_model=PGTimelineOut) — the
    Timeline ORM has a `practices` lazy relationship that triggers
    async lazy-load on Pydantic serialization, surfacing as a 500
    after the DB commit had already succeeded. Same shape as
    add_global_pg_timeline."""
    await _assert_sa_or_cm(db, current_user)
    tl = (await db.execute(
        select(Timeline).where(
            Timeline.id == tl_id,
            Timeline.pg_recommendation_id == pg_id,
        )
    )).scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")

    update_data = request.model_dump(exclude_unset=True)
    if "status" in update_data:
        if update_data["status"] not in ("ACTIVE", "INACTIVE"):
            raise HTTPException(status_code=422, detail={
                "code": "invalid_status",
                "message": "Timeline status must be ACTIVE or INACTIVE.",
            })

    for field, value in update_data.items():
        setattr(tl, field, value)
    await db.commit()
    await db.refresh(tl)
    return {
        "id": tl.id,
        "pg_recommendation_id": tl.pg_recommendation_id,
        "name": tl.name,
        "from_type": tl.from_type,
        "from_value": tl.from_value,
        "to_value": tl.to_value,
        "status": tl.status,
    }


@router.post("/advisory/global/pg-recommendations/{pg_id}/publish")
async def publish_global_pg(
    pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Publish a Global PG Recommendation. Same content-gate rules as
    Global Package (Batch 39P-d, 2026-05-16): ≥1 Timeline, every
    Timeline ≥1 Practice, no dangling Conditional Questions.

    Returns 422 `publish_blocked` with a structured `missing: [...]`
    body so the SA portal can render one consolidated checklist."""
    await _assert_sa_or_cm(db, current_user)
    pg = (await db.execute(
        select(PGRecommendation).where(PGRecommendation.id == pg_id)
    )).scalar_one_or_none()
    if not pg:
        raise HTTPException(status_code=404, detail="PG recommendation not found")
    res = validate_publish_transition(pg.status)
    if not res.allowed:
        _raise_publish_transition(res)

    try:
        await assert_global_pg_publish_ready(db, pg_id=pg.id)
    except PublishBlockedError as e:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "publish_blocked",
                "message": "PG Recommendation is not ready for publish.",
                "missing": [
                    {"code": m.code, "message": m.message, "extra": m.extra}
                    for m in e.missing
                ],
            },
        )

    # Deactivate previous active version for same problem_group +
    # area_or_plant + client lineage (lineage-aware after Batch 39P-d).
    prev = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.problem_group_cosh_id == pg.problem_group_cosh_id,
            PGRecommendation.area_or_plant == pg.area_or_plant,
            PGRecommendation.client_id == pg.client_id,
            PGRecommendation.status == "ACTIVE",
            PGRecommendation.id != pg.id,
        )
    )).scalars().all()
    for p in prev:
        p.status = "INACTIVE"

    # PGRecommendation has no published_at; "first publish" is signalled
    # by status=DRAFT. Once status moves to ACTIVE / INACTIVE, the row
    # has been published at least once, so subsequent publishes
    # increment normally.
    pg.version = compute_publish_version(
        current_version=pg.version, was_published=pg.status != "DRAFT",
    )
    pg.status = "ACTIVE"
    await db.commit()
    await db.refresh(pg)
    return pg


# ── PG clone-to-draft + lineage (Batch 39P-d, 2026-05-16) ──────────────

@router.post("/advisory/global/pg-recommendations/{pg_id}/clone-to-draft")
async def clone_global_pg_to_draft(
    pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CM clones an ACTIVE or INACTIVE Global PG Recommendation into
    a new DRAFT. Same model as Global Package clone (Batch 39L-b):

      • DRAFT sources rejected with `clone_source_is_draft`.
      • Single-DRAFT invariant — any other DRAFT in the same lineage
        flips to INACTIVE.
      • Content deep-copied via `_deep_copy_advisory_content` so
        Brand Lock / Relations / CQs / bindings all carry across.
    """
    await _assert_sa_or_cm(db, current_user)
    src = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.id == pg_id,
            PGRecommendation.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if not src:
        raise HTTPException(status_code=404, detail="Global PG not found")
    if src.status == "DRAFT":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "clone_source_is_draft",
                "message": (
                    "This row is already a DRAFT. Edit it in place rather "
                    "than cloning."
                ),
            },
        )

    # Single-DRAFT invariant: flip any existing DRAFT in this lineage
    # (same problem_group + area_or_plant, client_id NULL) to INACTIVE.
    existing_draft = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.client_id == None,  # noqa: E711
            PGRecommendation.problem_group_cosh_id == src.problem_group_cosh_id,
            PGRecommendation.area_or_plant == src.area_or_plant,
            PGRecommendation.status == "DRAFT",
        )
    )).scalar_one_or_none()
    if existing_draft is not None:
        existing_draft.status = "INACTIVE"
        await db.flush()

    new_draft = PGRecommendation(
        problem_group_cosh_id=src.problem_group_cosh_id,
        area_or_plant=src.area_or_plant,
        client_id=None,
        parent_id=src.parent_id,  # preserve lineage anchor
        status="DRAFT",
        version=1,
    )
    db.add(new_draft)
    await db.flush()

    await _deep_copy_advisory_content(
        db,
        src_parent_attr="pg_recommendation_id", src_parent_id=src.id,
        dst_parent_attr="pg_recommendation_id", dst_parent_id=new_draft.id,
    )
    await db.commit()
    await db.refresh(new_draft)
    return new_draft


@router.get("/advisory/global/pg-recommendations/{pg_id}/lineage")
async def get_global_pg_lineage(
    pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every row in this Global PG's lineage (same problem_group +
    area_or_plant, client_id=NULL), DRAFT first then version desc.
    `is_current` flags the row matching the path's `pg_id`. Mirror
    of the Global Package lineage endpoint (Batch 39L-b)."""
    src = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.id == pg_id,
            PGRecommendation.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if not src:
        raise HTTPException(status_code=404, detail="Global PG not found")
    rows = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.client_id == None,  # noqa: E711
            PGRecommendation.problem_group_cosh_id == src.problem_group_cosh_id,
            PGRecommendation.area_or_plant == src.area_or_plant,
        )
    )).scalars().all()

    def sort_key(r: PGRecommendation) -> tuple:
        # DRAFT first, then version desc.
        is_draft = 0 if r.status == "DRAFT" else 1
        return (is_draft, -r.version)

    sorted_rows = sorted(rows, key=sort_key)
    return [
        {
            "id": r.id,
            "status": r.status,
            "version": r.version,
            "is_current": r.id == pg_id,
        }
        for r in sorted_rows
    ]


# ── Client PG Recommendations ──────────────────────────────────────────────────

@router.get("/client/{client_id}/pg-recommendations", response_model=list[PGRecommendationOut])
async def list_client_pg(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    result = await db.execute(
        select(PGRecommendation).where(PGRecommendation.client_id == client_id)
        .order_by(PGRecommendation.created_at.desc())
    )
    return result.scalars().all()


@router.post(
    "/client/{client_id}/pg-recommendations",
    response_model=PGRecommendationOut, status_code=201,
)
async def create_client_pg(
    client_id: str,
    request: PGRecommendationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a fresh client-local PG recommendation bundle. Used by the
    SE who wants to author from scratch instead of importing from
    Global. Per the bundle model (CHA hub Round 1, 2026-05-10):

    - `area_or_plant` is required at this layer ('AREA_WISE' /
      'PLANT_WISE') — a bundle without it isn't authorable.
    - `(client_id, problem_group_cosh_id, area_or_plant)` is unique:
      one bundle per (PG, side) per client. Re-creating returns 409
      with a pointer to the existing bundle.
    - `problem_group_cosh_id` is validated against the V1 hardcoded
      PG list — checked against Cosh's `problem_groups` Core
      (Batch 39R-bridge, 2026-05-17)."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    from app.services.cha_problem_groups import is_known_problem_group

    if request.area_or_plant not in ("AREA_WISE", "PLANT_WISE"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "area_or_plant_required",
                "message": (
                    "area_or_plant must be 'AREA_WISE' or 'PLANT_WISE' — "
                    "the bundle side defines which crops this recommendation "
                    "applies to."
                ),
            },
        )

    if not await is_known_problem_group(db, request.problem_group_cosh_id):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_problem_group",
                "message": (
                    "This problem_group_cosh_id is not in the supported list. "
                    "Pick from the CHA · Problems screen."
                ),
            },
        )

    existing = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.client_id == client_id,
            PGRecommendation.problem_group_cosh_id == request.problem_group_cosh_id,
            PGRecommendation.area_or_plant == request.area_or_plant,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "bundle_already_exists",
                "message": (
                    "A bundle for this Problem and side already exists. "
                    "Edit the existing bundle instead of creating a new one."
                ),
                "existing": {
                    "pg_recommendation_id": existing.id,
                    "status": existing.status,
                    "version": existing.version,
                },
            },
        )

    pg = PGRecommendation(
        problem_group_cosh_id=request.problem_group_cosh_id,
        client_id=client_id,
        area_or_plant=request.area_or_plant,
    )
    db.add(pg)
    await db.commit()
    await db.refresh(pg)
    return pg


@router.get("/client/{client_id}/pg-recommendations/{pg_id}", response_model=PGRecommendationOut)
async def get_client_pg(
    client_id: str,
    pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    pg = (await db.execute(
        select(PGRecommendation).where(PGRecommendation.id == pg_id, PGRecommendation.client_id == client_id)
    )).scalar_one_or_none()
    if not pg:
        raise HTTPException(status_code=404, detail="PG recommendation not found")
    return pg


async def _copy_pg_content_into(
    db: AsyncSession, *, src_pg: PGRecommendation, target_pg: PGRecommendation,
) -> None:
    """Deep-copy timelines / practices / elements from one PGRecommendation
    onto another. Caller is responsible for clearing existing target
    content first if overwriting. Used by both Global → Local PG import
    and any future PG content moves."""
    tl_result = await db.execute(
        select(Timeline).where(Timeline.pg_recommendation_id == src_pg.id)
    )
    for src_tl in tl_result.scalars().all():
        new_tl = Timeline(
            pg_recommendation_id=target_pg.id,
            name=src_tl.name,
            from_type=src_tl.from_type,
            from_value=src_tl.from_value,
            to_value=src_tl.to_value,
        )
        db.add(new_tl)
        await db.flush()

        p_result = await db.execute(
            select(Practice).where(Practice.timeline_id == src_tl.id)
        )
        for src_p in p_result.scalars().all():
            new_p = Practice(
                timeline_id=new_tl.id,
                l0_type=src_p.l0_type,
                l1_type=src_p.l1_type,
                l2_type=src_p.l2_type,
                display_order=src_p.display_order,
                is_special_input=src_p.is_special_input,
                frequency_days=src_p.frequency_days,
            )
            db.add(new_p)
            await db.flush()

            el_result = await db.execute(
                select(Element).where(Element.practice_id == src_p.id)
            )
            for src_el in el_result.scalars().all():
                db.add(Element(
                    practice_id=new_p.id,
                    element_type=src_el.element_type,
                    cosh_ref=src_el.cosh_ref,
                    value=src_el.value,
                    unit_cosh_id=src_el.unit_cosh_id,
                    display_order=src_el.display_order,
                ))


async def _wipe_pg_content(db: AsyncSession, pg_id: str) -> dict:
    """Delete every Timeline / Practice / Element under a PG.
    Returns counts so the caller can report what was overwritten."""
    timelines = (await db.execute(
        select(Timeline).where(Timeline.pg_recommendation_id == pg_id)
    )).scalars().all()
    tl_count = len(timelines)
    practice_count = 0
    element_count = 0
    for tl in timelines:
        practices = (await db.execute(
            select(Practice).where(Practice.timeline_id == tl.id)
        )).scalars().all()
        practice_count += len(practices)
        for p in practices:
            elements = (await db.execute(
                select(Element).where(Element.practice_id == p.id)
            )).scalars().all()
            element_count += len(elements)
            for el in elements:
                await db.delete(el)
            await db.delete(p)
        await db.delete(tl)
    await db.flush()
    return {
        "timelines_replaced": tl_count,
        "practices_replaced": practice_count,
        "elements_replaced": element_count,
    }


@router.post("/client/{client_id}/pg-recommendations/import/{global_pg_id}", response_model=PGRecommendationOut, status_code=201)
async def import_global_pg(
    client_id: str,
    global_pg_id: str,
    overwrite: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Import a Global PG into a client.

    Batch T (2026-05-18) — DRAFT is a single reusable slot per
    lineage; only Publish creates new rows. Behaviour:
      • No DRAFT exists yet → create a new DRAFT row with the
        imported content.
      • DRAFT exists, `overwrite=false` → 409 `draft_exists_confirm_overwrite`.
        Frontend asks the SE whether to replace.
      • DRAFT exists, `overwrite=true` → wipe the existing DRAFT's
        content and copy the import in. No new row, no demotion.

    The ACTIVE row keeps serving farmers untouched; nothing changes
    for them until the SE reviews + publishes.

    Auth: SUBJECT_EXPERT of this client OR CM-EDIT assignee.
    """
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)

    src = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.id == global_pg_id,
            PGRecommendation.client_id == None,  # noqa: E711
        )
    )).scalar_one_or_none()
    if not src:
        raise HTTPException(status_code=404, detail="Global PG recommendation not found")

    # Publish gate: only ACTIVE global PGs may be imported. DRAFTs are
    # CM-curated work-in-progress; INACTIVE rows are superseded versions.
    if src.status != "ACTIVE":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "global_pg_not_published",
                "message": (
                    "This global PG recommendation has not been published "
                    "(or has been deactivated). Ask the CM to publish it "
                    "before importing."
                ),
                "current_status": src.status,
            },
        )

    existing_draft = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.client_id == client_id,
            PGRecommendation.problem_group_cosh_id == src.problem_group_cosh_id,
            PGRecommendation.area_or_plant == src.area_or_plant,
            PGRecommendation.status == "DRAFT",
        )
    )).scalar_one_or_none()

    from datetime import datetime, timezone

    if existing_draft is not None:
        if not overwrite:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "draft_exists_confirm_overwrite",
                    "message": (
                        "A draft already exists for this PG and bundle. "
                        "Importing again will replace the draft's contents. "
                        "Confirm to proceed."
                    ),
                    "existing_draft_id": existing_draft.id,
                },
            )
        # Overwrite in place — wipe existing DRAFT content, copy import in.
        await _wipe_pg_content(db, existing_draft.id)
        await _copy_pg_content_into(db, src_pg=src, target_pg=existing_draft)
        existing_draft.parent_id = global_pg_id
        existing_draft.source_version_id = None
        existing_draft.created_via = "SE_PULL_DRAFT"
        existing_draft.imported_from_global_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(existing_draft)
        return existing_draft

    # No DRAFT yet — create one from the import.
    target = PGRecommendation(
        problem_group_cosh_id=src.problem_group_cosh_id,
        client_id=client_id,
        parent_id=global_pg_id,
        area_or_plant=src.area_or_plant,
        status="DRAFT",
        version=1,
        source_version_id=None,  # source is Global, not a same-lineage row
        created_via="SE_PULL_DRAFT",
        imported_from_global_at=datetime.now(timezone.utc),
    )
    db.add(target)
    await db.flush()

    await _copy_pg_content_into(db, src_pg=src, target_pg=target)

    await db.commit()
    await db.refresh(target)
    return target


@router.post(
    "/client/{client_id}/pg-recommendations/{pg_id}/clone-to-draft",
    response_model=PGRecommendationOut, status_code=201,
)
async def clone_client_pg_to_draft(
    client_id: str, pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SE picks an ACTIVE or INACTIVE row in the lineage to start
    editing.

    Batch T (2026-05-18) — DRAFT is a single reusable slot per
    lineage. Behaviour:
      • A DRAFT already exists in the lineage → return it. No new
        row, no demotion. The SE was probably mid-edit and the
        existing DRAFT IS their edit-in-progress.
      • No DRAFT exists → deep-copy this row's content into a new
        DRAFT row.

    Refuses if the source is itself a DRAFT (`clone_source_is_draft`).
    """
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    src = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.id == pg_id,
            PGRecommendation.client_id == client_id,
        )
    )).scalar_one_or_none()
    if src is None:
        raise HTTPException(status_code=404, detail="PG recommendation not found")
    if src.status == "DRAFT":
        raise HTTPException(status_code=422, detail={
            "code": "clone_source_is_draft",
            "message": (
                "clone-to-draft requires an ACTIVE or INACTIVE row of "
                "the lineage as the source. The current row is already "
                "a DRAFT — edit it in place."
            ),
        })

    existing_draft = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.client_id == client_id,
            PGRecommendation.problem_group_cosh_id == src.problem_group_cosh_id,
            PGRecommendation.area_or_plant == src.area_or_plant,
            PGRecommendation.status == "DRAFT",
        )
    )).scalar_one_or_none()
    if existing_draft is not None:
        # Reuse — Batch T's only-publish-creates-rows contract.
        return existing_draft

    new_draft = PGRecommendation(
        problem_group_cosh_id=src.problem_group_cosh_id,
        client_id=client_id,
        parent_id=src.parent_id,
        area_or_plant=src.area_or_plant,
        status="DRAFT",
        version=1,
        source_version_id=src.id,
        created_via="SE_EDIT_DRAFT",
    )
    db.add(new_draft)
    await db.flush()
    await _copy_pg_content_into(db, src_pg=src, target_pg=new_draft)
    await db.commit()
    await db.refresh(new_draft)
    return new_draft


@router.get("/client/{client_id}/pg-recommendations/{pg_id}/lineage")
async def get_client_pg_lineage(
    client_id: str, pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every row in this PG's lineage — rows sharing (client_id,
    problem_group_cosh_id, area_or_plant). Feeds the version-history
    panel + ReadOnlyBanner on the CA-PG detail page. Mirror of
    get_package_lineage."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    pg = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.id == pg_id,
            PGRecommendation.client_id == client_id,
        )
    )).scalar_one_or_none()
    if pg is None:
        raise HTTPException(status_code=404, detail="PG recommendation not found")
    rows = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.client_id == client_id,
            PGRecommendation.problem_group_cosh_id == pg.problem_group_cosh_id,
            PGRecommendation.area_or_plant == pg.area_or_plant,
        ).order_by(PGRecommendation.version.desc(), PGRecommendation.created_at.desc())
    )).scalars().all()

    def sort_key(p: PGRecommendation):
        is_draft = 0 if p.status == "DRAFT" else 1
        return (is_draft, -p.version, -p.created_at.timestamp())

    return [
        {
            "id": r.id,
            "status": r.status,
            "version": r.version,
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "created_at": r.created_at.isoformat(),
            "created_via": r.created_via,
            "source_version_id": r.source_version_id,
            "is_current": r.id == pg_id,
        }
        for r in sorted(rows, key=sort_key)
    ]


async def _check_pg_publish_readiness(
    db: AsyncSession, *, pg: PGRecommendation,
) -> list[dict]:
    """Returns the missing-list — empty when ready. Used by both the
    publish endpoint (raises 422 on non-empty) and the read-only
    publish-readiness endpoint (returns the list verbatim)."""
    missing: list[dict] = []

    if not pg.area_or_plant:
        missing.append({
            "code": "missing_area_or_plant",
            "message": (
                "This recommendation has no bundle (area-wise / plant-wise) "
                "set. Pick one before publishing."
            ),
        })

    tl_count = (await db.execute(
        select(func.count()).select_from(Timeline).where(
            Timeline.pg_recommendation_id == pg.id,
        )
    )).scalar() or 0
    if tl_count == 0:
        missing.append({
            "code": "no_timelines",
            "message": (
                "Add at least one timeline before publishing. A bundle "
                "without any guidance has nothing to advise on."
            ),
        })

    return missing


@router.get("/client/{client_id}/pg-recommendations/{pg_id}/publish-readiness")
async def get_pg_publish_readiness(
    client_id: str,
    pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read-only check of every gate `publish_client_pg` runs. Same
    response shape as the package /publish-readiness — frontend can
    render either with the same gate-panel component."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    pg = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.id == pg_id, PGRecommendation.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not pg:
        raise HTTPException(status_code=404, detail="PG recommendation not found")

    base = {"version": pg.version, "status": pg.status}

    transition = validate_publish_transition(pg.status)
    if not transition.allowed:
        return {
            **base,
            "ready": False,
            "blocker_code": transition.error_code,
            "missing": [{"code": transition.error_code, "message": transition.message}],
        }

    missing = await _check_pg_publish_readiness(db, pg=pg)
    if missing:
        return {
            **base,
            "ready": False,
            "blocker_code": "publish_blocked_missing_fields",
            "missing": missing,
        }

    return {**base, "ready": True}


@router.post("/client/{client_id}/pg-recommendations/{pg_id}/publish")
async def publish_client_pg(
    client_id: str,
    pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    pg = (await db.execute(
        select(PGRecommendation).where(PGRecommendation.id == pg_id, PGRecommendation.client_id == client_id)
    )).scalar_one_or_none()
    if not pg:
        raise HTTPException(status_code=404, detail="PG recommendation not found")
    res = validate_publish_transition(pg.status)
    if not res.allowed:
        _raise_publish_transition(res)

    # CHA hub Round 4: content checklist must be clean before
    # publish. Mirrors the CCA Step 2C checklist gate. Surfaces every
    # missing item in one 422 so the CA portal renders a checklist.
    missing = await _check_pg_publish_readiness(db, pg=pg)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "publish_blocked_missing_fields",
                "message": "Cannot publish — checklist not clean.",
                "missing": missing,
            },
        )

    # Demote BOTH prior ACTIVE and any sibling DRAFT to INACTIVE.
    # Sibling DRAFT covers the "imported draft I never used" case
    # (Batch T, 2026-05-18) — after a publish there is exactly one
    # live row in the lineage, nothing stale lurking.
    prev = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.problem_group_cosh_id == pg.problem_group_cosh_id,
            PGRecommendation.client_id == client_id,
            PGRecommendation.area_or_plant == pg.area_or_plant,
            PGRecommendation.status.in_(("ACTIVE", "DRAFT")),
            PGRecommendation.id != pg.id,
        )
    )).scalars().all()
    for p in prev:
        p.status = "INACTIVE"

    # Bump to max(other rows in lineage) + 1 — multi-row versioning
    # semantic (Batch R, 2026-05-18). Excluding the row being
    # published keeps first-publish at v1 (lineage had only this
    # DRAFT) while still bumping past any prior ACTIVE/INACTIVE
    # siblings.
    max_version = (await db.execute(
        select(func.max(PGRecommendation.version)).where(
            PGRecommendation.client_id == client_id,
            PGRecommendation.problem_group_cosh_id == pg.problem_group_cosh_id,
            PGRecommendation.area_or_plant == pg.area_or_plant,
            PGRecommendation.id != pg.id,
        )
    )).scalar()
    pg.version = (max_version or 0) + 1
    pg.status = "ACTIVE"
    from datetime import datetime, timezone
    pg.published_at = datetime.now(timezone.utc)
    pg.published_by = current_user.id
    await db.commit()
    await db.refresh(pg)
    return pg


# ── Client PG Timelines + Practices (for editing imported copies) ─────────────

@router.get("/client/{client_id}/pg-recommendations/{pg_id}/timelines")
async def list_client_pg_timelines(
    client_id: str,
    pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    result = await db.execute(
        select(Timeline).where(Timeline.pg_recommendation_id == pg_id)
    )
    timelines = result.scalars().all()
    out = []
    for tl in timelines:
        p_res = await db.execute(select(Practice).where(Practice.timeline_id == tl.id).order_by(Practice.display_order))
        out.append({
            "id": tl.id, "pg_recommendation_id": tl.pg_recommendation_id,
            "name": tl.name, "from_type": tl.from_type, "from_value": tl.from_value, "to_value": tl.to_value,
            "practices": [
                {"id": p.id, "l0_type": p.l0_type, "l1_type": p.l1_type, "l2_type": p.l2_type,
                 "display_order": p.display_order, "is_special_input": p.is_special_input}
                for p in p_res.scalars().all()
            ],
        })
    return out


@router.post("/client/{client_id}/pg-recommendations/{pg_id}/timelines", status_code=201)
async def add_client_pg_timeline(
    client_id: str,
    pg_id: str,
    request: PGTimelineCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_pg_recommendation_draft(db, pg_id, client_id=client_id)
    tl = Timeline(
        pg_recommendation_id=pg_id,
        name=request.name,
        from_type=request.from_type,
        from_value=request.from_value,
        to_value=request.to_value,
    )
    db.add(tl)
    await db.commit()
    await db.refresh(tl)
    return tl


@router.put(
    "/client/{client_id}/pg-recommendations/{pg_id}/timelines/{tl_id}",
)
async def update_client_pg_timeline(
    client_id: str,
    pg_id: str,
    tl_id: str,
    request: PGTimelineUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA-side mirror of update_global_pg_timeline (line ~5866).
    Edit Timeline modal on the CA-PG editor posts here. `from_type`
    stays immutable; `status` toggle gates the Timeline out of the
    farmer's daily advisory without deleting it.

    Returns a plain dict (NOT response_model=PGTimelineOut) — same
    lazy-load-on-serialize bug as update_global_pg_timeline (see
    docstring there)."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_pg_recommendation_draft(db, pg_id, client_id=client_id)
    tl = (await db.execute(
        select(Timeline).where(
            Timeline.id == tl_id,
            Timeline.pg_recommendation_id == pg_id,
        )
    )).scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")

    update_data = request.model_dump(exclude_unset=True)
    if "status" in update_data:
        if update_data["status"] not in ("ACTIVE", "INACTIVE"):
            raise HTTPException(status_code=422, detail={
                "code": "invalid_status",
                "message": "Timeline status must be ACTIVE or INACTIVE.",
            })

    for field, value in update_data.items():
        setattr(tl, field, value)
    await db.commit()
    await db.refresh(tl)
    return {
        "id": tl.id,
        "pg_recommendation_id": tl.pg_recommendation_id,
        "name": tl.name,
        "from_type": tl.from_type,
        "from_value": tl.from_value,
        "to_value": tl.to_value,
        "status": tl.status,
    }


@router.post("/client/{client_id}/pg-recommendations/{pg_id}/timelines/{tl_id}/practices", status_code=201)
async def add_client_pg_practice(
    client_id: str,
    pg_id: str,
    tl_id: str,
    request: PGPracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Local-PG practice — same UCAT shape as global-PG and Q&A."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_pg_recommendation_draft(db, pg_id, client_id=client_id)
    try:
        await assert_l2_elements_valid(
            db,
            l2_type=request.l2_type,
            elements=request.elements,
            is_special_input=request.is_special_input,
            frequency_days=request.frequency_days,
        )
    except L2ElementValidationError as e:
        _raise_l2_element_validation(e)

    practice = Practice(
        timeline_id=tl_id,
        l0_type=request.l0_type,
        l1_type=request.l1_type,
        l2_type=request.l2_type,
        display_order=request.display_order,
        is_special_input=request.is_special_input,
        frequency_days=request.frequency_days,
    )
    db.add(practice)
    await db.flush()
    for el in request.elements:
        db.add(Element(
            practice_id=practice.id,
            element_type=el.element_type,
            cosh_ref=el.cosh_ref,
            value=el.value,
            unit_cosh_id=el.unit_cosh_id,
            display_order=el.display_order,
        ))
    await db.commit()
    await db.refresh(practice)
    return practice


@router.put(
    "/client/{client_id}/pg-recommendations/{pg_id}/timelines/{tl_id}/practices/{practice_id}",
    response_model=PracticeOut,
)
async def update_client_pg_practice(
    client_id: str,
    pg_id: str,
    tl_id: str,
    practice_id: str,
    request: PGPracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA-side mirror of update_global_pg_practice (line ~5715).
    Atomic Practice replace — same shape as add_client_pg_practice
    so the shared <PracticeFormModal> can call this for Edit. The
    Practice row's elements list is replaced wholesale to match
    what the form submits."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_pg_recommendation_draft(db, pg_id, client_id=client_id)
    try:
        await assert_l2_elements_valid(
            db,
            l2_type=request.l2_type,
            elements=request.elements,
            is_special_input=request.is_special_input,
            frequency_days=request.frequency_days,
        )
    except L2ElementValidationError as e:
        _raise_l2_element_validation(e)

    practice = (await db.execute(
        select(Practice).where(
            Practice.id == practice_id,
            Practice.timeline_id == tl_id,
        )
    )).scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")

    practice.l0_type = request.l0_type
    practice.l1_type = request.l1_type
    practice.l2_type = request.l2_type
    practice.display_order = request.display_order
    practice.is_special_input = request.is_special_input
    practice.frequency_days = request.frequency_days

    # Wipe + re-insert elements (atomic replace mirrors what the
    # PracticeFormModal posts on Edit).
    existing_els = (await db.execute(
        select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    for e in existing_els:
        await db.delete(e)
    for el in request.elements:
        db.add(Element(
            practice_id=practice.id,
            element_type=el.element_type,
            cosh_ref=el.cosh_ref,
            value=el.value,
            unit_cosh_id=el.unit_cosh_id,
            display_order=el.display_order,
        ))
    await db.commit()
    await db.refresh(practice)
    return practice


# ── CHA local-PG per-element CRUD (Round 2) ────────────────────────────────

@router.post(
    "/client/{client_id}/pg-recommendations/{pg_id}/timelines/{tl_id}/practices/{practice_id}/elements",
    status_code=201,
)
async def add_client_pg_element(
    client_id: str, pg_id: str, tl_id: str, practice_id: str,
    body: ElementIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    practice = await _load_pg_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    new = await _add_practice_element(
        db, practice=practice, element_model=Element, body=body,
    )
    return _element_row_to_out(new)


@router.put(
    "/client/{client_id}/pg-recommendations/{pg_id}/timelines/{tl_id}/practices/{practice_id}/elements/{element_id}",
)
async def update_client_pg_element(
    client_id: str, pg_id: str, tl_id: str, practice_id: str, element_id: str,
    body: ElementIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    practice = await _load_pg_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    updated = await _update_practice_element(
        db, practice=practice, element_model=Element,
        element_id=element_id, body=body,
    )
    return _element_row_to_out(updated)


@router.delete(
    "/client/{client_id}/pg-recommendations/{pg_id}/timelines/{tl_id}/practices/{practice_id}/elements/{element_id}",
    status_code=204,
)
async def delete_client_pg_element(
    client_id: str, pg_id: str, tl_id: str, practice_id: str, element_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    practice = await _load_pg_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    await _delete_practice_element(
        db, practice=practice, element_model=Element, element_id=element_id,
    )


@router.delete(
    "/client/{client_id}/pg-recommendations/{pg_id}/timelines/{tl_id}/practices/{practice_id}",
    status_code=204,
)
async def delete_client_pg_practice(
    client_id: str, pg_id: str, tl_id: str, practice_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove a practice from a client-local PG recommendation. Cascade
    drops its elements via ORM. Mirror of delete_practice on CCA."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_pg_recommendation_draft(db, pg_id, client_id=client_id)
    practice = (await db.execute(
        select(Practice).where(
            Practice.id == practice_id,
            Practice.timeline_id == tl_id,
        )
    )).scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")
    elems = (await db.execute(
        select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    for e in elems:
        await db.delete(e)
    await db.delete(practice)
    await db.commit()


@router.delete("/client/{client_id}/pg-recommendations/{pg_id}/timelines/{tl_id}", status_code=204)
async def delete_client_pg_timeline(
    client_id: str,
    pg_id: str,
    tl_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_pg_recommendation_draft(db, pg_id, client_id=client_id)
    tl = (await db.execute(
        select(Timeline).where(Timeline.id == tl_id, Timeline.pg_recommendation_id == pg_id)
    )).scalar_one_or_none()
    if tl:
        await db.delete(tl)
        await db.commit()


# ── Client SP Recommendations ──────────────────────────────────────────────────

@router.get("/client/{client_id}/sp-recommendations", response_model=list[SPRecommendationOut])
async def list_client_sp(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.services.crop_measure import get_measure
    result = await db.execute(
        select(SPRecommendation).where(SPRecommendation.client_id == client_id)
        .order_by(SPRecommendation.created_at.desc())
    )
    sps = result.scalars().all()
    measure_by_crop: dict[str, str | None] = {}
    out: list[SPRecommendationOut] = []
    for sp in sps:
        measure: str | None = None
        if sp.crop_cosh_id:
            if sp.crop_cosh_id not in measure_by_crop:
                measure_by_crop[sp.crop_cosh_id] = await get_measure(db, sp.crop_cosh_id)
            measure = measure_by_crop[sp.crop_cosh_id]
        out.append(SPRecommendationOut(
            id=sp.id,
            specific_problem_cosh_id=sp.specific_problem_cosh_id,
            client_id=sp.client_id,
            crop_cosh_id=sp.crop_cosh_id,
            crop_measure=measure,
            status=sp.status,
            version=sp.version,
            created_at=sp.created_at,
        ))
    return out


@router.post("/client/{client_id}/sp-recommendations", response_model=SPRecommendationOut, status_code=201)
async def create_client_sp(
    client_id: str,
    request: SPRecommendationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    sp = SPRecommendation(
        specific_problem_cosh_id=request.specific_problem_cosh_id,
        client_id=client_id,
        crop_cosh_id=request.crop_cosh_id,
    )
    db.add(sp)
    await db.commit()
    await db.refresh(sp)
    return sp


@router.get("/client/{client_id}/sp-recommendations/{sp_id}/timelines")
async def list_sp_timelines(
    client_id: str,
    sp_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    result = await db.execute(select(Timeline).where(Timeline.sp_recommendation_id == sp_id))
    timelines = result.scalars().all()
    out = []
    for tl in timelines:
        p_res = await db.execute(select(Practice).where(Practice.timeline_id == tl.id).order_by(Practice.display_order))
        out.append({
            "id": tl.id, "sp_recommendation_id": tl.sp_recommendation_id,
            "name": tl.name, "from_type": tl.from_type, "from_value": tl.from_value, "to_value": tl.to_value,
            "practices": [
                {"id": p.id, "l0_type": p.l0_type, "l1_type": p.l1_type, "l2_type": p.l2_type,
                 "display_order": p.display_order, "is_special_input": p.is_special_input}
                for p in p_res.scalars().all()
            ],
        })
    return out


@router.post("/client/{client_id}/sp-recommendations/{sp_id}/timelines", status_code=201)
async def add_sp_timeline(
    client_id: str,
    sp_id: str,
    request: SPTimelineCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    tl = Timeline(
        sp_recommendation_id=sp_id,
        name=request.name,
        from_type=request.from_type,
        from_value=request.from_value,
        to_value=request.to_value,
    )
    db.add(tl)
    await db.commit()
    await db.refresh(tl)
    return tl


@router.put("/client/{client_id}/sp-recommendations/{sp_id}/timelines/{tl_id}")
async def update_sp_timeline(
    client_id: str,
    sp_id: str,
    tl_id: str,
    request: PGTimelineUpdate,  # name/from/to/status — same shape as PG/QA
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA-SP Edit Timeline (Phase 4 of CA-portal parity, 2026-05-17).
    Mirror of update_client_pg_timeline. `from_type` stays immutable;
    `status` toggle excludes the Timeline from the farmer's daily
    advisory without deleting it."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    tl = (await db.execute(
        select(Timeline).where(
            Timeline.id == tl_id,
            Timeline.sp_recommendation_id == sp_id,
        )
    )).scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")

    update_data = request.model_dump(exclude_unset=True)
    if "status" in update_data:
        if update_data["status"] not in ("ACTIVE", "INACTIVE"):
            raise HTTPException(status_code=422, detail={
                "code": "invalid_status",
                "message": "Timeline status must be ACTIVE or INACTIVE.",
            })

    for field, value in update_data.items():
        setattr(tl, field, value)
    await db.commit()
    await db.refresh(tl)
    return tl


@router.post("/client/{client_id}/sp-recommendations/{sp_id}/timelines/{tl_id}/practices", status_code=201)
async def add_sp_practice(
    client_id: str,
    sp_id: str,
    tl_id: str,
    request: SPPracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SP practice — same UCAT shape as PG and Q&A."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    try:
        await assert_l2_elements_valid(
            db,
            l2_type=request.l2_type,
            elements=request.elements,
            is_special_input=request.is_special_input,
            frequency_days=request.frequency_days,
        )
    except L2ElementValidationError as e:
        _raise_l2_element_validation(e)

    practice = Practice(
        timeline_id=tl_id,
        l0_type=request.l0_type,
        l1_type=request.l1_type,
        l2_type=request.l2_type,
        display_order=request.display_order,
        is_special_input=request.is_special_input,
        frequency_days=request.frequency_days,
    )
    db.add(practice)
    await db.flush()
    for el in request.elements:
        db.add(Element(
            practice_id=practice.id,
            element_type=el.element_type,
            cosh_ref=el.cosh_ref,
            value=el.value,
            unit_cosh_id=el.unit_cosh_id,
            display_order=el.display_order,
        ))
    await db.commit()
    await db.refresh(practice)
    return practice


@router.put(
    "/client/{client_id}/sp-recommendations/{sp_id}/timelines/{tl_id}/practices/{practice_id}",
    response_model=PracticeOut,
)
async def update_sp_practice(
    client_id: str,
    sp_id: str,
    tl_id: str,
    practice_id: str,
    request: SPPracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA-SP atomic Practice replace (Phase 4 of CA-portal parity,
    2026-05-17). Mirror of update_client_pg_practice — same shape
    so the shared <PracticeFormModal> uses one body for Create and
    Edit. Elements wiped + reinserted to match what the form
    submits."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    try:
        await assert_l2_elements_valid(
            db,
            l2_type=request.l2_type,
            elements=request.elements,
            is_special_input=request.is_special_input,
            frequency_days=request.frequency_days,
        )
    except L2ElementValidationError as e:
        _raise_l2_element_validation(e)

    practice = (await db.execute(
        select(Practice).where(
            Practice.id == practice_id,
            Practice.timeline_id == tl_id,
        )
    )).scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")

    practice.l0_type = request.l0_type
    practice.l1_type = request.l1_type
    practice.l2_type = request.l2_type
    practice.display_order = request.display_order
    practice.is_special_input = request.is_special_input
    practice.frequency_days = request.frequency_days

    existing_els = (await db.execute(
        select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    for e in existing_els:
        await db.delete(e)
    for el in request.elements:
        db.add(Element(
            practice_id=practice.id,
            element_type=el.element_type,
            cosh_ref=el.cosh_ref,
            value=el.value,
            unit_cosh_id=el.unit_cosh_id,
            display_order=el.display_order,
        ))
    await db.commit()
    await db.refresh(practice)
    return practice


# ── CHA local-SP per-element CRUD (Round 2) ────────────────────────────────

async def _load_sp_practice_by_timeline(db, *, timeline_id: str, practice_id: str):
    practice = (await db.execute(
        select(Practice).where(
            Practice.id == practice_id,
            Practice.timeline_id == timeline_id,
        )
    )).scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")
    return practice


@router.post(
    "/client/{client_id}/sp-recommendations/{sp_id}/timelines/{tl_id}/practices/{practice_id}/elements",
    status_code=201,
)
async def add_sp_element(
    client_id: str, sp_id: str, tl_id: str, practice_id: str,
    body: ElementIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    practice = await _load_sp_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    new = await _add_practice_element(
        db, practice=practice, element_model=Element, body=body,
    )
    return _element_row_to_out(new)


@router.put(
    "/client/{client_id}/sp-recommendations/{sp_id}/timelines/{tl_id}/practices/{practice_id}/elements/{element_id}",
)
async def update_sp_element(
    client_id: str, sp_id: str, tl_id: str, practice_id: str, element_id: str,
    body: ElementIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    practice = await _load_sp_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    updated = await _update_practice_element(
        db, practice=practice, element_model=Element,
        element_id=element_id, body=body,
    )
    return _element_row_to_out(updated)


@router.delete(
    "/client/{client_id}/sp-recommendations/{sp_id}/timelines/{tl_id}/practices/{practice_id}/elements/{element_id}",
    status_code=204,
)
async def delete_sp_element(
    client_id: str, sp_id: str, tl_id: str, practice_id: str, element_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    practice = await _load_sp_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    await _delete_practice_element(
        db, practice=practice, element_model=Element, element_id=element_id,
    )


@router.delete("/client/{client_id}/sp-recommendations/{sp_id}/timelines/{tl_id}", status_code=204)
async def delete_sp_timeline(
    client_id: str,
    sp_id: str,
    tl_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    tl = (await db.execute(
        select(Timeline).where(Timeline.id == tl_id, Timeline.sp_recommendation_id == sp_id)
    )).scalar_one_or_none()
    if tl:
        await db.delete(tl)
        await db.commit()


# ── Client Q&A Library timelines (UCAT pipe-3, spec §14.9) ──────────────────
# These endpoints write into the same `pg_timelines` / `pg_practices` /
# `pg_elements` tables as the CHA endpoints above; the difference is the
# parent — `standard_response_id` instead of `pg_recommendation_id`.
# Practices and Elements are reused as-is. The DB CHECK
# `pg_timelines_one_parent_chk` guarantees a row never has both parents.

async def _assert_sr_belongs_to_client(
    db: AsyncSession, sr_id: str, client_id: str,
):
    """Look up a StandardResponse and assert it belongs to the
    target client. 404 on miss or cross-client (same shape on both
    so the existence of other clients' rows isn't leaked).

    Imported lazily because StandardResponse lives in the farmpundit
    module — the static import at the top of this file would create
    a cycle since farmpundit.router imports from advisory tables."""
    from app.modules.farmpundit.models import StandardResponse

    sr = (await db.execute(
        select(StandardResponse).where(
            StandardResponse.id == sr_id,
            StandardResponse.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not sr:
        raise HTTPException(status_code=404, detail="Standard response not found")
    return sr


@router.get("/client/{client_id}/standard-responses/{sr_id}/timelines")
async def list_qa_timelines(
    client_id: str,
    sr_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full advisory tree under a Standard Response — Timelines with
    nested Practices and Elements. The CA-portal editor renders the
    whole tree at once; the Pundit picker only needs the metadata
    so it goes through the simpler farmpundit search endpoint."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)

    timelines = (await db.execute(
        select(Timeline).where(
            Timeline.standard_response_id == sr_id,
        ).order_by(Timeline.from_value, Timeline.id)
    )).scalars().all()

    out = []
    for tl in timelines:
        practices = (await db.execute(
            select(Practice).where(Practice.timeline_id == tl.id)
            .order_by(Practice.display_order)
        )).scalars().all()
        practice_dicts = []
        for p in practices:
            elements = (await db.execute(
                select(Element).where(Element.practice_id == p.id)
                .order_by(Element.display_order)
            )).scalars().all()
            practice_dicts.append({
                "id": p.id,
                "timeline_id": p.timeline_id,
                "l0_type": p.l0_type,
                "l1_type": p.l1_type,
                "l2_type": p.l2_type,
                "display_order": p.display_order,
                "is_special_input": p.is_special_input,
                "frequency_days": p.frequency_days,
                "elements": [
                    {
                        "id": e.id,
                        "element_type": e.element_type,
                        "cosh_ref": e.cosh_ref,
                        "value": e.value,
                        "unit_cosh_id": e.unit_cosh_id,
                        "display_order": e.display_order,
                    }
                    for e in elements
                ],
            })
        out.append({
            "id": tl.id,
            "standard_response_id": tl.standard_response_id,
            "parent_kind": tl.parent_kind,
            "name": tl.name,
            "from_type": tl.from_type,
            "from_value": tl.from_value,
            "to_value": tl.to_value,
            "practices": practice_dicts,
        })
    return out


@router.post("/client/{client_id}/standard-responses/{sr_id}/timelines", status_code=201)
async def add_qa_timeline(
    client_id: str,
    sr_id: str,
    request: QATimelineCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)

    tl = Timeline(
        standard_response_id=sr_id,
        # pg_recommendation_id stays None — the DB CHECK enforces
        # exactly-one-parent so this row can never drift into a
        # dual-parent state.
        name=request.name,
        from_type=request.from_type,
        from_value=request.from_value,
        to_value=request.to_value,
    )
    db.add(tl)
    await db.commit()
    await db.refresh(tl)
    return {
        "id": tl.id,
        "standard_response_id": tl.standard_response_id,
        "parent_kind": tl.parent_kind,
        "name": tl.name,
        "from_type": tl.from_type,
        "from_value": tl.from_value,
        "to_value": tl.to_value,
    }


@router.put("/client/{client_id}/standard-responses/{sr_id}/timelines/{tl_id}")
async def update_qa_timeline(
    client_id: str,
    sr_id: str,
    tl_id: str,
    request: PGTimelineUpdate,  # name/from/to/status — same shape as PG/SP
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA-QA Edit Timeline (Phase 4 of CA-portal parity, 2026-05-17).
    Mirror of update_client_pg_timeline. `from_type` (default
    DAYS_AFTER_RESPONSE) stays immutable. Note QA has no publish
    lifecycle — these edits are immediately live for FarmPundits."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)

    tl = (await db.execute(
        select(Timeline).where(
            Timeline.id == tl_id,
            Timeline.standard_response_id == sr_id,
        )
    )).scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")

    update_data = request.model_dump(exclude_unset=True)
    if "status" in update_data:
        if update_data["status"] not in ("ACTIVE", "INACTIVE"):
            raise HTTPException(status_code=422, detail={
                "code": "invalid_status",
                "message": "Timeline status must be ACTIVE or INACTIVE.",
            })

    for field, value in update_data.items():
        setattr(tl, field, value)
    await db.commit()
    await db.refresh(tl)
    return {
        "id": tl.id,
        "standard_response_id": tl.standard_response_id,
        "parent_kind": tl.parent_kind,
        "name": tl.name,
        "from_type": tl.from_type,
        "from_value": tl.from_value,
        "to_value": tl.to_value,
        "status": tl.status,
    }


@router.delete(
    "/client/{client_id}/standard-responses/{sr_id}/timelines/{tl_id}",
    status_code=204,
)
async def delete_qa_timeline(
    client_id: str,
    sr_id: str,
    tl_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cascade-deletes the timeline's practices and elements via the
    SQLAlchemy session's delete-orphan cascade behaviour. Practice
    and Element FKs to timeline_id / practice_id remain intact in
    the schema — the cascade is application-level via SQLAlchemy
    relationships, matching the existing PG/SP delete patterns."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)

    tl = (await db.execute(
        select(Timeline).where(
            Timeline.id == tl_id,
            Timeline.standard_response_id == sr_id,
        )
    )).scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")

    # Manually delete practices and elements first — SQLAlchemy
    # relationships on Timeline don't carry cascade='delete' (it
    # would require a back_populates change that ripples through
    # CHA tests). Mirrors the PG delete pattern though PG's delete
    # endpoint relies on the caller having no practices yet.
    practices = (await db.execute(
        select(Practice).where(Practice.timeline_id == tl_id)
    )).scalars().all()
    for p in practices:
        elements = (await db.execute(
            select(Element).where(Element.practice_id == p.id)
        )).scalars().all()
        for e in elements:
            await db.delete(e)
        await db.delete(p)
    await db.delete(tl)
    await db.commit()


@router.post(
    "/client/{client_id}/standard-responses/{sr_id}/timelines/{tl_id}/practices",
    status_code=201,
)
async def add_qa_practice(
    client_id: str,
    sr_id: str,
    tl_id: str,
    request: QAPracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a Practice on a Q&A timeline with its Elements inline.
    Mirrors PGPracticeCreate exactly — UCAT means Practice + Element
    shapes are pipe-agnostic."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)

    tl = (await db.execute(
        select(Timeline).where(
            Timeline.id == tl_id,
            Timeline.standard_response_id == sr_id,
        )
    )).scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")

    try:
        await assert_l2_elements_valid(
            db,
            l2_type=request.l2_type,
            elements=request.elements,
            is_special_input=request.is_special_input,
            frequency_days=request.frequency_days,
        )
    except L2ElementValidationError as e:
        _raise_l2_element_validation(e)

    practice = Practice(
        timeline_id=tl_id,
        l0_type=request.l0_type,
        l1_type=request.l1_type,
        l2_type=request.l2_type,
        display_order=request.display_order,
        is_special_input=request.is_special_input,
        frequency_days=request.frequency_days,
    )
    db.add(practice)
    await db.flush()

    for el in request.elements:
        db.add(Element(
            practice_id=practice.id,
            element_type=el.element_type,
            cosh_ref=el.cosh_ref,
            value=el.value,
            unit_cosh_id=el.unit_cosh_id,
            display_order=el.display_order,
        ))
    await db.commit()
    await db.refresh(practice)

    elements = (await db.execute(
        select(Element).where(Element.practice_id == practice.id)
        .order_by(Element.display_order)
    )).scalars().all()
    return {
        "id": practice.id,
        "timeline_id": practice.timeline_id,
        "l0_type": practice.l0_type,
        "l1_type": practice.l1_type,
        "l2_type": practice.l2_type,
        "display_order": practice.display_order,
        "is_special_input": practice.is_special_input,
        "frequency_days": practice.frequency_days,
        "elements": [
            {
                "id": e.id,
                "element_type": e.element_type,
                "cosh_ref": e.cosh_ref,
                "value": e.value,
                "unit_cosh_id": e.unit_cosh_id,
                "display_order": e.display_order,
            }
            for e in elements
        ],
    }


@router.put(
    "/client/{client_id}/standard-responses/{sr_id}/timelines/{tl_id}/practices/{practice_id}",
)
async def update_qa_practice(
    client_id: str,
    sr_id: str,
    tl_id: str,
    practice_id: str,
    request: QAPracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA-QA atomic Practice replace (Phase 4 of CA-portal parity,
    2026-05-17). Mirror of update_client_pg_practice / update_sp_practice.
    Elements wiped + reinserted to match what the shared modal
    submits."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)

    try:
        await assert_l2_elements_valid(
            db,
            l2_type=request.l2_type,
            elements=request.elements,
            is_special_input=request.is_special_input,
            frequency_days=request.frequency_days,
        )
    except L2ElementValidationError as e:
        _raise_l2_element_validation(e)

    practice = (await db.execute(
        select(Practice).where(
            Practice.id == practice_id,
            Practice.timeline_id == tl_id,
        )
    )).scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")

    practice.l0_type = request.l0_type
    practice.l1_type = request.l1_type
    practice.l2_type = request.l2_type
    practice.display_order = request.display_order
    practice.is_special_input = request.is_special_input
    practice.frequency_days = request.frequency_days

    existing_els = (await db.execute(
        select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    for e in existing_els:
        await db.delete(e)
    for el in request.elements:
        db.add(Element(
            practice_id=practice.id,
            element_type=el.element_type,
            cosh_ref=el.cosh_ref,
            value=el.value,
            unit_cosh_id=el.unit_cosh_id,
            display_order=el.display_order,
        ))
    await db.commit()
    await db.refresh(practice)

    elements = (await db.execute(
        select(Element).where(Element.practice_id == practice.id)
        .order_by(Element.display_order)
    )).scalars().all()
    return {
        "id": practice.id,
        "timeline_id": practice.timeline_id,
        "l0_type": practice.l0_type,
        "l1_type": practice.l1_type,
        "l2_type": practice.l2_type,
        "display_order": practice.display_order,
        "is_special_input": practice.is_special_input,
        "frequency_days": practice.frequency_days,
        "elements": [
            {
                "id": e.id,
                "element_type": e.element_type,
                "cosh_ref": e.cosh_ref,
                "value": e.value,
                "unit_cosh_id": e.unit_cosh_id,
                "display_order": e.display_order,
            }
            for e in elements
        ],
    }


# ── Q&A per-element CRUD (Round 2) ─────────────────────────────────────────

async def _assert_qa_practice_path(
    db, *, current_user, client_id: str, sr_id: str,
    tl_id: str, practice_id: str,
):
    """Q&A authoring is gated on portal-member auth + sr-belongs-to-client.
    The practice itself must live under the named QA timeline (which in
    turn lives under the named standard_response_id)."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)
    tl = (await db.execute(
        select(Timeline).where(
            Timeline.id == tl_id,
            Timeline.standard_response_id == sr_id,
        )
    )).scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")
    practice = (await db.execute(
        select(Practice).where(
            Practice.id == practice_id,
            Practice.timeline_id == tl_id,
        )
    )).scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")
    return practice


@router.post(
    "/client/{client_id}/standard-responses/{sr_id}/timelines/{tl_id}/practices/{practice_id}/elements",
    status_code=201,
)
async def add_qa_element(
    client_id: str, sr_id: str, tl_id: str, practice_id: str,
    body: ElementIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    practice = await _assert_qa_practice_path(
        db, current_user=current_user, client_id=client_id,
        sr_id=sr_id, tl_id=tl_id, practice_id=practice_id,
    )
    new = await _add_practice_element(
        db, practice=practice, element_model=Element, body=body,
    )
    return _element_row_to_out(new)


@router.put(
    "/client/{client_id}/standard-responses/{sr_id}/timelines/{tl_id}/practices/{practice_id}/elements/{element_id}",
)
async def update_qa_element(
    client_id: str, sr_id: str, tl_id: str, practice_id: str, element_id: str,
    body: ElementIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    practice = await _assert_qa_practice_path(
        db, current_user=current_user, client_id=client_id,
        sr_id=sr_id, tl_id=tl_id, practice_id=practice_id,
    )
    updated = await _update_practice_element(
        db, practice=practice, element_model=Element,
        element_id=element_id, body=body,
    )
    return _element_row_to_out(updated)


@router.delete(
    "/client/{client_id}/standard-responses/{sr_id}/timelines/{tl_id}/practices/{practice_id}/elements/{element_id}",
    status_code=204,
)
async def delete_qa_element(
    client_id: str, sr_id: str, tl_id: str, practice_id: str, element_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    practice = await _assert_qa_practice_path(
        db, current_user=current_user, client_id=client_id,
        sr_id=sr_id, tl_id=tl_id, practice_id=practice_id,
    )
    await _delete_practice_element(
        db, practice=practice, element_model=Element, element_id=element_id,
    )


@router.delete(
    "/client/{client_id}/standard-responses/{sr_id}/timelines/{tl_id}/practices/{p_id}",
    status_code=204,
)
async def delete_qa_practice(
    client_id: str,
    sr_id: str,
    tl_id: str,
    p_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)

    practice = (await db.execute(
        select(Practice).where(
            Practice.id == p_id,
            Practice.timeline_id == tl_id,
        )
    )).scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")

    # Same parent-walk validation as the timeline endpoints — make
    # sure the timeline really belongs to this Standard Response
    # before deleting under it.
    tl = (await db.execute(
        select(Timeline).where(
            Timeline.id == tl_id,
            Timeline.standard_response_id == sr_id,
        )
    )).scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")

    elements = (await db.execute(
        select(Element).where(Element.practice_id == p_id)
    )).scalars().all()
    for e in elements:
        await db.delete(e)
    await db.delete(practice)
    await db.commit()


async def _wipe_sp_content(db: AsyncSession, sp_id: str) -> dict:
    """Delete every Timeline / Practice / Element under an SP.
    Mirrors `_wipe_pg_content` for the SP-side import overwrite path."""
    timelines = (await db.execute(
        select(Timeline).where(Timeline.sp_recommendation_id == sp_id)
    )).scalars().all()
    tl_count = len(timelines)
    practice_count = 0
    element_count = 0
    for tl in timelines:
        practices = (await db.execute(
            select(Practice).where(Practice.timeline_id == tl.id)
        )).scalars().all()
        practice_count += len(practices)
        for p in practices:
            elements = (await db.execute(
                select(Element).where(Element.practice_id == p.id)
            )).scalars().all()
            element_count += len(elements)
            for el in elements:
                await db.delete(el)
            await db.delete(p)
        await db.delete(tl)
    await db.flush()
    return {
        "timelines_replaced": tl_count,
        "practices_replaced": practice_count,
        "elements_replaced": element_count,
    }


@router.post(
    "/client/{client_id}/sp-recommendations/{sp_id}/import-from-pg/{local_pg_id}",
    response_model=SPRecommendationOut,
    status_code=201,
)
async def import_pg_into_sp(
    client_id: str,
    sp_id: str,
    local_pg_id: str,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deep-copy a LOCAL PG recommendation's content (timelines /
    practices / elements) into an existing SP recommendation as a
    starting point. The SE then customises from there.

    Source PG must belong to the same client. Cross-client imports
    are blocked at the 404 level (no information leak about other
    clients' data).

    First import (SP has no timelines yet): copies content directly.
    Re-import (SP already has content):
      • Default — refuses with 409 + structured `existing` summary
        so the CA portal can show a "this will overwrite your local
        edits" warning.
      • `?force=true` — wipes existing SP content and re-imports
        fresh from the source PG. SP recommendation row keeps its
        same primary key.

    The SP must already exist (created via
    POST /client/{id}/sp-recommendations). This endpoint adds
    content; it doesn't create the SP itself.

    Auth (Batch 39U, widened from CM-EDIT-only on 2026-05-17): any
    ACTIVE ClientUser of this client OR an ACTIVE CMClientAssignment
    with EDIT rights may import. Earlier CM-only gate was a mismatch
    — this is a within-client copy operation (cross-client is 404'd
    below); the SE who authored the source PG should be able to
    seed their own SP from it without round-tripping through the CM."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)

    sp = (await db.execute(
        select(SPRecommendation).where(
            SPRecommendation.id == sp_id,
            SPRecommendation.client_id == client_id,
        )
    )).scalar_one_or_none()
    if sp is None:
        raise HTTPException(status_code=404, detail="SP recommendation not found")

    src_pg = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.id == local_pg_id,
            PGRecommendation.client_id == client_id,
        )
    )).scalar_one_or_none()
    if src_pg is None:
        # 404 — could be wrong id OR cross-client. Same response
        # either way; no info leak.
        raise HTTPException(status_code=404, detail="Local PG recommendation not found")

    existing_tl_count = (await db.execute(
        select(func.count()).select_from(Timeline).where(
            Timeline.sp_recommendation_id == sp_id,
        )
    )).scalar() or 0

    if existing_tl_count > 0 and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "import_would_overwrite",
                "message": (
                    "This SP recommendation already has content. Re-importing "
                    "from a PG will overwrite its existing timelines and "
                    "practices. Send force=true to confirm."
                ),
                "existing": {
                    "sp_recommendation_id": sp_id,
                    "timeline_count": existing_tl_count,
                },
            },
        )

    if existing_tl_count > 0:
        await _wipe_sp_content(db, sp_id)

    # Deep-copy: Timeline → Timeline, Practice → Practice,
    # Element → Element. Same field shapes; just different tables.
    pg_timelines = (await db.execute(
        select(Timeline).where(Timeline.pg_recommendation_id == src_pg.id)
    )).scalars().all()
    for src_tl in pg_timelines:
        new_tl = Timeline(
            sp_recommendation_id=sp_id,
            name=src_tl.name,
            from_type=src_tl.from_type,
            from_value=src_tl.from_value,
            to_value=src_tl.to_value,
        )
        db.add(new_tl)
        await db.flush()

        src_practices = (await db.execute(
            select(Practice).where(Practice.timeline_id == src_tl.id)
        )).scalars().all()
        for src_p in src_practices:
            new_p = Practice(
                timeline_id=new_tl.id,
                l0_type=src_p.l0_type,
                l1_type=src_p.l1_type,
                l2_type=src_p.l2_type,
                display_order=src_p.display_order,
                is_special_input=src_p.is_special_input,
                frequency_days=src_p.frequency_days,
            )
            db.add(new_p)
            await db.flush()

            src_elements = (await db.execute(
                select(Element).where(Element.practice_id == src_p.id)
            )).scalars().all()
            for src_el in src_elements:
                db.add(Element(
                    practice_id=new_p.id,
                    element_type=src_el.element_type,
                    cosh_ref=src_el.cosh_ref,
                    value=src_el.value,
                    unit_cosh_id=src_el.unit_cosh_id,
                    display_order=src_el.display_order,
                ))

    await db.commit()
    await db.refresh(sp)
    return sp


@router.get("/client/{client_id}/sp-recommendations/{sp_id}", response_model=SPRecommendationOut)
async def get_client_sp(
    client_id: str, sp_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.services.crop_measure import get_measure
    sp = (await db.execute(
        select(SPRecommendation).where(
            SPRecommendation.id == sp_id, SPRecommendation.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not sp:
        raise HTTPException(status_code=404, detail="SP recommendation not found")
    measure = await get_measure(db, sp.crop_cosh_id) if sp.crop_cosh_id else None
    return SPRecommendationOut(
        id=sp.id,
        specific_problem_cosh_id=sp.specific_problem_cosh_id,
        client_id=sp.client_id,
        crop_cosh_id=sp.crop_cosh_id,
        crop_measure=measure,
        status=sp.status,
        version=sp.version,
        created_at=sp.created_at,
    )


@router.delete(
    "/client/{client_id}/sp-recommendations/{sp_id}/timelines/{tl_id}/practices/{practice_id}",
    status_code=204,
)
async def delete_client_sp_practice(
    client_id: str, sp_id: str, tl_id: str, practice_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mirror of delete_client_pg_practice. Cascades the practice's
    elements via ORM."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    from app.modules.advisory.models import Element, Practice
    practice = (await db.execute(
        select(Practice).where(
            Practice.id == practice_id, Practice.timeline_id == tl_id,
        )
    )).scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")
    elems = (await db.execute(
        select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    for e in elems:
        await db.delete(e)
    await db.delete(practice)
    await db.commit()


async def _check_sp_publish_readiness(
    db: AsyncSession, *, sp: SPRecommendation,
) -> list[dict]:
    """Mirror of `_check_pg_publish_readiness`. SP-specific gates:
    crop_cosh_id set + at least one timeline."""
    missing: list[dict] = []
    if not sp.crop_cosh_id:
        missing.append({
            "code": "missing_crop_cosh_id",
            "message": (
                "This SP recommendation has no crop set — pre-Round-1 "
                "rows might be NULL. Re-create the recommendation."
            ),
        })
    tl_count = (await db.execute(
        select(func.count()).select_from(Timeline).where(
            Timeline.sp_recommendation_id == sp.id,
        )
    )).scalar() or 0
    if tl_count == 0:
        missing.append({
            "code": "no_timelines",
            "message": (
                "Add at least one timeline before publishing. A bundle "
                "without any guidance has nothing to advise on."
            ),
        })
    return missing


@router.get("/client/{client_id}/sp-recommendations/{sp_id}/publish-readiness")
async def get_sp_publish_readiness(
    client_id: str, sp_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read-only check of every gate `publish_sp` runs. Same envelope
    shape as the package + PG readiness endpoints — frontend renders
    all three with the same gate-panel component."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    sp = (await db.execute(
        select(SPRecommendation).where(
            SPRecommendation.id == sp_id, SPRecommendation.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not sp:
        raise HTTPException(status_code=404, detail="SP recommendation not found")

    base = {"version": sp.version, "status": sp.status}

    transition = validate_publish_transition(sp.status)
    if not transition.allowed:
        return {**base, "ready": False, "blocker_code": transition.error_code,
                "missing": [{"code": transition.error_code, "message": transition.message}]}

    missing = await _check_sp_publish_readiness(db, sp=sp)
    if missing:
        return {**base, "ready": False, "blocker_code": "publish_blocked_missing_fields",
                "missing": missing}

    return {**base, "ready": True}


@router.post("/client/{client_id}/sp-recommendations/{sp_id}/publish")
async def publish_sp(
    client_id: str,
    sp_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    sp = (await db.execute(
        select(SPRecommendation).where(SPRecommendation.id == sp_id, SPRecommendation.client_id == client_id)
    )).scalar_one_or_none()
    if not sp:
        raise HTTPException(status_code=404, detail="SP recommendation not found")
    res = validate_publish_transition(sp.status)
    if not res.allowed:
        _raise_publish_transition(res)

    # CHA-SP hub Round 3: content checklist must be clean before
    # publish (mirror of PG Round 4). Empty SPs leaving farmers
    # subscribed to a no-op recommendation is the failure mode this
    # gate prevents.
    missing = await _check_sp_publish_readiness(db, sp=sp)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "publish_blocked_missing_fields",
                "message": "Cannot publish — checklist not clean.",
                "missing": missing,
            },
        )

    # Sibling-deactivation scoped by (client, crop, specific_problem)
    # — pre-Round-3 it scoped only by sp_cosh_id which would have
    # failed if two crops happen to share an sp_cosh_id (they
    # shouldn't post-Round-1, but defensive).
    prev = (await db.execute(
        select(SPRecommendation).where(
            SPRecommendation.specific_problem_cosh_id == sp.specific_problem_cosh_id,
            SPRecommendation.crop_cosh_id == sp.crop_cosh_id,
            SPRecommendation.client_id == client_id,
            SPRecommendation.status == "ACTIVE",
            SPRecommendation.id != sp.id,
        )
    )).scalars().all()
    for p in prev:
        p.status = "INACTIVE"

    sp.version = compute_publish_version(
        current_version=sp.version, was_published=sp.status != "DRAFT",
    )
    sp.status = "ACTIVE"
    await db.commit()
    await db.refresh(sp)
    return sp


# ── CCA Hub list endpoints (2026-05-10) ─────────────────────────────────────
# Four screens — Crops, Packages, Timelines, Practices — that the SE can
# navigate independently or via drill-down. Each endpoint returns the
# denormalised join needed to render its row without N+1 follow-ups.
# Filter chips (?crop_cosh_id=, ?package_id=, ?timeline_id=) follow the
# user's selection and are independently clearable.

async def _crop_names_by_cosh_id(db: AsyncSession, cosh_ids: set[str]) -> dict[str, str]:
    """Look up English names for a set of biological_name cosh_ids.
    Returns {cosh_id: name_en} — missing cosh_ids are absent (caller
    falls back to the raw id)."""
    if not cosh_ids:
        return {}
    rows = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id.in_(cosh_ids),
            CoshCoreItem.core_type == COSH_BIOLOGICAL_NAMES_CORE,
        )
    )).scalars().all()
    return {
        r.cosh_id: (r.translations or {}).get("en", r.cosh_id)
        for r in rows
    }


@router.get("/client/{client_id}/cca/crops")
async def cca_list_crops(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crop-level list for the four-screen CCA hub. Each row carries a
    package-status breakdown ({DRAFT, ACTIVE, INACTIVE}) so the SE can
    see at a glance which crops have advisory work in flight."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    crops = (await db.execute(
        select(ClientCrop)
        .where(ClientCrop.client_id == client_id, ClientCrop.removed_at.is_(None))
        .order_by(ClientCrop.crop_name_en)
    )).scalars().all()

    counts_q = (await db.execute(
        select(Package.crop_cosh_id, Package.status, func.count())
        .where(Package.client_id == client_id)
        .group_by(Package.crop_cosh_id, Package.status)
    )).all()
    counts: dict[str, dict[str, int]] = {}
    for crop_cosh_id, status, n in counts_q:
        counts.setdefault(crop_cosh_id, {})[status.value if hasattr(status, "value") else str(status)] = n

    last_q = (await db.execute(
        select(Package.crop_cosh_id, func.max(Package.updated_at))
        .where(Package.client_id == client_id)
        .group_by(Package.crop_cosh_id)
    )).all()
    last_edited = {crop_cosh_id: ts for crop_cosh_id, ts in last_q}

    # Fall back to Cosh's biological_names translations when the
    # per-client snapshot is empty — keeps the SE view friendly even
    # for crops registered before snapshots were captured or via
    # factory-seeded test fixtures.
    crop_name_fallback = await _crop_names_by_cosh_id(
        db, {c.crop_cosh_id for c in crops if not c.crop_name_en},
    )
    return [
        {
            "crop_cosh_id": c.crop_cosh_id,
            "name_en": (
                c.crop_name_en
                or crop_name_fallback.get(c.crop_cosh_id)
                or c.crop_cosh_id
            ),
            "area_or_plant": c.crop_area_or_plant,
            "added_at": c.added_at,
            "package_counts": counts.get(c.crop_cosh_id, {}),
            "last_edited": last_edited.get(c.crop_cosh_id),
        }
        for c in crops
    ]


@router.get("/client/{client_id}/cca/packages")
async def cca_list_packages(
    client_id: str,
    crop_cosh_id: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Package-level list with denormalised crop name + counts of
    timelines and locations + last-edited. Filter chips:
    ?crop_cosh_id= and ?status= (DRAFT/ACTIVE/INACTIVE)."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    q = select(Package).where(Package.client_id == client_id)
    if crop_cosh_id:
        q = q.where(Package.crop_cosh_id == crop_cosh_id)
    if status:
        try:
            q = q.where(Package.status == PackageStatus(status))
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_status", "message": f"Unknown status {status!r}"},
            )
    q = q.order_by(Package.updated_at.desc().nullslast(), Package.created_at.desc())

    packages = (await db.execute(q)).scalars().all()
    if not packages:
        return []

    pids = [p.id for p in packages]
    tl_counts = dict((await db.execute(
        select(Timeline.package_id, func.count())
        .where(Timeline.package_id.in_(pids))
        .group_by(Timeline.package_id)
    )).all())
    from app.modules.advisory.models import PackageLocation
    loc_counts = dict((await db.execute(
        select(PackageLocation.package_id, func.count())
        .where(PackageLocation.package_id.in_(pids))
        .group_by(PackageLocation.package_id)
    )).all())

    crop_ids = {p.crop_cosh_id for p in packages}
    crop_names = await _crop_names_by_cosh_id(db, crop_ids)

    return [
        {
            "id": p.id,
            "name": p.name,
            "crop_cosh_id": p.crop_cosh_id,
            "crop_name_en": crop_names.get(p.crop_cosh_id, p.crop_cosh_id),
            "package_type": p.package_type.value,
            "status": p.status.value,
            "version": p.version,
            "duration_days": p.duration_days,
            "description": p.description,
            "timeline_count": tl_counts.get(p.id, 0),
            "location_count": loc_counts.get(p.id, 0),
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }
        for p in packages
    ]


@router.get("/client/{client_id}/cca/timelines")
async def cca_list_timelines(
    client_id: str,
    crop_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cross-package timeline list with denormalised package + crop
    info and a practice count per timeline. Filter chips:
    ?crop_cosh_id= and ?package_id=."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    q = (
        select(Timeline, Package)
        .join(Package, Timeline.package_id == Package.id)
        .where(Package.client_id == client_id)
    )
    if crop_cosh_id:
        q = q.where(Package.crop_cosh_id == crop_cosh_id)
    if package_id:
        q = q.where(Timeline.package_id == package_id)
    q = q.order_by(Package.name, Timeline.display_order, Timeline.from_value)

    rows = (await db.execute(q)).all()
    if not rows:
        return []

    tl_ids = [tl.id for tl, _ in rows]
    practice_counts = dict((await db.execute(
        select(Practice.timeline_id, func.count())
        .where(Practice.timeline_id.in_(tl_ids))
        .group_by(Practice.timeline_id)
    )).all())

    crop_ids = {p.crop_cosh_id for _, p in rows}
    crop_names = await _crop_names_by_cosh_id(db, crop_ids)

    return [
        {
            "id": tl.id,
            "name": tl.name,
            "from_type": tl.from_type.value if hasattr(tl.from_type, "value") else str(tl.from_type),
            "from_value": tl.from_value,
            "to_value": tl.to_value,
            "display_order": tl.display_order,
            "package_id": pkg.id,
            "package_name": pkg.name,
            "package_status": pkg.status.value,
            "crop_cosh_id": pkg.crop_cosh_id,
            "crop_name_en": crop_names.get(pkg.crop_cosh_id, pkg.crop_cosh_id),
            "practice_count": practice_counts.get(tl.id, 0),
        }
        for tl, pkg in rows
    ]


@router.get("/client/{client_id}/cca/practices")
async def cca_list_practices(
    client_id: str,
    crop_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    timeline_id: Optional[str] = None,
    l0: Optional[str] = None,
    l1: Optional[str] = None,
    l2: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cross-timeline practice list with denormalised timeline / package
    / crop + brand + dosage summary. Filter chips at every level plus
    L0/L1/L2 type filters. Paginated (default limit 100). Cross-cutting
    queries like 'all practices in any package using brand X' work by
    scoping with ?crop_cosh_id=...&l1=PESTICIDE — the brand-level filter
    falls out of the L2-bound element list."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    if limit < 1 or limit > 500:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_limit", "message": "limit must be 1..500"},
        )
    if offset < 0:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_offset", "message": "offset must be >= 0"},
        )

    q = (
        select(Practice, Timeline, Package)
        .join(Timeline, Practice.timeline_id == Timeline.id)
        .join(Package, Timeline.package_id == Package.id)
        .where(Package.client_id == client_id)
    )
    if crop_cosh_id:
        q = q.where(Package.crop_cosh_id == crop_cosh_id)
    if package_id:
        q = q.where(Timeline.package_id == package_id)
    if timeline_id:
        q = q.where(Practice.timeline_id == timeline_id)
    if l0:
        q = q.where(Practice.l0_type == l0)
    if l1:
        q = q.where(Practice.l1_type == l1)
    if l2:
        q = q.where(Practice.l2_type == l2)

    total = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar() or 0

    q = q.order_by(Package.name, Timeline.display_order, Practice.display_order)
    q = q.offset(offset).limit(limit)

    rows = (await db.execute(q)).all()
    if not rows:
        return {"items": [], "total": total, "limit": limit, "offset": offset}

    practice_ids = [pr.id for pr, _, _ in rows]
    elements_by_practice: dict[str, list[Element]] = {}
    if practice_ids:
        elem_rows = (await db.execute(
            select(Element)
            .where(Element.practice_id.in_(practice_ids))
            .order_by(Element.display_order)
        )).scalars().all()
        for e in elem_rows:
            elements_by_practice.setdefault(e.practice_id, []).append(e)

    crop_ids = {p.crop_cosh_id for _, _, p in rows}
    crop_names = await _crop_names_by_cosh_id(db, crop_ids)

    items = []
    for practice, timeline, package in rows:
        elements = elements_by_practice.get(practice.id, [])
        # Pull a few key elements for the summary column.
        common_name = next(
            (e.cosh_ref for e in elements if e.element_type == "COMMON_NAME" and e.cosh_ref),
            None,
        )
        brand_cosh_id = next(
            (e.cosh_ref for e in elements if e.element_type == "BRAND_NAME" and e.cosh_ref),
            None,
        )
        dosage = next(
            (e for e in elements if e.element_type == "DOSAGE" and (e.value or e.cosh_ref)),
            None,
        )
        dosage_summary = (
            f"{dosage.value} {dosage.unit_cosh_id}" if dosage and dosage.unit_cosh_id
            else (dosage.value if dosage else None)
        )
        items.append({
            "id": practice.id,
            "l0_type": practice.l0_type.value if hasattr(practice.l0_type, "value") else str(practice.l0_type),
            "l1_type": practice.l1_type,
            "l2_type": practice.l2_type,
            "display_order": practice.display_order,
            "is_special_input": practice.is_special_input,
            "frequency_days": practice.frequency_days,
            "common_name_cosh_id": common_name,
            "brand_cosh_id": brand_cosh_id,
            "dosage_summary": dosage_summary,
            "timeline_id": timeline.id,
            "timeline_name": timeline.name,
            "package_id": package.id,
            "package_name": package.name,
            "package_status": package.status.value,
            "crop_cosh_id": package.crop_cosh_id,
            "crop_name_en": crop_names.get(package.crop_cosh_id, package.crop_cosh_id),
        })

    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ── CHA Hub list endpoints (2026-05-10) ─────────────────────────────────────
# Mirror of /cca/* for Problem-Group recommendations. Four screens:
# Problems / Recommendations / Timelines / Practices, each with chip
# filters that follow the SE's drill-down. PG is crop-agnostic; the
# bundle dimension is `area_or_plant`.

@router.get("/client/{client_id}/cha/problems")
async def cha_list_problems(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List of Problem-Groups the SE can author against. Each row
    carries the per-bundle status — area-wise / plant-wise — so the
    SE can see at a glance which PGs are complete, in progress, or
    untouched.

    PG list source: Cosh's `problem_groups` Core via
    `app/services/cha_problem_groups.list_problem_groups`
    (Batch 39R-bridge, 2026-05-17)."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.services.cha_problem_groups import list_problem_groups

    pgs = await list_problem_groups(db)

    # Aggregate the company's existing recommendations by (PG, bundle).
    rec_q = (await db.execute(
        select(
            PGRecommendation.problem_group_cosh_id,
            PGRecommendation.area_or_plant,
            PGRecommendation.status,
        ).where(PGRecommendation.client_id == client_id)
    )).all()
    bundle_status: dict[tuple[str, str], str] = {}
    for pg_id, ap, status in rec_q:
        if ap:
            bundle_status[(pg_id, ap)] = status

    return [
        {
            "cosh_id": pg["cosh_id"],
            "name_en": pg["name_en"],
            "status": pg["status"],
            "area_wise_status": bundle_status.get((pg["cosh_id"], "AREA_WISE")),
            "plant_wise_status": bundle_status.get((pg["cosh_id"], "PLANT_WISE")),
        }
        for pg in pgs
    ]


@router.get("/client/{client_id}/cha/recommendations")
async def cha_list_recommendations(
    client_id: str,
    problem_group_cosh_id: Optional[str] = None,
    area_or_plant: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bundle-level list. One row per (PG × area_or_plant). Chips:
    ?problem_group_cosh_id=, ?area_or_plant=, ?status=. Each row
    denormalises the friendly PG name + a timeline_count so the
    table is rendered without N+1."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.services.cha_problem_groups import list_problem_groups

    q = select(PGRecommendation).where(PGRecommendation.client_id == client_id)
    if problem_group_cosh_id:
        q = q.where(PGRecommendation.problem_group_cosh_id == problem_group_cosh_id)
    if area_or_plant:
        q = q.where(PGRecommendation.area_or_plant == area_or_plant)
    if status:
        q = q.where(PGRecommendation.status == status)
    q = q.order_by(PGRecommendation.created_at.desc())

    recs = (await db.execute(q)).scalars().all()
    if not recs:
        return []

    rec_ids = [r.id for r in recs]
    tl_counts = dict((await db.execute(
        select(Timeline.pg_recommendation_id, func.count())
        .where(Timeline.pg_recommendation_id.in_(rec_ids))
        .group_by(Timeline.pg_recommendation_id)
    )).all())

    pg_names = {p["cosh_id"]: p["name_en"] for p in await list_problem_groups(db)}

    return [
        {
            "id": r.id,
            "problem_group_cosh_id": r.problem_group_cosh_id,
            "problem_group_name_en": pg_names.get(
                r.problem_group_cosh_id, r.problem_group_cosh_id,
            ),
            "area_or_plant": r.area_or_plant,
            "status": r.status,
            "version": r.version,
            "imported_from_global_at": r.imported_from_global_at,
            "timeline_count": tl_counts.get(r.id, 0),
            "created_at": r.created_at,
        }
        for r in recs
    ]


@router.get("/client/{client_id}/cha/timelines")
async def cha_list_timelines(
    client_id: str,
    problem_group_cosh_id: Optional[str] = None,
    recommendation_id: Optional[str] = None,
    area_or_plant: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cross-recommendation timeline list with denormalised PG +
    bundle context + practice count. Chips: ?problem_group_cosh_id=,
    ?recommendation_id=, ?area_or_plant=. Filters out QA-rooted
    timelines (those live under standard_response_id)."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.services.cha_problem_groups import list_problem_groups

    q = (
        select(Timeline, PGRecommendation)
        .join(PGRecommendation, Timeline.pg_recommendation_id == PGRecommendation.id)
        .where(
            PGRecommendation.client_id == client_id,
            Timeline.pg_recommendation_id.isnot(None),
        )
    )
    if problem_group_cosh_id:
        q = q.where(PGRecommendation.problem_group_cosh_id == problem_group_cosh_id)
    if recommendation_id:
        q = q.where(Timeline.pg_recommendation_id == recommendation_id)
    if area_or_plant:
        q = q.where(PGRecommendation.area_or_plant == area_or_plant)
    q = q.order_by(PGRecommendation.problem_group_cosh_id, Timeline.from_value)

    rows = (await db.execute(q)).all()
    if not rows:
        return []

    tl_ids = [tl.id for tl, _ in rows]
    practice_counts = dict((await db.execute(
        select(Practice.timeline_id, func.count())
        .where(Practice.timeline_id.in_(tl_ids))
        .group_by(Practice.timeline_id)
    )).all())

    pg_names = {p["cosh_id"]: p["name_en"] for p in await list_problem_groups(db)}

    return [
        {
            "id": tl.id,
            "name": tl.name,
            "from_type": tl.from_type if isinstance(tl.from_type, str)
            else getattr(tl.from_type, "value", str(tl.from_type)),
            "from_value": tl.from_value,
            "to_value": tl.to_value,
            "recommendation_id": rec.id,
            "problem_group_cosh_id": rec.problem_group_cosh_id,
            "problem_group_name_en": pg_names.get(
                rec.problem_group_cosh_id, rec.problem_group_cosh_id,
            ),
            "area_or_plant": rec.area_or_plant,
            "recommendation_status": rec.status,
            "practice_count": practice_counts.get(tl.id, 0),
        }
        for tl, rec in rows
    ]


@router.get("/client/{client_id}/cha/practices")
async def cha_list_practices(
    client_id: str,
    problem_group_cosh_id: Optional[str] = None,
    recommendation_id: Optional[str] = None,
    timeline_id: Optional[str] = None,
    area_or_plant: Optional[str] = None,
    l1: Optional[str] = None,
    l2: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cross-timeline CHA practice list. Same cross-cutting power as
    /cca/practices — "every PESTICIDE practice in any of our PG
    recommendations" type queries. Paginated."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.services.cha_problem_groups import list_problem_groups

    if limit < 1 or limit > 500:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_limit", "message": "limit must be 1..500"},
        )
    if offset < 0:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_offset", "message": "offset must be >= 0"},
        )

    q = (
        select(Practice, Timeline, PGRecommendation)
        .join(Timeline, Practice.timeline_id == Timeline.id)
        .join(PGRecommendation, Timeline.pg_recommendation_id == PGRecommendation.id)
        .where(
            PGRecommendation.client_id == client_id,
            Timeline.pg_recommendation_id.isnot(None),
        )
    )
    if problem_group_cosh_id:
        q = q.where(PGRecommendation.problem_group_cosh_id == problem_group_cosh_id)
    if recommendation_id:
        q = q.where(Timeline.pg_recommendation_id == recommendation_id)
    if timeline_id:
        q = q.where(Practice.timeline_id == timeline_id)
    if area_or_plant:
        q = q.where(PGRecommendation.area_or_plant == area_or_plant)
    if l1:
        q = q.where(Practice.l1_type == l1)
    if l2:
        q = q.where(Practice.l2_type == l2)

    total = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar() or 0

    q = q.order_by(
        PGRecommendation.problem_group_cosh_id, Timeline.from_value,
        Practice.display_order,
    ).offset(offset).limit(limit)

    rows = (await db.execute(q)).all()
    if not rows:
        return {"items": [], "total": total, "limit": limit, "offset": offset}

    practice_ids = [pr.id for pr, _, _ in rows]
    elements_by_practice: dict[str, list[Element]] = {}
    if practice_ids:
        elem_rows = (await db.execute(
            select(Element)
            .where(Element.practice_id.in_(practice_ids))
            .order_by(Element.display_order)
        )).scalars().all()
        for e in elem_rows:
            elements_by_practice.setdefault(e.practice_id, []).append(e)

    pg_names = {p["cosh_id"]: p["name_en"] for p in await list_problem_groups(db)}

    items = []
    for practice, timeline, rec in rows:
        elements = elements_by_practice.get(practice.id, [])
        brand = next(
            (e.cosh_ref for e in elements if e.element_type == "BRAND_NAME" and e.cosh_ref),
            None,
        )
        dosage = next(
            (e for e in elements if e.element_type == "DOSAGE" and (e.value or e.cosh_ref)),
            None,
        )
        dosage_summary = (
            f"{dosage.value} {dosage.unit_cosh_id}" if dosage and dosage.unit_cosh_id
            else (dosage.value if dosage else None)
        )
        items.append({
            "id": practice.id,
            "l0_type": practice.l0_type if isinstance(practice.l0_type, str)
            else getattr(practice.l0_type, "value", str(practice.l0_type)),
            "l1_type": practice.l1_type,
            "l2_type": practice.l2_type,
            "is_special_input": practice.is_special_input,
            "frequency_days": practice.frequency_days,
            "brand_cosh_id": brand,
            "dosage_summary": dosage_summary,
            "timeline_id": timeline.id,
            "timeline_name": timeline.name,
            "recommendation_id": rec.id,
            "problem_group_cosh_id": rec.problem_group_cosh_id,
            "problem_group_name_en": pg_names.get(
                rec.problem_group_cosh_id, rec.problem_group_cosh_id,
            ),
            "area_or_plant": rec.area_or_plant,
            "recommendation_status": rec.status,
        })

    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ── CHA-SP Hub list endpoints (2026-05-10) ──────────────────────────────────
# Mirror of the CHA-PG hub for Specific Problem recommendations.
# Crop-keyed instead of PG-keyed: SE picks a crop (from the
# CA ∩ CM-CHA-enabled intersection) → picks a specific problem from
# that crop's list → creates a recommendation for (client, crop, SP).

@router.get("/client/{client_id}/cha-sp/eligible-crops")
async def cha_sp_eligible_crops(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crops the SE may author specific-problem recommendations for.

    User locked 2026-05-10: this is the **intersection** of
    (a) crops the CA has shortlisted for the company (`ClientCrop`,
    not soft-removed) and (b) crops the CM has enabled for CHA at
    the platform level (`CropHealthCrop`, status=ACTIVE).

    Surfaced on the SE-side picker AND on the CA-side Setup → Crops
    page (informational: "of your N crops, M have CHA-SP authoring
    enabled by RootsTalk")."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.modules.sync.models import CropHealthCrop

    client_crops = (await db.execute(
        select(ClientCrop).where(
            ClientCrop.client_id == client_id,
            ClientCrop.removed_at.is_(None),
        )
    )).scalars().all()
    client_set = {c.crop_cosh_id for c in client_crops}

    health_rows = (await db.execute(
        select(CropHealthCrop).where(CropHealthCrop.status == "ACTIVE")
    )).scalars().all()
    health_set = {r.crop_cosh_id for r in health_rows}

    eligible = client_set & health_set

    crop_names = await _crop_names_by_cosh_id(db, eligible)

    return [
        {
            "crop_cosh_id": cid,
            "name_en": crop_names.get(cid, cid),
            "is_eligible": True,
        }
        for cid in sorted(eligible, key=lambda c: crop_names.get(c, c).lower())
    ]


@router.get("/client/{client_id}/cha-sp/specific-problems")
async def cha_sp_specific_problems(
    client_id: str,
    crop_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """V1 stopgap — list of specific problems for the given crop,
    drawn from `cha_specific_problems._SPECIFIC_PROBLEMS_V1`. When
    Cosh ships the `specific_problem` Connect, swap the source in
    `list_specific_problems_for_crop()`; this endpoint stays.

    Each row carries a `taken_by_recommendation_id` when an SP
    bundle for (this client, crop, SP) already exists, so the SE
    sees at a glance which problems have been authored against and
    can navigate straight into the existing bundle."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.services.cha_specific_problems import list_specific_problems_for_crop

    problems = list_specific_problems_for_crop(crop_cosh_id)
    if not problems:
        return []

    sp_ids = [p["cosh_id"] for p in problems]
    existing = (await db.execute(
        select(SPRecommendation).where(
            SPRecommendation.client_id == client_id,
            SPRecommendation.crop_cosh_id == crop_cosh_id,
            SPRecommendation.specific_problem_cosh_id.in_(sp_ids),
        )
    )).scalars().all()
    taken: dict[str, dict] = {
        e.specific_problem_cosh_id: {
            "id": e.id, "status": e.status, "version": e.version,
        } for e in existing
    }

    return [
        {**p, "existing": taken.get(p["cosh_id"])}
        for p in problems
    ]


@router.get("/client/{client_id}/cha-sp/recommendations")
async def cha_sp_list_recommendations(
    client_id: str,
    crop_cosh_id: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SP recommendation list with denormalised crop name + SP name +
    timeline_count. Filter chips: ?crop_cosh_id= and ?status=."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    q = select(SPRecommendation).where(SPRecommendation.client_id == client_id)
    if crop_cosh_id:
        q = q.where(SPRecommendation.crop_cosh_id == crop_cosh_id)
    if status:
        q = q.where(SPRecommendation.status == status)
    q = q.order_by(SPRecommendation.created_at.desc())

    sps = (await db.execute(q)).scalars().all()
    if not sps:
        return []

    sp_ids = [s.id for s in sps]
    tl_counts = dict((await db.execute(
        select(Timeline.sp_recommendation_id, func.count())
        .where(Timeline.sp_recommendation_id.in_(sp_ids))
        .group_by(Timeline.sp_recommendation_id)
    )).all())

    crop_ids = {s.crop_cosh_id for s in sps if s.crop_cosh_id}
    crop_names = await _crop_names_by_cosh_id(db, crop_ids)

    # SP friendly names from the V1 hardcoded list — for any
    # `specific_problem_cosh_id` we don't recognise, fall back to the
    # raw cosh_id.
    from app.services.cha_specific_problems import _SPECIFIC_PROBLEMS_V1
    sp_names: dict[str, str] = {}
    for crop_id, items in _SPECIFIC_PROBLEMS_V1.items():
        for it in items:
            sp_names[it["cosh_id"]] = it["name_en"]

    return [
        {
            "id": s.id,
            "specific_problem_cosh_id": s.specific_problem_cosh_id,
            "specific_problem_name_en": sp_names.get(
                s.specific_problem_cosh_id, s.specific_problem_cosh_id,
            ),
            "crop_cosh_id": s.crop_cosh_id,
            "crop_name_en": crop_names.get(s.crop_cosh_id, s.crop_cosh_id) if s.crop_cosh_id else None,
            "status": s.status,
            "version": s.version,
            "timeline_count": tl_counts.get(s.id, 0),
            "created_at": s.created_at,
        }
        for s in sps
    ]


@router.get("/client/{client_id}/cha-sp/timelines")
async def cha_sp_list_timelines(
    client_id: str,
    crop_cosh_id: Optional[str] = None,
    recommendation_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cross-recommendation SP timeline list with denormalised crop +
    SP context + practice count. Chips: ?crop_cosh_id=,
    ?recommendation_id=."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.modules.advisory.models import Practice

    q = (
        select(Timeline, SPRecommendation)
        .join(SPRecommendation, Timeline.sp_recommendation_id == SPRecommendation.id)
        .where(SPRecommendation.client_id == client_id)
    )
    if crop_cosh_id:
        q = q.where(SPRecommendation.crop_cosh_id == crop_cosh_id)
    if recommendation_id:
        q = q.where(Timeline.sp_recommendation_id == recommendation_id)
    q = q.order_by(Timeline.from_value)

    rows = (await db.execute(q)).all()
    if not rows:
        return []

    tl_ids = [tl.id for tl, _ in rows]
    practice_counts = dict((await db.execute(
        select(Practice.timeline_id, func.count())
        .where(Practice.timeline_id.in_(tl_ids))
        .group_by(Practice.timeline_id)
    )).all())

    crop_ids = {sp.crop_cosh_id for _, sp in rows if sp.crop_cosh_id}
    crop_names = await _crop_names_by_cosh_id(db, crop_ids)

    from app.services.cha_specific_problems import _SPECIFIC_PROBLEMS_V1
    sp_names: dict[str, str] = {}
    for _, items in _SPECIFIC_PROBLEMS_V1.items():
        for it in items:
            sp_names[it["cosh_id"]] = it["name_en"]

    return [
        {
            "id": tl.id,
            "name": tl.name,
            "from_type": tl.from_type,
            "from_value": tl.from_value,
            "to_value": tl.to_value,
            "recommendation_id": sp.id,
            "specific_problem_cosh_id": sp.specific_problem_cosh_id,
            "specific_problem_name_en": sp_names.get(
                sp.specific_problem_cosh_id, sp.specific_problem_cosh_id,
            ),
            "crop_cosh_id": sp.crop_cosh_id,
            "crop_name_en": crop_names.get(sp.crop_cosh_id, sp.crop_cosh_id) if sp.crop_cosh_id else None,
            "recommendation_status": sp.status,
            "practice_count": practice_counts.get(tl.id, 0),
        }
        for tl, sp in rows
    ]


@router.get("/client/{client_id}/cha-sp/practices")
async def cha_sp_list_practices(
    client_id: str,
    crop_cosh_id: Optional[str] = None,
    recommendation_id: Optional[str] = None,
    timeline_id: Optional[str] = None,
    l1: Optional[str] = None,
    l2: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cross-timeline SP practice list with brand + dosage summary +
    full breadcrumb. Paginated. Same cross-cutting power as the
    CHA-PG / CCA practice lists."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.modules.advisory.models import Element, Practice

    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail={"code": "invalid_limit", "message": "limit must be 1..500"})
    if offset < 0:
        raise HTTPException(status_code=422, detail={"code": "invalid_offset", "message": "offset must be >= 0"})

    q = (
        select(Practice, Timeline, SPRecommendation)
        .join(Timeline, Practice.timeline_id == Timeline.id)
        .join(SPRecommendation, Timeline.sp_recommendation_id == SPRecommendation.id)
        .where(SPRecommendation.client_id == client_id)
    )
    if crop_cosh_id:
        q = q.where(SPRecommendation.crop_cosh_id == crop_cosh_id)
    if recommendation_id:
        q = q.where(Timeline.sp_recommendation_id == recommendation_id)
    if timeline_id:
        q = q.where(Practice.timeline_id == timeline_id)
    if l1:
        q = q.where(Practice.l1_type == l1)
    if l2:
        q = q.where(Practice.l2_type == l2)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0

    q = q.order_by(SPRecommendation.specific_problem_cosh_id, Timeline.from_value, Practice.display_order)
    q = q.offset(offset).limit(limit)
    rows = (await db.execute(q)).all()
    if not rows:
        return {"items": [], "total": total, "limit": limit, "offset": offset}

    practice_ids = [pr.id for pr, _, _ in rows]
    elements_by_practice: dict[str, list[Element]] = {}
    if practice_ids:
        elem_rows = (await db.execute(
            select(Element).where(Element.practice_id.in_(practice_ids))
            .order_by(Element.display_order)
        )).scalars().all()
        for e in elem_rows:
            elements_by_practice.setdefault(e.practice_id, []).append(e)

    crop_ids = {sp.crop_cosh_id for _, _, sp in rows if sp.crop_cosh_id}
    crop_names = await _crop_names_by_cosh_id(db, crop_ids)

    from app.services.cha_specific_problems import _SPECIFIC_PROBLEMS_V1
    sp_names: dict[str, str] = {}
    for _, items in _SPECIFIC_PROBLEMS_V1.items():
        for it in items:
            sp_names[it["cosh_id"]] = it["name_en"]

    items = []
    for practice, timeline, sp in rows:
        elements = elements_by_practice.get(practice.id, [])
        brand = next(
            (e.cosh_ref for e in elements if e.element_type == "BRAND_NAME" and e.cosh_ref),
            None,
        )
        dosage = next(
            (e for e in elements if e.element_type == "DOSAGE" and (e.value or e.cosh_ref)),
            None,
        )
        dosage_summary = (
            f"{dosage.value} {dosage.unit_cosh_id}" if dosage and dosage.unit_cosh_id
            else (dosage.value if dosage else None)
        )
        items.append({
            "id": practice.id,
            "l0_type": practice.l0_type if isinstance(practice.l0_type, str)
            else getattr(practice.l0_type, "value", str(practice.l0_type)),
            "l1_type": practice.l1_type,
            "l2_type": practice.l2_type,
            "is_special_input": practice.is_special_input,
            "frequency_days": practice.frequency_days,
            "brand_cosh_id": brand,
            "dosage_summary": dosage_summary,
            "timeline_id": timeline.id,
            "timeline_name": timeline.name,
            "recommendation_id": sp.id,
            "specific_problem_cosh_id": sp.specific_problem_cosh_id,
            "specific_problem_name_en": sp_names.get(
                sp.specific_problem_cosh_id, sp.specific_problem_cosh_id,
            ),
            "crop_cosh_id": sp.crop_cosh_id,
            "crop_name_en": crop_names.get(sp.crop_cosh_id, sp.crop_cosh_id) if sp.crop_cosh_id else None,
            "recommendation_status": sp.status,
        })

    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ── QA Hub list endpoints (2026-05-10) ─────────────────────────────────────
# UCAT pipe-3 — Q&A library. Mirror of CCA/CHA hub patterns. Per user
# 2026-05-10: QA crops = CA's full shortlist (no CHA-enabled
# intersection). SE picks a crop first, then authors a Standard
# Response under that crop. Standard Responses themselves use the
# existing /client/{cid}/standard-responses CRUD; this section adds
# the cross-cutting list endpoints + the eligible-crops surface.

@router.get("/client/{client_id}/qa/eligible-crops")
async def qa_eligible_crops(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crops the SE may author Q&A standard responses for. **No CHA
    intersection**: every crop the CA has shortlisted is fair game
    for Q&A authoring (a question may be crop-bound or
    crop-agnostic, but the picker only surfaces the CA's belt
    crops). Plus a synthetic 'Crop-agnostic' entry for questions
    that don't belong to any crop."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    rows = (await db.execute(
        select(ClientCrop).where(
            ClientCrop.client_id == client_id,
            ClientCrop.removed_at.is_(None),
        )
    )).scalars().all()
    cosh_ids = {c.crop_cosh_id for c in rows}
    crop_names = await _crop_names_by_cosh_id(db, cosh_ids)
    return [
        {
            "crop_cosh_id": c.crop_cosh_id,
            "name_en": crop_names.get(c.crop_cosh_id, c.crop_cosh_id),
        }
        for c in sorted(
            rows, key=lambda r: crop_names.get(r.crop_cosh_id, r.crop_cosh_id).lower()
        )
    ]


@router.get("/client/{client_id}/qa/standard-responses")
async def qa_list_standard_responses(
    client_id: str,
    crop_cosh_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Denormalised list of Standard Responses. Adds friendly crop
    name + timeline_count to the rows so the QA · Standard Responses
    screen renders without N+1. Filter chip: ?crop_cosh_id=. The
    special value `__AGNOSTIC__` filters to crop-agnostic SRs
    (question_text without a crop attached)."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.modules.farmpundit.models import StandardResponse

    q = select(StandardResponse).where(StandardResponse.client_id == client_id)
    if crop_cosh_id == "__AGNOSTIC__":
        q = q.where(StandardResponse.crop_cosh_id.is_(None))
    elif crop_cosh_id:
        q = q.where(StandardResponse.crop_cosh_id == crop_cosh_id)
    q = q.order_by(StandardResponse.updated_at.desc())

    srs = (await db.execute(q)).scalars().all()
    if not srs:
        return []

    sr_ids = [s.id for s in srs]
    tl_counts = dict((await db.execute(
        select(Timeline.standard_response_id, func.count())
        .where(Timeline.standard_response_id.in_(sr_ids))
        .group_by(Timeline.standard_response_id)
    )).all())

    crop_ids = {s.crop_cosh_id for s in srs if s.crop_cosh_id}
    crop_names = await _crop_names_by_cosh_id(db, crop_ids)

    return [
        {
            "id": s.id,
            "question_text": s.question_text,
            "crop_cosh_id": s.crop_cosh_id,
            "crop_name_en": (
                crop_names.get(s.crop_cosh_id, s.crop_cosh_id)
                if s.crop_cosh_id else None
            ),
            "timeline_count": tl_counts.get(s.id, 0),
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
        for s in srs
    ]


@router.get("/client/{client_id}/qa/timelines")
async def qa_list_timelines(
    client_id: str,
    standard_response_id: Optional[str] = None,
    crop_cosh_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cross-SR timeline list. Walks the polymorphic `pg_timelines`
    table for rows with `standard_response_id IS NOT NULL` (excluding
    PG-rooted rows). Chips: ?standard_response_id=, ?crop_cosh_id=."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.modules.farmpundit.models import StandardResponse

    q = (
        select(Timeline, StandardResponse)
        .join(StandardResponse, Timeline.standard_response_id == StandardResponse.id)
        .where(StandardResponse.client_id == client_id)
    )
    if standard_response_id:
        q = q.where(Timeline.standard_response_id == standard_response_id)
    if crop_cosh_id == "__AGNOSTIC__":
        q = q.where(StandardResponse.crop_cosh_id.is_(None))
    elif crop_cosh_id:
        q = q.where(StandardResponse.crop_cosh_id == crop_cosh_id)
    q = q.order_by(Timeline.from_value)

    rows = (await db.execute(q)).all()
    if not rows:
        return []

    tl_ids = [tl.id for tl, _ in rows]
    practice_counts = dict((await db.execute(
        select(Practice.timeline_id, func.count())
        .where(Practice.timeline_id.in_(tl_ids))
        .group_by(Practice.timeline_id)
    )).all())

    crop_ids = {sr.crop_cosh_id for _, sr in rows if sr.crop_cosh_id}
    crop_names = await _crop_names_by_cosh_id(db, crop_ids)

    return [
        {
            "id": tl.id,
            "name": tl.name,
            "from_type": tl.from_type if isinstance(tl.from_type, str)
            else getattr(tl.from_type, "value", str(tl.from_type)),
            "from_value": tl.from_value,
            "to_value": tl.to_value,
            "standard_response_id": sr.id,
            "question_text": sr.question_text,
            "crop_cosh_id": sr.crop_cosh_id,
            "crop_name_en": (
                crop_names.get(sr.crop_cosh_id, sr.crop_cosh_id)
                if sr.crop_cosh_id else None
            ),
            "practice_count": practice_counts.get(tl.id, 0),
        }
        for tl, sr in rows
    ]


@router.get("/client/{client_id}/qa/practices")
async def qa_list_practices(
    client_id: str,
    standard_response_id: Optional[str] = None,
    crop_cosh_id: Optional[str] = None,
    timeline_id: Optional[str] = None,
    l1: Optional[str] = None,
    l2: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cross-timeline QA practice list. Same shape as /cha/practices
    and /cha-sp/practices. Paginated."""
    await _assert_can_view_client_advisory(db, current_user.id, client_id)
    from app.modules.farmpundit.models import StandardResponse

    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail={"code": "invalid_limit", "message": "limit must be 1..500"})
    if offset < 0:
        raise HTTPException(status_code=422, detail={"code": "invalid_offset", "message": "offset must be >= 0"})

    q = (
        select(Practice, Timeline, StandardResponse)
        .join(Timeline, Practice.timeline_id == Timeline.id)
        .join(StandardResponse, Timeline.standard_response_id == StandardResponse.id)
        .where(StandardResponse.client_id == client_id)
    )
    if standard_response_id:
        q = q.where(Timeline.standard_response_id == standard_response_id)
    if crop_cosh_id == "__AGNOSTIC__":
        q = q.where(StandardResponse.crop_cosh_id.is_(None))
    elif crop_cosh_id:
        q = q.where(StandardResponse.crop_cosh_id == crop_cosh_id)
    if timeline_id:
        q = q.where(Practice.timeline_id == timeline_id)
    if l1:
        q = q.where(Practice.l1_type == l1)
    if l2:
        q = q.where(Practice.l2_type == l2)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0

    q = q.order_by(StandardResponse.created_at.desc(), Timeline.from_value, Practice.display_order)
    q = q.offset(offset).limit(limit)

    rows = (await db.execute(q)).all()
    if not rows:
        return {"items": [], "total": total, "limit": limit, "offset": offset}

    practice_ids = [pr.id for pr, _, _ in rows]
    elements_by_practice: dict[str, list[Element]] = {}
    if practice_ids:
        elem_rows = (await db.execute(
            select(Element).where(Element.practice_id.in_(practice_ids))
            .order_by(Element.display_order)
        )).scalars().all()
        for e in elem_rows:
            elements_by_practice.setdefault(e.practice_id, []).append(e)

    crop_ids = {sr.crop_cosh_id for _, _, sr in rows if sr.crop_cosh_id}
    crop_names = await _crop_names_by_cosh_id(db, crop_ids)

    items = []
    for practice, timeline, sr in rows:
        elements = elements_by_practice.get(practice.id, [])
        brand = next(
            (e.cosh_ref for e in elements if e.element_type == "BRAND_NAME" and e.cosh_ref),
            None,
        )
        dosage = next(
            (e for e in elements if e.element_type == "DOSAGE" and (e.value or e.cosh_ref)),
            None,
        )
        dosage_summary = (
            f"{dosage.value} {dosage.unit_cosh_id}" if dosage and dosage.unit_cosh_id
            else (dosage.value if dosage else None)
        )
        items.append({
            "id": practice.id,
            "l0_type": practice.l0_type if isinstance(practice.l0_type, str)
            else getattr(practice.l0_type, "value", str(practice.l0_type)),
            "l1_type": practice.l1_type,
            "l2_type": practice.l2_type,
            "is_special_input": practice.is_special_input,
            "frequency_days": practice.frequency_days,
            "brand_cosh_id": brand,
            "dosage_summary": dosage_summary,
            "timeline_id": timeline.id,
            "timeline_name": timeline.name,
            "standard_response_id": sr.id,
            "question_text": sr.question_text,
            "crop_cosh_id": sr.crop_cosh_id,
            "crop_name_en": (
                crop_names.get(sr.crop_cosh_id, sr.crop_cosh_id)
                if sr.crop_cosh_id else None
            ),
        })

    return {"items": items, "total": total, "limit": limit, "offset": offset}
