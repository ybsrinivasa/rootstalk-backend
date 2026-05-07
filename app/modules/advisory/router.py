from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import StatusEnum, User
from app.modules.advisory.models import (
    Package, PackageLocation, PackageAuthor, PackageVariable,
    Parameter, Variable, PackageVariable,
    ParameterTranslation, VariableTranslation, TranslationStatus,
    Timeline, Practice, Element, Relation, ConditionalQuestion, PracticeConditional,
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
)
from app.modules.advisory.models import (
    PGRecommendation, PGTimeline, PGPractice, PGElement,
    SPRecommendation, SPTimeline, SPPractice, SPElement,
)
from app.modules.clients.models import ClientUser, ClientUserRole
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
    await _get_package(db, package_id, client_id)
    _validate_timeline(request)

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
    tl = await _get_timeline(db, timeline_id, package_id)
    for field, value in request.model_dump(exclude_unset=True).items():
        setattr(tl, field, value)
    # BL-17: validate boundaries after applying changes
    from app.modules.advisory.models import TimelineFromType
    check_from = request.from_value if request.from_value is not None else tl.from_value
    check_to = request.to_value if request.to_value is not None else tl.to_value
    if tl.from_type == TimelineFromType.DBS:
        if check_to >= check_from:
            raise HTTPException(status_code=422, detail="DBS timeline: from_value must be greater than to_value")
    else:
        if check_to <= check_from:
            raise HTTPException(status_code=422, detail="DAS/CALENDAR timeline: to_value must be greater than from_value")
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
    practice = Practice(
        timeline_id=timeline_id,
        l0_type=request.l0_type,
        l1_type=request.l1_type,
        l2_type=request.l2_type,
        display_order=request.display_order,
        is_special_input=request.is_special_input,
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


# ── Relations ──────────────────────────────────────────────────────────────────

@router.post("/client/{client_id}/timelines/{timeline_id}/relations", status_code=201)
async def create_relation(
    client_id: str, timeline_id: str,
    request: RelationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    relation = Relation(
        timeline_id=timeline_id,
        relation_type=request.relation_type,
        expression=request.expression,
    )
    db.add(relation)
    await db.flush()

    for practice_id in request.practice_ids:
        result = await db.execute(select(Practice).where(Practice.id == practice_id))
        practice = result.scalar_one_or_none()
        if practice:
            practice.relation_id = relation.id

    await db.commit()
    await db.refresh(relation)
    return {"id": relation.id, "relation_type": relation.relation_type, "expression": relation.expression}


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
    pc = PracticeConditional(
        practice_id=practice_id,
        question_id=request.question_id,
        answer=request.answer,
    )
    db.add(pc)
    await db.commit()
    await db.refresh(pc)
    return pc


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


def _validate_timeline(request: TimelineCreate):
    """DBS: from > to. DAS/CALENDAR: to > from. No cross-start timelines."""
    from app.modules.advisory.models import TimelineFromType
    if request.from_type == TimelineFromType.DBS:
        if request.to_value >= request.from_value:
            raise HTTPException(status_code=422, detail="DBS timeline: from_value must be greater than to_value")
    else:
        if request.to_value <= request.from_value:
            raise HTTPException(status_code=422, detail="DAS/CALENDAR timeline: to_value must be greater than from_value")


# ── Global CCA Packages ────────────────────────────────────────────────────────

@router.get("/advisory/global/packages", response_model=list[PackageOut])
async def list_global_packages(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Package).where(Package.client_id == None).order_by(Package.created_at.desc())  # noqa: E711
    )
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
    practice = Practice(
        timeline_id=tl_id,
        l0_type=request.l0_type,
        l1_type=request.l1_type,
        l2_type=request.l2_type,
        display_order=request.display_order,
        is_special_input=request.is_special_input,
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


@router.post("/client/{client_id}/packages/{pkg_id}/fork", response_model=PackageOut, status_code=201)
async def fork_global_package(
    client_id: str,
    pkg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deep-copy a global package (all timelines + practices + elements) to a client."""
    src = (await db.execute(
        select(Package).where(Package.id == pkg_id, Package.client_id == None)  # noqa: E711
    )).scalar_one_or_none()
    if not src:
        raise HTTPException(status_code=404, detail="Global package not found")

    # Create the local copy
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(PGRecommendation).where(PGRecommendation.client_id == None)  # noqa: E711
        .order_by(PGRecommendation.created_at.desc())
    )
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
        application_type=request.application_type,
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
    practice = PGPractice(
        timeline_id=tl_id,
        l0_type=request.l0_type,
        l1_type=request.l1_type,
        l2_type=request.l2_type,
        display_order=request.display_order,
        is_special_input=request.is_special_input,
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


@router.post("/client/{client_id}/pg-recommendations/import/{global_pg_id}", response_model=PGRecommendationOut, status_code=201)
async def import_global_pg(
    client_id: str,
    global_pg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deep-copy a global PG recommendation to a client, creating an independent local copy."""
    src = (await db.execute(
        select(PGRecommendation).where(PGRecommendation.id == global_pg_id, PGRecommendation.client_id == None)  # noqa: E711
    )).scalar_one_or_none()
    if not src:
        raise HTTPException(status_code=404, detail="Global PG recommendation not found")

    # Check for existing import
    existing = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.client_id == client_id,
            PGRecommendation.parent_id == global_pg_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="This PG recommendation is already imported. Edit the existing local copy.")

    copy = PGRecommendation(
        problem_group_cosh_id=src.problem_group_cosh_id,
        client_id=client_id,
        parent_id=global_pg_id,
        application_type=src.application_type,
    )
    db.add(copy)
    await db.flush()

    tl_result = await db.execute(select(PGTimeline).where(PGTimeline.pg_recommendation_id == src.id))
    for src_tl in tl_result.scalars().all():
        new_tl = PGTimeline(
            pg_recommendation_id=copy.id,
            name=src_tl.name,
            from_type=src_tl.from_type,
            from_value=src_tl.from_value,
            to_value=src_tl.to_value,
        )
        db.add(new_tl)
        await db.flush()

        p_result = await db.execute(select(PGPractice).where(PGPractice.timeline_id == src_tl.id))
        for src_p in p_result.scalars().all():
            new_p = PGPractice(
                timeline_id=new_tl.id,
                l0_type=src_p.l0_type,
                l1_type=src_p.l1_type,
                l2_type=src_p.l2_type,
                display_order=src_p.display_order,
                is_special_input=src_p.is_special_input,
            )
            db.add(new_p)
            await db.flush()

            el_result = await db.execute(select(PGElement).where(PGElement.practice_id == src_p.id))
            for src_el in el_result.scalars().all():
                db.add(PGElement(
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
    practice = PGPractice(
        timeline_id=tl_id,
        l0_type=request.l0_type,
        l1_type=request.l1_type,
        l2_type=request.l2_type,
        display_order=request.display_order,
        is_special_input=request.is_special_input,
    )
    db.add(practice)
    await db.commit()
    await db.refresh(practice)
    return practice


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
    practice = SPPractice(
        timeline_id=tl_id,
        l0_type=request.l0_type,
        l1_type=request.l1_type,
        l2_type=request.l2_type,
        display_order=request.display_order,
        is_special_input=request.is_special_input,
    )
    db.add(practice)
    await db.commit()
    await db.refresh(practice)
    return practice


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
