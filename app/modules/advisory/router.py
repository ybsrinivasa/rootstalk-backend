from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.advisory.models import (
    Package, PackageLocation, PackageAuthor, PackageVariable,
    Parameter, Variable, PackageVariable,
    Timeline, Practice, Element, Relation, ConditionalQuestion, PracticeConditional,
    PackageStatus, PackageType,
)
from app.modules.advisory.schemas import (
    PackageCreate, PackageUpdate, PackageOut, PackageLocationIn,
    ParameterCreate, VariableCreate, PackageVariableSet,
    TimelineCreate, TimelineUpdate, TimelineOut,
    PracticeCreate, PracticeOut,
    RelationCreate, ConditionalQuestionCreate, PracticeConditionalCreate,
)
from app.modules.clients.models import ClientUser, ClientUserRole

router = APIRouter(tags=["Advisory"])


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
    # Package type lock: duration fixed for PERENNIAL
    duration = 365 if request.package_type == PackageType.PERENNIAL else (request.duration_days or 180)

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
    pkg = await _get_package(db, package_id, client_id)
    for field, value in request.model_dump(exclude_unset=True).items():
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
    """BL-13: Versioning lifecycle — publish creates new version, previous ACTIVE → INACTIVE."""
    pkg = await _get_package(db, package_id, client_id)

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

    pkg.status = PackageStatus.ACTIVE
    pkg.version = pkg.version + 1
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
    pkg = await _get_package(db, package_id, client_id)
    existing = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == package_id)
    )).scalars().all()
    for loc in existing:
        await db.delete(loc)
    for loc in locations:
        db.add(PackageLocation(package_id=package_id, **loc.model_dump()))
    await db.commit()
    return {"detail": f"{len(locations)} locations saved"}


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


@router.put("/client/{client_id}/packages/{package_id}/variables")
async def set_package_variables(
    client_id: str, package_id: str,
    request: PackageVariableSet,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set the parameter→variable fingerprint for a Package."""
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
