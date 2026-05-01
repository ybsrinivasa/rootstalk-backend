from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.farmpundit.models import (
    FarmPunditProfile, FarmPunditExpertise, FarmPunditSupportArea,
    ClientFarmPundit, PunditInvitation, PunditRole,
    Query, QueryMedia, QueryRemark, QueryResponse, StandardResponse,
    QueryStatus, QueryRemarkAction,
)

router = APIRouter(tags=["FarmPundit"])

QUERY_EXPIRE_DAYS = 7
FREE_QUERIES_PER_COMPANY = 6


# ── FarmPundit Profile ─────────────────────────────────────────────────────────

class PunditProfileCreate(BaseModel):
    email: Optional[str] = None
    education: Optional[str] = None
    experience_band: Optional[str] = None
    support_method: Optional[str] = None
    cultivation_type: Optional[str] = None
    organisation_name: Optional[str] = None
    declaration_accepted: bool = False
    expertise_domains: list[str] = []
    support_areas: list[dict] = []


@router.post("/pundit/profile", status_code=201)
async def create_pundit_profile(
    request: PunditProfileCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.user_id == current_user.id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Profile already exists. Use PUT to update.")

    profile = FarmPunditProfile(
        user_id=current_user.id,
        email=request.email,
        education=request.education,
        experience_band=request.experience_band,
        support_method=request.support_method,
        cultivation_type=request.cultivation_type,
        organisation_name=request.organisation_name,
        declaration_accepted=request.declaration_accepted,
    )
    db.add(profile)
    await db.flush()

    for domain in request.expertise_domains:
        db.add(FarmPunditExpertise(pundit_id=profile.id, domain=domain))
    for area in request.support_areas:
        db.add(FarmPunditSupportArea(pundit_id=profile.id, **area))

    await db.commit()
    await db.refresh(profile)
    return {"id": profile.id, "user_id": profile.user_id, "declaration_accepted": profile.declaration_accepted}


@router.put("/pundit/profile/phone-privacy")
async def toggle_phone_privacy(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = await _get_pundit_profile(db, current_user.id)
    profile.phone_hidden = data.get("phone_hidden", not profile.phone_hidden)
    await db.commit()
    return {"phone_hidden": profile.phone_hidden}


# ── Company Invitations ────────────────────────────────────────────────────────

class InviteRequest(BaseModel):
    pundit_user_id: str
    role: PunditRole = PunditRole.PRIMARY


@router.post("/client/{client_id}/pundit-invitations", status_code=201)
async def invite_pundit(
    client_id: str,
    request: InviteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.user_id == request.pundit_user_id)
    )).scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="FarmPundit profile not found")

    invitation = PunditInvitation(
        client_id=client_id,
        pundit_id=profile.id,
        role=request.role,
        status="PENDING",
    )
    db.add(invitation)
    await db.commit()
    return {"invitation_id": invitation.id, "status": "PENDING"}


@router.get("/pundit/invitations")
async def list_invitations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = await _get_pundit_profile(db, current_user.id)
    result = await db.execute(
        select(PunditInvitation).where(
            PunditInvitation.pundit_id == profile.id,
            PunditInvitation.status == "PENDING",
        )
    )
    return result.scalars().all()


@router.put("/pundit/invitations/{invitation_id}/accept")
async def accept_invitation(
    invitation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    inv = await _get_invitation(db, invitation_id)
    inv.status = "ACCEPTED"

    sequence = await _next_round_robin_sequence(db, inv.client_id)
    db.add(ClientFarmPundit(
        client_id=inv.client_id,
        pundit_id=inv.pundit_id,
        role=inv.role,
        status="ACTIVE",
        round_robin_sequence=sequence if inv.role == PunditRole.PRIMARY else None,
    ))
    await db.commit()
    return {"status": "ACCEPTED"}


@router.put("/pundit/invitations/{invitation_id}/reject")
async def reject_invitation(
    invitation_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not data.get("reason"):
        raise HTTPException(status_code=422, detail="Rejection reason is mandatory")
    inv = await _get_invitation(db, invitation_id)
    inv.status = "REJECTED"
    inv.rejection_reason = data["reason"]
    await db.commit()
    return {"status": "REJECTED"}


# ── Query Management (Farmer) ──────────────────────────────────────────────────

class QueryCreate(BaseModel):
    subscription_id: str
    client_id: str
    crop_cosh_id: Optional[str] = None
    crop_age: Optional[str] = None
    title: str
    description: Optional[str] = None
    severity: str = "MODERATE"


@router.post("/farmer/queries", status_code=201)
async def submit_query(
    request: QueryCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-12a: Submit query. Routes to default expert via round-robin."""
    expires_at = datetime.now(timezone.utc) + timedelta(days=QUERY_EXPIRE_DAYS)

    query = Query(
        farmer_user_id=current_user.id,
        subscription_id=request.subscription_id,
        client_id=request.client_id,
        crop_cosh_id=request.crop_cosh_id,
        crop_age=request.crop_age,
        title=request.title,
        description=request.description,
        severity=request.severity,
        status=QueryStatus.NEW,
        expires_at=expires_at,
    )
    db.add(query)
    await db.flush()

    # BL-12a: Assign to next Primary Expert via round-robin
    next_pundit = await _get_next_round_robin_pundit(db, request.client_id)
    if next_pundit:
        query.current_holder_id = next_pundit.id
        db.add(QueryRemark(
            query_id=query.id,
            pundit_id=next_pundit.id,
            action=QueryRemarkAction.RECEIVED,
        ))

    await db.commit()
    await db.refresh(query)
    return {"id": query.id, "status": query.status, "expires_at": query.expires_at}


@router.get("/farmer/queries")
async def list_farmer_queries(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Query).where(Query.farmer_user_id == current_user.id).order_by(Query.created_at.desc())
    )
    queries = result.scalars().all()
    return [{"id": q.id, "title": q.title, "status": q.status, "severity": q.severity,
             "expires_at": q.expires_at, "created_at": q.created_at} for q in queries]


# ── Query Management (FarmPundit) ──────────────────────────────────────────────

@router.get("/pundit/queries")
async def list_pundit_queries(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """New, forwarded, returned queries — sorted by urgency (fewest days remaining first)."""
    profile = await _get_pundit_profile(db, current_user.id)
    result = await db.execute(
        select(Query).where(
            Query.current_holder_id == profile.id,
            Query.status.in_([QueryStatus.NEW, QueryStatus.FORWARDED, QueryStatus.RETURNED]),
        ).order_by(Query.expires_at)
    )
    queries = result.scalars().all()
    return [{"id": q.id, "title": q.title, "status": q.status, "severity": q.severity,
             "client_id": q.client_id, "expires_at": q.expires_at,
             "days_remaining": max(0, (q.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days)} for q in queries]


@router.put("/pundit/queries/{query_id}/respond")
async def respond_to_query(
    query_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Any expert holding query can respond. Closes query everywhere simultaneously."""
    profile = await _get_pundit_profile(db, current_user.id)
    query = await _get_query(db, query_id)

    if not any([data.get("text"), data.get("problem_cosh_id"), data.get("standard_response_id")]):
        raise HTTPException(status_code=422, detail="At least one response element required")

    response = QueryResponse(
        query_id=query_id,
        pundit_id=profile.id,
        problem_cosh_id=data.get("problem_cosh_id"),
        text=data.get("text"),
        standard_response_id=data.get("standard_response_id"),
    )
    db.add(response)
    db.add(QueryRemark(query_id=query_id, pundit_id=profile.id, action=QueryRemarkAction.RESPONDED))

    query.status = QueryStatus.RESPONDED
    query.current_holder_id = None
    await db.commit()
    return {"status": "RESPONDED"}


@router.put("/pundit/queries/{query_id}/forward")
async def forward_query(
    query_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Primary Expert forwards to another expert. Mandatory comments. 7-day clock never resets."""
    if not data.get("to_pundit_id") or not data.get("remarks"):
        raise HTTPException(status_code=422, detail="to_pundit_id and remarks are mandatory")

    profile = await _get_pundit_profile(db, current_user.id)
    query = await _get_query(db, query_id)

    db.add(QueryRemark(
        query_id=query_id,
        pundit_id=profile.id,
        action=QueryRemarkAction.FORWARDED,
        forwarded_to_pundit_id=data["to_pundit_id"],
        remark=data["remarks"],
    ))

    query.status = QueryStatus.FORWARDED
    query.current_holder_id = data["to_pundit_id"]
    await db.commit()
    return {"status": "FORWARDED"}


@router.put("/pundit/queries/{query_id}/return")
async def return_query(
    query_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Recipient returns query to sender. Mandatory remarks."""
    if not data.get("remarks"):
        raise HTTPException(status_code=422, detail="Remarks are mandatory when returning")

    profile = await _get_pundit_profile(db, current_user.id)
    query = await _get_query(db, query_id)

    # Find original sender
    remarks = (await db.execute(
        select(QueryRemark).where(
            QueryRemark.query_id == query_id,
            QueryRemark.action == QueryRemarkAction.FORWARDED,
        ).order_by(QueryRemark.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    original_sender_id = remarks.pundit_id if remarks else None

    db.add(QueryRemark(
        query_id=query_id,
        pundit_id=profile.id,
        action=QueryRemarkAction.RETURNED,
        remark=data["remarks"],
    ))
    query.status = QueryStatus.RETURNED
    query.current_holder_id = original_sender_id
    await db.commit()
    return {"status": "RETURNED"}


@router.put("/pundit/queries/{query_id}/reject")
async def reject_query(
    query_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Primary Expert only. Mandatory comments."""
    if not data.get("remarks"):
        raise HTTPException(status_code=422, detail="Remarks are mandatory when rejecting")

    profile = await _get_pundit_profile(db, current_user.id)
    query = await _get_query(db, query_id)

    db.add(QueryRemark(query_id=query_id, pundit_id=profile.id,
                       action=QueryRemarkAction.REJECTED, remark=data["remarks"]))
    query.status = QueryStatus.REJECTED
    query.current_holder_id = None
    await db.commit()
    return {"status": "REJECTED"}


@router.get("/pundit/queries/history")
async def query_history(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = await _get_pundit_profile(db, current_user.id)
    result = await db.execute(
        select(Query).where(
            Query.current_holder_id == profile.id,
            Query.status.in_([QueryStatus.RESPONDED, QueryStatus.REJECTED, QueryStatus.EXPIRED]),
        ).order_by(Query.created_at.desc())
    )
    return result.scalars().all()


# ── Standard Q&A Library ──────────────────────────────────────────────────────

@router.post("/client/{client_id}/standard-responses", status_code=201)
async def create_standard_response(
    client_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sr = StandardResponse(
        client_id=client_id,
        crop_cosh_id=data.get("crop_cosh_id"),
        question_text=data["question_text"],
        created_by=current_user.id,
    )
    db.add(sr)
    await db.commit()
    return {"id": sr.id}


@router.get("/pundit/standard-responses")
async def search_standard_responses(
    client_id: str,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(StandardResponse).where(StandardResponse.client_id == client_id)
    if search:
        q = q.where(StandardResponse.question_text.ilike(f"%{search}%"))
    result = await db.execute(q)
    return result.scalars().all()


# ── FarmPundit search (Client Portal CA) ──────────────────────────────────────

@router.get("/client/{client_id}/pundit-search")
async def search_pundits(
    support_area: Optional[str] = None,
    expertise_domain: Optional[str] = None,
    language: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.declaration_accepted == True)
    )
    profiles = result.scalars().all()
    return [{"id": p.id, "user_id": p.user_id, "education": p.education,
             "experience_band": p.experience_band, "phone_hidden": p.phone_hidden} for p in profiles]


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_pundit_profile(db: AsyncSession, user_id: str) -> FarmPunditProfile:
    result = await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.user_id == user_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="FarmPundit profile not found. Please register first.")
    return profile


async def _get_query(db: AsyncSession, query_id: str) -> Query:
    result = await db.execute(select(Query).where(Query.id == query_id))
    q = result.scalar_one_or_none()
    if not q:
        raise HTTPException(status_code=404, detail="Query not found")
    return q


async def _get_invitation(db: AsyncSession, invitation_id: str) -> PunditInvitation:
    result = await db.execute(select(PunditInvitation).where(PunditInvitation.id == invitation_id))
    inv = result.scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Invitation not found")
    return inv


async def _get_next_round_robin_pundit(db: AsyncSession, client_id: str) -> Optional[FarmPunditProfile]:
    """BL-12a: Sequential round-robin among PRIMARY experts, ordered by onboarded_at."""
    result = await db.execute(
        select(ClientFarmPundit)
        .where(
            ClientFarmPundit.client_id == client_id,
            ClientFarmPundit.role == PunditRole.PRIMARY,
            ClientFarmPundit.status == "ACTIVE",
        )
        .order_by(ClientFarmPundit.onboarded_at)
    )
    pundits = result.scalars().all()
    if not pundits:
        return None

    # Find which pundit received the last query
    last_remark = (await db.execute(
        select(QueryRemark)
        .join(Query, Query.id == QueryRemark.query_id)
        .where(Query.client_id == client_id, QueryRemark.action == QueryRemarkAction.RECEIVED)
        .order_by(QueryRemark.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    if not last_remark:
        profile = (await db.execute(
            select(FarmPunditProfile).where(FarmPunditProfile.id == pundits[0].pundit_id)
        )).scalar_one_or_none()
        return profile

    last_idx = next((i for i, p in enumerate(pundits) if p.pundit_id == last_remark.pundit_id), -1)
    next_pundit_row = pundits[(last_idx + 1) % len(pundits)]
    profile = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.id == next_pundit_row.pundit_id)
    )).scalar_one_or_none()
    return profile


async def _next_round_robin_sequence(db: AsyncSession, client_id: str) -> int:
    result = await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.client_id == client_id,
            ClientFarmPundit.role == PunditRole.PRIMARY,
        )
    )
    existing = result.scalars().all()
    return len(existing) + 1
