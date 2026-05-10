from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import StatusEnum, User
from app.modules.advisory.models import (
    Package, PackageLocation, PackageAuthor, PackageVariable,
    Parameter, Variable, PackageVariable,
    ParameterTranslation, VariableTranslation, TranslationStatus,
    Timeline, Practice, Element, Relation,
    ConditionalQuestion, PracticeConditional, RelationConditional,
    PackageStatus, PackageType,
)
from app.modules.advisory.schemas import (
    PackageCreate, PackageUpdate, PackageOut,
    PackageLocationIn, PackageAuthorIn, PackageAuthorOut,
    ParameterCreate, VariableCreate, PackageVariableSet,
    TimelineCreate, TimelineUpdate, TimelineOut,
    PracticeCreate, PracticeOut,
    RelationCreate, ConditionalQuestionCreate, PracticeConditionalCreate,
    PGRecommendationCreate, PGRecommendationOut, PGTimelineCreate, PGTimelineOut, PGPracticeCreate,
    SPRecommendationCreate, SPRecommendationOut, SPTimelineCreate, SPTimelineOut, SPPracticeCreate,
    QATimelineCreate, QAPracticeCreate,
    ElementIn,
)
from app.modules.advisory.models import (
    PGRecommendation, PGTimeline, PGPractice, PGElement,
    SPRecommendation, SPTimeline, SPPractice, SPElement,
)
from app.modules.clients.models import ClientCrop, ClientUser, ClientUserRole
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
)
from app.services.timeline_validation import (
    TimelineValidationError, validate_timeline,
)
from app.services.relations import PracticeRef
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


# ── Element-level CRUD helpers (Round 2 — element-level authoring) ─────────

def _element_row_to_in(row) -> ElementIn:
    """Coerce a persisted Element / PGElement / SPElement row back into
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
    pkg = await _get_package(db, package_id, client_id)
    return pkg


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

    pkg.version = compute_publish_version(
        current_version=pkg.version, was_published=pkg.published_at is not None,
    )
    pkg.status = PackageStatus.ACTIVE
    pkg.published_at = datetime.now(timezone.utc)
    pkg.published_by = current_user.id
    await db.commit()
    await db.refresh(pkg)
    return pkg


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
            designation=pa.designation,
            professional_profile=pa.professional_profile,
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
            designation=a.designation,
            professional_profile=a.professional_profile,
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
    result = await db.execute(
        select(Parameter).where(
            Parameter.crop_cosh_id == crop_cosh_id,
            Parameter.client_id == client_id,
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
    from app.modules.advisory.models import ParameterSource
    param = Parameter(
        crop_cosh_id=request.crop_cosh_id,
        client_id=client_id,
        name=request.name,
        source=ParameterSource.CUSTOM,
        display_order=request.display_order,
    )
    db.add(param)
    await db.commit()
    await db.refresh(param)
    return param


@router.get("/client/{client_id}/parameters/{parameter_id}/variables")
async def list_variables(
    client_id: str, parameter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Variable).where(Variable.parameter_id == parameter_id).order_by(Variable.created_at)
    )
    return result.scalars().all()


@router.post("/client/{client_id}/parameters/{parameter_id}/variables", status_code=201)
async def create_variable(
    client_id: str, parameter_id: str,
    request: VariableCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Validate min 2 variables enforced at list level
    var = Variable(parameter_id=parameter_id, name=request.name)
    db.add(var)
    await db.commit()
    await db.refresh(var)
    return var


# ── Custom Parameters: extended CRUD (status, edit, translation) ─────────────

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
    """Edit variable text. Resets all its translations to PENDING_REVIEW per spec A1.4."""
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
    pkg = await _get_package(db, package_id, client_id)
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

@router.get("/client/{client_id}/timelines/{timeline_id}/practices", response_model=list[PracticeOut])
async def list_practices(
    client_id: str, timeline_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Practice).where(Practice.timeline_id == timeline_id).order_by(Practice.display_order)
    )
    return result.scalars().all()


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
    integrity, special-input / frequency-based / plant-wise invariants)."""
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
        timeline_id=timeline_id,
        l0_type=request.l0_type,
        l1_type=request.l1_type,
        l2_type=request.l2_type,
        display_order=request.display_order,
        is_special_input=request.is_special_input,
        frequency_days=request.frequency_days,
    )
    db.add(practice)
    await db.flush()

    for i, elem in enumerate(request.elements):
        db.add(Element(practice_id=practice.id, **elem.model_dump()))

    await db.commit()
    await db.refresh(practice)
    return practice


@router.delete("/client/{client_id}/timelines/{timeline_id}/practices/{practice_id}", status_code=204)
async def delete_practice(
    client_id: str, timeline_id: str, practice_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Global packages visible for fork into client scope. Defaults to
    ACTIVE only — DRAFT and INACTIVE rows are hidden from CA-portal
    SEs. CMs can pass `include_drafts=true` to see everything in their
    own admin views."""
    q = select(Package).where(Package.client_id == None)  # noqa: E711
    if not include_drafts:
        q = q.where(Package.status == PackageStatus.ACTIVE)
    q = q.order_by(Package.created_at.desc())
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/advisory/global/packages", response_model=PackageOut, status_code=201)
async def create_global_package(
    request: PackageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
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


@router.post("/advisory/global/packages/{pkg_id}/publish", response_model=PackageOut)
async def publish_global_package(
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
    current_status = pkg.status.value if hasattr(pkg.status, "value") else str(pkg.status)
    res = validate_publish_transition(current_status)
    if not res.allowed:
        _raise_publish_transition(res)
    pkg.version = compute_publish_version(
        current_version=pkg.version, was_published=pkg.published_at is not None,
    )
    pkg.status = PackageStatus.ACTIVE
    pkg.published_at = datetime.now(timezone.utc)
    pkg.published_by = current_user.id
    await db.commit()
    await db.refresh(pkg)
    return pkg


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
    pkg = (await db.execute(
        select(Package).where(Package.id == pkg_id, Package.client_id == None)  # noqa: E711
    )).scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=404, detail="Global package not found")
    _validate_timeline(request)
    tl = Timeline(package_id=pkg_id, **request.model_dump())
    db.add(tl)
    await db.commit()
    await db.refresh(tl)
    return tl


@router.delete("/advisory/global/packages/{pkg_id}/timelines/{tl_id}", status_code=204)
async def delete_global_timeline(
    pkg_id: str, tl_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tl = await _get_timeline(db, tl_id, pkg_id)
    await db.delete(tl)
    await db.commit()


@router.get("/advisory/global/packages/{pkg_id}/timelines/{tl_id}/practices", response_model=list[PracticeOut])
async def list_global_practices(
    pkg_id: str, tl_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Practice).where(Practice.timeline_id == tl_id).order_by(Practice.display_order)
    )
    return result.scalars().all()


@router.post("/advisory/global/packages/{pkg_id}/timelines/{tl_id}/practices", response_model=PracticeOut, status_code=201)
async def create_global_practice(
    pkg_id: str, tl_id: str,
    request: PracticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CCA Step 4 / Batch 4C-i.D: same L2 element rule book validation
    as the client-side create_practice route."""
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
    for elem in request.elements:
        db.add(Element(practice_id=practice.id, **elem.model_dump()))
    await db.commit()
    await db.refresh(practice)
    return practice


@router.delete("/advisory/global/packages/{pkg_id}/timelines/{tl_id}/practices/{practice_id}", status_code=204)
async def delete_global_practice(
    pkg_id: str, tl_id: str, practice_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Practice).where(Practice.id == practice_id, Practice.timeline_id == tl_id))
    practice = result.scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")
    await db.delete(practice)
    await db.commit()


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
    practice = await _load_cca_practice(db, timeline_id=tl_id, practice_id=practice_id)
    await _delete_practice_element(
        db, practice=practice, element_model=Element, element_id=element_id,
    )


@router.post("/client/{client_id}/packages/{pkg_id}/fork", response_model=PackageOut, status_code=201)
async def fork_global_package(
    client_id: str,
    pkg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deep-copy a global package (all timelines + practices + elements)
    into a client. **Once-per-client-lifetime rule applies** — a
    given global package can be forked into a given client at most
    once. After the fork, the local copy lives entirely independently;
    re-forking the same global is permanently 409. The SE either edits
    the existing local copy or (separately) deletes it before any
    re-import is even possible.

    Authorisation:
      Only a CM with an active EDIT-rights `CMClientAssignment` for
      this client may fork. Any other caller — including SEs at the
      same client — gets 403.

    Publish gate: only ACTIVE global packages may be forked. DRAFT
    is CM work-in-progress; INACTIVE is a superseded version.
    """
    await _assert_cm_can_edit_client(db, current_user.id, client_id)

    src = (await db.execute(
        select(Package).where(Package.id == pkg_id, Package.client_id == None)  # noqa: E711
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
                    "forking."
                ),
                "current_status": src.status.value if hasattr(src.status, "value") else src.status,
            },
        )

    existing = (await db.execute(
        select(Package).where(
            Package.client_id == client_id,
            Package.parent_global_id == pkg_id,
        )
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
                "code": "package_already_forked",
                "message": (
                    "This global package has already been forked into "
                    "this client. A package can be forked into a client "
                    "only once in the client's lifetime — edit the "
                    "existing local copy or delete it before any future "
                    "re-import is possible."
                ),
                "existing": {
                    "package_id": existing.id,
                    "timeline_count": tl_count,
                },
            },
        )

    copy = Package(
        client_id=client_id,
        parent_global_id=src.id,
        crop_cosh_id=src.crop_cosh_id,
        name=src.name,
        package_type=src.package_type,
        duration_days=src.duration_days,
        start_date_label_cosh_id=src.start_date_label_cosh_id,
        description=src.description,
        created_by=current_user.id,
    )
    db.add(copy)
    await db.flush()

    # Load source timelines + practices + elements
    tl_result = await db.execute(
        select(Timeline).where(Timeline.package_id == src.id).order_by(Timeline.display_order)
    )
    for src_tl in tl_result.scalars().all():
        new_tl = Timeline(
            package_id=copy.id,
            name=src_tl.name,
            from_type=src_tl.from_type,
            from_value=src_tl.from_value,
            to_value=src_tl.to_value,
            display_order=src_tl.display_order,
        )
        db.add(new_tl)
        await db.flush()

        p_result = await db.execute(
            select(Practice).where(Practice.timeline_id == src_tl.id).order_by(Practice.display_order)
        )
        for src_p in p_result.scalars().all():
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

            el_result = await db.execute(
                select(Element).where(Element.practice_id == src_p.id).order_by(Element.display_order)
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

    await db.commit()
    await db.refresh(copy)
    return copy


# ── Global PG Recommendations ──────────────────────────────────────────────────

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


@router.post("/advisory/global/pg-recommendations/{pg_id}/timelines", status_code=201)
async def add_global_pg_timeline(
    pg_id: str,
    request: PGTimelineCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pg = (await db.execute(
        select(PGRecommendation).where(PGRecommendation.id == pg_id)
    )).scalar_one_or_none()
    if not pg:
        raise HTTPException(status_code=404, detail="PG recommendation not found")
    tl = PGTimeline(
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
    """CCA Step 4 / Batch 4C-i.D: L2 element rule book validation also
    applies to PG-recommendation practices — same shape as PoP."""
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

    practice = PGPractice(
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
        db.add(PGElement(
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


# ── CHA global-PG per-element CRUD (Round 2) ───────────────────────────────

async def _load_pg_practice_by_timeline(db, *, timeline_id: str, practice_id: str):
    practice = (await db.execute(
        select(PGPractice).where(
            PGPractice.id == practice_id,
            PGPractice.timeline_id == timeline_id,
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
    practice = await _load_pg_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    new = await _add_practice_element(
        db, practice=practice, element_model=PGElement, body=body,
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
    practice = await _load_pg_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    updated = await _update_practice_element(
        db, practice=practice, element_model=PGElement,
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
    practice = await _load_pg_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    await _delete_practice_element(
        db, practice=practice, element_model=PGElement, element_id=element_id,
    )


@router.delete("/advisory/global/pg-recommendations/{pg_id}/timelines/{tl_id}", status_code=204)
async def delete_global_pg_timeline(
    pg_id: str,
    tl_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tl = (await db.execute(
        select(PGTimeline).where(PGTimeline.id == tl_id, PGTimeline.pg_recommendation_id == pg_id)
    )).scalar_one_or_none()
    if tl:
        await db.delete(tl)
        await db.commit()


@router.post("/advisory/global/pg-recommendations/{pg_id}/publish")
async def publish_global_pg(
    pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pg = (await db.execute(
        select(PGRecommendation).where(PGRecommendation.id == pg_id)
    )).scalar_one_or_none()
    if not pg:
        raise HTTPException(status_code=404, detail="PG recommendation not found")
    res = validate_publish_transition(pg.status)
    if not res.allowed:
        _raise_publish_transition(res)

    # Deactivate previous active version for same problem_group + client
    prev = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.problem_group_cosh_id == pg.problem_group_cosh_id,
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


# ── Client PG Recommendations ──────────────────────────────────────────────────

@router.get("/client/{client_id}/pg-recommendations", response_model=list[PGRecommendationOut])
async def list_client_pg(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
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
      PG list (will become a Cosh-Connect membership check once the
      `problem_group` Connect ships)."""
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

    if not is_known_problem_group(request.problem_group_cosh_id):
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
        select(PGTimeline).where(PGTimeline.pg_recommendation_id == src_pg.id)
    )
    for src_tl in tl_result.scalars().all():
        new_tl = PGTimeline(
            pg_recommendation_id=target_pg.id,
            name=src_tl.name,
            from_type=src_tl.from_type,
            from_value=src_tl.from_value,
            to_value=src_tl.to_value,
        )
        db.add(new_tl)
        await db.flush()

        p_result = await db.execute(
            select(PGPractice).where(PGPractice.timeline_id == src_tl.id)
        )
        for src_p in p_result.scalars().all():
            new_p = PGPractice(
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
                select(PGElement).where(PGElement.practice_id == src_p.id)
            )
            for src_el in el_result.scalars().all():
                db.add(PGElement(
                    practice_id=new_p.id,
                    element_type=src_el.element_type,
                    cosh_ref=src_el.cosh_ref,
                    value=src_el.value,
                    unit_cosh_id=src_el.unit_cosh_id,
                    display_order=src_el.display_order,
                ))


async def _wipe_pg_content(db: AsyncSession, pg_id: str) -> dict:
    """Delete every PGTimeline / PGPractice / PGElement under a PG.
    Returns counts so the caller can report what was overwritten."""
    timelines = (await db.execute(
        select(PGTimeline).where(PGTimeline.pg_recommendation_id == pg_id)
    )).scalars().all()
    tl_count = len(timelines)
    practice_count = 0
    element_count = 0
    for tl in timelines:
        practices = (await db.execute(
            select(PGPractice).where(PGPractice.timeline_id == tl.id)
        )).scalars().all()
        practice_count += len(practices)
        for p in practices:
            elements = (await db.execute(
                select(PGElement).where(PGElement.practice_id == p.id)
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
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deep-copy a global PG recommendation into a client.

    First import: creates a fresh local PGRecommendation linked to the
    global via parent_id, with all timelines / practices / elements
    deep-copied.

    Re-import (existing local copy detected):
      • Default — refuses with 409 + structured `existing` summary so
        the CA portal can show the SE a "this will overwrite your
        local edits" warning.
      • `?force=true` — wipes the existing local copy's content and
        re-imports fresh from the global. Local copy keeps its same
        primary key (so triggered references stay intact); only its
        timelines / practices / elements are replaced.
    """
    await _assert_cm_can_edit_client(db, current_user.id, client_id)

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
    # Either signals the source isn't fit for client consumption.
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

    existing = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.client_id == client_id,
            PGRecommendation.parent_id == global_pg_id,
        )
    )).scalar_one_or_none()

    if existing and not force:
        # Tally what would be overwritten so the CA portal can show
        # a meaningful confirmation dialog.
        tl_count = (await db.execute(
            select(func.count()).select_from(PGTimeline).where(
                PGTimeline.pg_recommendation_id == existing.id,
            )
        )).scalar() or 0
        raise HTTPException(
            status_code=409,
            detail={
                "code": "import_would_overwrite",
                "message": (
                    "This PG recommendation is already imported. Re-importing "
                    "will overwrite the existing local copy's timelines and "
                    "practices. Send force=true to confirm."
                ),
                "existing": {
                    "pg_recommendation_id": existing.id,
                    "timeline_count": tl_count,
                },
            },
        )

    if existing:
        # Force-overwrite path: wipe existing content + reuse the row.
        await _wipe_pg_content(db, existing.id)
        target = existing
    else:
        target = PGRecommendation(
            problem_group_cosh_id=src.problem_group_cosh_id,
            client_id=client_id,
            parent_id=global_pg_id,
            area_or_plant=src.area_or_plant,
        )
        db.add(target)
        await db.flush()

    await _copy_pg_content_into(db, src_pg=src, target_pg=target)

    await db.commit()
    await db.refresh(target)
    return target


@router.post("/client/{client_id}/pg-recommendations/{pg_id}/publish")
async def publish_client_pg(
    client_id: str,
    pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pg = (await db.execute(
        select(PGRecommendation).where(PGRecommendation.id == pg_id, PGRecommendation.client_id == client_id)
    )).scalar_one_or_none()
    if not pg:
        raise HTTPException(status_code=404, detail="PG recommendation not found")
    res = validate_publish_transition(pg.status)
    if not res.allowed:
        _raise_publish_transition(res)

    prev = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.problem_group_cosh_id == pg.problem_group_cosh_id,
            PGRecommendation.client_id == client_id,
            PGRecommendation.status == "ACTIVE",
            PGRecommendation.id != pg.id,
        )
    )).scalars().all()
    for p in prev:
        p.status = "INACTIVE"

    pg.version = compute_publish_version(
        current_version=pg.version, was_published=pg.status != "DRAFT",
    )
    pg.status = "ACTIVE"
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
    result = await db.execute(
        select(PGTimeline).where(PGTimeline.pg_recommendation_id == pg_id)
    )
    timelines = result.scalars().all()
    out = []
    for tl in timelines:
        p_res = await db.execute(select(PGPractice).where(PGPractice.timeline_id == tl.id).order_by(PGPractice.display_order))
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
    tl = PGTimeline(
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

    practice = PGPractice(
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
        db.add(PGElement(
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
    practice = await _load_pg_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    new = await _add_practice_element(
        db, practice=practice, element_model=PGElement, body=body,
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
    practice = await _load_pg_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    updated = await _update_practice_element(
        db, practice=practice, element_model=PGElement,
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
    practice = await _load_pg_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    await _delete_practice_element(
        db, practice=practice, element_model=PGElement, element_id=element_id,
    )


@router.delete("/client/{client_id}/pg-recommendations/{pg_id}/timelines/{tl_id}", status_code=204)
async def delete_client_pg_timeline(
    client_id: str,
    pg_id: str,
    tl_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tl = (await db.execute(
        select(PGTimeline).where(PGTimeline.id == tl_id, PGTimeline.pg_recommendation_id == pg_id)
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
    result = await db.execute(
        select(SPRecommendation).where(SPRecommendation.client_id == client_id)
        .order_by(SPRecommendation.created_at.desc())
    )
    return result.scalars().all()


@router.post("/client/{client_id}/sp-recommendations", response_model=SPRecommendationOut, status_code=201)
async def create_client_sp(
    client_id: str,
    request: SPRecommendationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sp = SPRecommendation(
        specific_problem_cosh_id=request.specific_problem_cosh_id,
        client_id=client_id,
        application_type=request.application_type,
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
    result = await db.execute(select(SPTimeline).where(SPTimeline.sp_recommendation_id == sp_id))
    timelines = result.scalars().all()
    out = []
    for tl in timelines:
        p_res = await db.execute(select(SPPractice).where(SPPractice.timeline_id == tl.id).order_by(SPPractice.display_order))
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
    tl = SPTimeline(
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

    practice = SPPractice(
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
        db.add(SPElement(
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
        select(SPPractice).where(
            SPPractice.id == practice_id,
            SPPractice.timeline_id == timeline_id,
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
    practice = await _load_sp_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    new = await _add_practice_element(
        db, practice=practice, element_model=SPElement, body=body,
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
    practice = await _load_sp_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    updated = await _update_practice_element(
        db, practice=practice, element_model=SPElement,
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
    practice = await _load_sp_practice_by_timeline(db, timeline_id=tl_id, practice_id=practice_id)
    await _delete_practice_element(
        db, practice=practice, element_model=SPElement, element_id=element_id,
    )


@router.delete("/client/{client_id}/sp-recommendations/{sp_id}/timelines/{tl_id}", status_code=204)
async def delete_sp_timeline(
    client_id: str,
    sp_id: str,
    tl_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tl = (await db.execute(
        select(SPTimeline).where(SPTimeline.id == tl_id, SPTimeline.sp_recommendation_id == sp_id)
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
    from app.modules.farmpundit.router import _assert_portal_member
    await _assert_portal_member(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)

    timelines = (await db.execute(
        select(PGTimeline).where(
            PGTimeline.standard_response_id == sr_id,
        ).order_by(PGTimeline.from_value, PGTimeline.id)
    )).scalars().all()

    out = []
    for tl in timelines:
        practices = (await db.execute(
            select(PGPractice).where(PGPractice.timeline_id == tl.id)
            .order_by(PGPractice.display_order)
        )).scalars().all()
        practice_dicts = []
        for p in practices:
            elements = (await db.execute(
                select(PGElement).where(PGElement.practice_id == p.id)
                .order_by(PGElement.display_order)
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
    from app.modules.farmpundit.router import _assert_portal_member
    await _assert_portal_member(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)

    tl = PGTimeline(
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
    from app.modules.farmpundit.router import _assert_portal_member
    await _assert_portal_member(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)

    tl = (await db.execute(
        select(PGTimeline).where(
            PGTimeline.id == tl_id,
            PGTimeline.standard_response_id == sr_id,
        )
    )).scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")

    # Manually delete practices and elements first — SQLAlchemy
    # relationships on PGTimeline don't carry cascade='delete' (it
    # would require a back_populates change that ripples through
    # CHA tests). Mirrors the PG delete pattern though PG's delete
    # endpoint relies on the caller having no practices yet.
    practices = (await db.execute(
        select(PGPractice).where(PGPractice.timeline_id == tl_id)
    )).scalars().all()
    for p in practices:
        elements = (await db.execute(
            select(PGElement).where(PGElement.practice_id == p.id)
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
    from app.modules.farmpundit.router import _assert_portal_member
    await _assert_portal_member(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)

    tl = (await db.execute(
        select(PGTimeline).where(
            PGTimeline.id == tl_id,
            PGTimeline.standard_response_id == sr_id,
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

    practice = PGPractice(
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
        db.add(PGElement(
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
        select(PGElement).where(PGElement.practice_id == practice.id)
        .order_by(PGElement.display_order)
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
    from app.modules.farmpundit.router import _assert_portal_member
    await _assert_portal_member(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)
    tl = (await db.execute(
        select(PGTimeline).where(
            PGTimeline.id == tl_id,
            PGTimeline.standard_response_id == sr_id,
        )
    )).scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")
    practice = (await db.execute(
        select(PGPractice).where(
            PGPractice.id == practice_id,
            PGPractice.timeline_id == tl_id,
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
        db, practice=practice, element_model=PGElement, body=body,
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
        db, practice=practice, element_model=PGElement,
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
        db, practice=practice, element_model=PGElement, element_id=element_id,
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
    from app.modules.farmpundit.router import _assert_portal_member
    await _assert_portal_member(db, current_user.id, client_id)
    await _assert_sr_belongs_to_client(db, sr_id, client_id)

    practice = (await db.execute(
        select(PGPractice).where(
            PGPractice.id == p_id,
            PGPractice.timeline_id == tl_id,
        )
    )).scalar_one_or_none()
    if not practice:
        raise HTTPException(status_code=404, detail="Practice not found")

    # Same parent-walk validation as the timeline endpoints — make
    # sure the timeline really belongs to this Standard Response
    # before deleting under it.
    tl = (await db.execute(
        select(PGTimeline).where(
            PGTimeline.id == tl_id,
            PGTimeline.standard_response_id == sr_id,
        )
    )).scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")

    elements = (await db.execute(
        select(PGElement).where(PGElement.practice_id == p_id)
    )).scalars().all()
    for e in elements:
        await db.delete(e)
    await db.delete(practice)
    await db.commit()


async def _wipe_sp_content(db: AsyncSession, sp_id: str) -> dict:
    """Delete every SPTimeline / SPPractice / SPElement under an SP.
    Mirrors `_wipe_pg_content` for the SP-side import overwrite path."""
    timelines = (await db.execute(
        select(SPTimeline).where(SPTimeline.sp_recommendation_id == sp_id)
    )).scalars().all()
    tl_count = len(timelines)
    practice_count = 0
    element_count = 0
    for tl in timelines:
        practices = (await db.execute(
            select(SPPractice).where(SPPractice.timeline_id == tl.id)
        )).scalars().all()
        practice_count += len(practices)
        for p in practices:
            elements = (await db.execute(
                select(SPElement).where(SPElement.practice_id == p.id)
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
    content; it doesn't create the SP itself."""
    await _assert_cm_can_edit_client(db, current_user.id, client_id)

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
        select(func.count()).select_from(SPTimeline).where(
            SPTimeline.sp_recommendation_id == sp_id,
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

    # Deep-copy: PGTimeline → SPTimeline, PGPractice → SPPractice,
    # PGElement → SPElement. Same field shapes; just different tables.
    pg_timelines = (await db.execute(
        select(PGTimeline).where(PGTimeline.pg_recommendation_id == src_pg.id)
    )).scalars().all()
    for src_tl in pg_timelines:
        new_tl = SPTimeline(
            sp_recommendation_id=sp_id,
            name=src_tl.name,
            from_type=src_tl.from_type,
            from_value=src_tl.from_value,
            to_value=src_tl.to_value,
        )
        db.add(new_tl)
        await db.flush()

        src_practices = (await db.execute(
            select(PGPractice).where(PGPractice.timeline_id == src_tl.id)
        )).scalars().all()
        for src_p in src_practices:
            new_p = SPPractice(
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
                select(PGElement).where(PGElement.practice_id == src_p.id)
            )).scalars().all()
            for src_el in src_elements:
                db.add(SPElement(
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


@router.post("/client/{client_id}/sp-recommendations/{sp_id}/publish")
async def publish_sp(
    client_id: str,
    sp_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sp = (await db.execute(
        select(SPRecommendation).where(SPRecommendation.id == sp_id, SPRecommendation.client_id == client_id)
    )).scalar_one_or_none()
    if not sp:
        raise HTTPException(status_code=404, detail="SP recommendation not found")
    res = validate_publish_transition(sp.status)
    if not res.allowed:
        _raise_publish_transition(res)

    prev = (await db.execute(
        select(SPRecommendation).where(
            SPRecommendation.specific_problem_cosh_id == sp.specific_problem_cosh_id,
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

    PG list source is `app/services/cha_problem_groups.py` (a
    hardcoded V1 stopgap). When Cosh ships the `problem_group`
    Connect, swap the source there; this endpoint stays the same."""
    from app.services.cha_problem_groups import list_problem_groups

    pgs = list_problem_groups()

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
        select(PGTimeline.pg_recommendation_id, func.count())
        .where(PGTimeline.pg_recommendation_id.in_(rec_ids))
        .group_by(PGTimeline.pg_recommendation_id)
    )).all())

    pg_names = {p["cosh_id"]: p["name_en"] for p in list_problem_groups()}

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
    from app.services.cha_problem_groups import list_problem_groups

    q = (
        select(PGTimeline, PGRecommendation)
        .join(PGRecommendation, PGTimeline.pg_recommendation_id == PGRecommendation.id)
        .where(
            PGRecommendation.client_id == client_id,
            PGTimeline.pg_recommendation_id.isnot(None),
        )
    )
    if problem_group_cosh_id:
        q = q.where(PGRecommendation.problem_group_cosh_id == problem_group_cosh_id)
    if recommendation_id:
        q = q.where(PGTimeline.pg_recommendation_id == recommendation_id)
    if area_or_plant:
        q = q.where(PGRecommendation.area_or_plant == area_or_plant)
    q = q.order_by(PGRecommendation.problem_group_cosh_id, PGTimeline.from_value)

    rows = (await db.execute(q)).all()
    if not rows:
        return []

    tl_ids = [tl.id for tl, _ in rows]
    practice_counts = dict((await db.execute(
        select(PGPractice.timeline_id, func.count())
        .where(PGPractice.timeline_id.in_(tl_ids))
        .group_by(PGPractice.timeline_id)
    )).all())

    pg_names = {p["cosh_id"]: p["name_en"] for p in list_problem_groups()}

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
        select(PGPractice, PGTimeline, PGRecommendation)
        .join(PGTimeline, PGPractice.timeline_id == PGTimeline.id)
        .join(PGRecommendation, PGTimeline.pg_recommendation_id == PGRecommendation.id)
        .where(
            PGRecommendation.client_id == client_id,
            PGTimeline.pg_recommendation_id.isnot(None),
        )
    )
    if problem_group_cosh_id:
        q = q.where(PGRecommendation.problem_group_cosh_id == problem_group_cosh_id)
    if recommendation_id:
        q = q.where(PGTimeline.pg_recommendation_id == recommendation_id)
    if timeline_id:
        q = q.where(PGPractice.timeline_id == timeline_id)
    if area_or_plant:
        q = q.where(PGRecommendation.area_or_plant == area_or_plant)
    if l1:
        q = q.where(PGPractice.l1_type == l1)
    if l2:
        q = q.where(PGPractice.l2_type == l2)

    total = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar() or 0

    q = q.order_by(
        PGRecommendation.problem_group_cosh_id, PGTimeline.from_value,
        PGPractice.display_order,
    ).offset(offset).limit(limit)

    rows = (await db.execute(q)).all()
    if not rows:
        return {"items": [], "total": total, "limit": limit, "offset": offset}

    practice_ids = [pr.id for pr, _, _ in rows]
    elements_by_practice: dict[str, list[PGElement]] = {}
    if practice_ids:
        elem_rows = (await db.execute(
            select(PGElement)
            .where(PGElement.practice_id.in_(practice_ids))
            .order_by(PGElement.display_order)
        )).scalars().all()
        for e in elem_rows:
            elements_by_practice.setdefault(e.practice_id, []).append(e)

    pg_names = {p["cosh_id"]: p["name_en"] for p in list_problem_groups()}

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
