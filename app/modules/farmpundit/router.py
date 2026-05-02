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
    FarmPunditLanguage, FarmPunditCropGroup, FarmPunditPreference,
    ClientFarmPundit, PunditInvitation, PunditRole,
    Query, QueryMedia, QueryRemark, QueryResponse, QueryResponseMedia,
    StandardResponse, QueryStatus, QueryRemarkAction,
)
from app.services.bl12_query_routing import route_query, ExpertSlot

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
    organisation_type_cosh_id: Optional[str] = None
    declaration_accepted: bool = False
    expertise_domains: list[str] = []
    support_areas: list[dict] = []    # [{"state_cosh_id": ..., "district_cosh_id": ...}]
    languages: list[str] = []          # language_code list
    crop_groups: list[str] = []        # crop_group_cosh_id list


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
        organisation_type_cosh_id=request.organisation_type_cosh_id,
        declaration_accepted=request.declaration_accepted,
    )
    db.add(profile)
    await db.flush()

    for domain in request.expertise_domains:
        db.add(FarmPunditExpertise(pundit_id=profile.id, domain=domain))
    for area in request.support_areas:
        db.add(FarmPunditSupportArea(pundit_id=profile.id, **area))
    for lang in request.languages:
        db.add(FarmPunditLanguage(pundit_id=profile.id, language_code=lang))
    for cg in request.crop_groups:
        db.add(FarmPunditCropGroup(pundit_id=profile.id, crop_group_cosh_id=cg))

    # Add FARM_PUNDIT role to user
    from app.modules.platform.models import UserRole, RoleType
    existing_role = (await db.execute(
        select(UserRole).where(UserRole.user_id == current_user.id, UserRole.role_type == RoleType.FARM_PUNDIT)
    )).scalar_one_or_none()
    if not existing_role:
        db.add(UserRole(user_id=current_user.id, role_type=RoleType.FARM_PUNDIT))

    await db.commit()
    await db.refresh(profile)
    return {"id": profile.id, "user_id": profile.user_id, "declaration_accepted": profile.declaration_accepted}


@router.get("/pundit/profile")
async def get_pundit_profile_detail(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = await _get_pundit_profile(db, current_user.id)
    domains = (await db.execute(
        select(FarmPunditExpertise).where(FarmPunditExpertise.pundit_id == profile.id)
    )).scalars().all()
    areas = (await db.execute(
        select(FarmPunditSupportArea).where(FarmPunditSupportArea.pundit_id == profile.id)
    )).scalars().all()
    langs = (await db.execute(
        select(FarmPunditLanguage).where(FarmPunditLanguage.pundit_id == profile.id)
    )).scalars().all()
    crop_groups = (await db.execute(
        select(FarmPunditCropGroup).where(FarmPunditCropGroup.pundit_id == profile.id)
    )).scalars().all()
    companies = (await db.execute(
        select(ClientFarmPundit).where(ClientFarmPundit.pundit_id == profile.id, ClientFarmPundit.status == "ACTIVE")
    )).scalars().all()
    return {
        "id": profile.id,
        "user_id": profile.user_id,
        "email": profile.email,
        "education": profile.education,
        "experience_band": profile.experience_band,
        "support_method": profile.support_method,
        "cultivation_type": profile.cultivation_type,
        "organisation_name": profile.organisation_name,
        "phone_hidden": profile.phone_hidden,
        "declaration_accepted": profile.declaration_accepted,
        "expertise_domains": [d.domain for d in domains],
        "support_areas": [{"state_cosh_id": a.state_cosh_id, "district_cosh_id": a.district_cosh_id} for a in areas],
        "languages": [l.language_code for l in langs],
        "crop_groups": [c.crop_group_cosh_id for c in crop_groups],
        "companies": [{"client_id": c.client_id, "role": c.role, "is_promoter_pundit": c.is_promoter_pundit} for c in companies],
    }


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

    # BL-12a: Full priority routing (preference → Promoter-Pundit → round-robin)
    next_pundit = await _get_next_pundit_for_query(db, request.client_id, request.subscription_id)
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
    await db.flush()

    # Attach response media if provided
    for media in data.get("media", []):
        db.add(QueryResponseMedia(
            response_id=response.id,
            media_type=media.get("media_type", "IMAGE"),
            url=media["url"],
            caption=media.get("caption"),
        ))

    db.add(QueryRemark(query_id=query_id, pundit_id=profile.id, action=QueryRemarkAction.RESPONDED))

    query.status = QueryStatus.RESPONDED
    query.current_holder_id = None

    # BL-12 / §14.7: If pundit identified a crop health problem → trigger CHA delivery
    if data.get("problem_cosh_id"):
        await _trigger_cha_for_query(db, query, data["problem_cosh_id"])

    await db.commit()
    return {"status": "RESPONDED", "response_id": response.id}


@router.put("/pundit/queries/{query_id}/forward")
async def forward_query(
    query_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Primary Expert forwards to another expert. Panel Experts cannot forward. Mandatory remarks."""
    if not data.get("to_pundit_id") or not data.get("remarks"):
        raise HTTPException(status_code=422, detail="to_pundit_id and remarks are mandatory")

    profile = await _get_pundit_profile(db, current_user.id)
    query = await _get_query(db, query_id)

    # BL-12 TC-BL12-04: Panel Experts cannot forward
    holder_slot = (await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.client_id == query.client_id,
            ClientFarmPundit.pundit_id == profile.id,
        )
    )).scalar_one_or_none()
    if holder_slot and holder_slot.role == PunditRole.PANEL:
        raise HTTPException(status_code=403, detail="Panel Experts cannot forward queries. You can only Respond or Return.")

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
    client_id: str,
    state_cosh_id: Optional[str] = None,
    expertise_domain: Optional[str] = None,
    language_code: Optional[str] = None,
    education: Optional[str] = None,
    experience_band: Optional[str] = None,
    support_method: Optional[str] = None,
    crop_group: Optional[str] = None,
    phone: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Multi-filter search across all registered FarmPundits (declaration_accepted=True)."""
    q = select(FarmPunditProfile).where(FarmPunditProfile.declaration_accepted == True)  # noqa: E712
    if education:
        q = q.where(FarmPunditProfile.education == education)
    if experience_band:
        q = q.where(FarmPunditProfile.experience_band == experience_band)
    if support_method:
        q = q.where(FarmPunditProfile.support_method == support_method)

    profiles = (await db.execute(q)).scalars().all()

    # Filter by support area, language, expertise domain in Python (small dataset)
    if state_cosh_id:
        area_pundit_ids = {
            r.pundit_id for r in (await db.execute(
                select(FarmPunditSupportArea).where(FarmPunditSupportArea.state_cosh_id == state_cosh_id)
            )).scalars().all()
        }
        profiles = [p for p in profiles if p.id in area_pundit_ids]

    if expertise_domain:
        domain_pundit_ids = {
            r.pundit_id for r in (await db.execute(
                select(FarmPunditExpertise).where(FarmPunditExpertise.domain == expertise_domain)
            )).scalars().all()
        }
        profiles = [p for p in profiles if p.id in domain_pundit_ids]

    if language_code:
        lang_pundit_ids = {
            r.pundit_id for r in (await db.execute(
                select(FarmPunditLanguage).where(FarmPunditLanguage.language_code == language_code)
            )).scalars().all()
        }
        profiles = [p for p in profiles if p.id in lang_pundit_ids]

    if crop_group:
        cg_pundit_ids = {
            r.pundit_id for r in (await db.execute(
                select(FarmPunditCropGroup).where(FarmPunditCropGroup.crop_group_cosh_id == crop_group)
            )).scalars().all()
        }
        profiles = [p for p in profiles if p.id in cg_pundit_ids]

    if phone:
        phone_user_ids = {
            u.id for u in (await db.execute(
                select(User).where(User.phone.like(f"%{phone}%"))
            )).scalars().all()
        }
        profiles = [p for p in profiles if p.user_id in phone_user_ids]

    # Already onboarded by this client?
    onboarded_ids = {
        r.pundit_id for r in (await db.execute(
            select(ClientFarmPundit).where(ClientFarmPundit.client_id == client_id)
        )).scalars().all()
    }

    result_out = []
    for p in profiles:
        user = (await db.execute(select(User).where(User.id == p.user_id))).scalar_one_or_none()
        result_out.append({
            "id": p.id,
            "user_id": p.user_id,
            "name": user.name if user else None,
            "phone": user.phone if (user and not p.phone_hidden) else None,
            "email": p.email,
            "education": p.education,
            "experience_band": p.experience_band,
            "support_method": p.support_method,
            "already_onboarded": p.id in onboarded_ids,
        })
    return result_out


# ── Company Pundit Management (Client Portal) ─────────────────────────────────

@router.get("/client/{client_id}/pundits")
async def list_company_pundits(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ClientFarmPundit).where(ClientFarmPundit.client_id == client_id)
        .order_by(ClientFarmPundit.onboarded_at)
    )
    pundits = result.scalars().all()
    out = []
    for cp in pundits:
        profile = (await db.execute(
            select(FarmPunditProfile).where(FarmPunditProfile.id == cp.pundit_id)
        )).scalar_one_or_none()
        user = (await db.execute(
            select(User).where(User.id == profile.user_id)
        )).scalar_one_or_none() if profile else None
        out.append({
            "id": cp.id,
            "pundit_id": cp.pundit_id,
            "name": user.name if user else None,
            "phone": user.phone if user else None,
            "role": cp.role,
            "status": cp.status,
            "is_promoter_pundit": cp.is_promoter_pundit,
            "round_robin_sequence": cp.round_robin_sequence,
            "onboarded_at": cp.onboarded_at,
        })
    return out


@router.put("/client/{client_id}/pundits/{cp_id}/deactivate")
async def deactivate_company_pundit(
    client_id: str,
    cp_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deactivate a FarmPundit from this company. They keep active queries until resolved."""
    cp = (await db.execute(
        select(ClientFarmPundit).where(ClientFarmPundit.id == cp_id, ClientFarmPundit.client_id == client_id)
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Company pundit not found")
    cp.status = "INACTIVE"
    await db.commit()
    return {"status": "INACTIVE"}


@router.put("/client/{client_id}/pundits/{cp_id}/promoter-pundit")
async def toggle_promoter_pundit(
    client_id: str,
    cp_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Field Manager designates a facilitator FarmPundit as a Promoter-Pundit."""
    cp = (await db.execute(
        select(ClientFarmPundit).where(ClientFarmPundit.id == cp_id, ClientFarmPundit.client_id == client_id)
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Company pundit not found")
    cp.is_promoter_pundit = data.get("is_promoter_pundit", not cp.is_promoter_pundit)
    await db.commit()
    return {"is_promoter_pundit": cp.is_promoter_pundit}


# ── Query Detail Routes ────────────────────────────────────────────────────────

@router.get("/pundit/queries/{query_id}")
async def get_query_detail_pundit(
    query_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pundit sees full query with remarks chain and response."""
    profile = await _get_pundit_profile(db, current_user.id)
    query = await _get_query(db, query_id)

    remarks = (await db.execute(
        select(QueryRemark).where(QueryRemark.query_id == query_id).order_by(QueryRemark.created_at)
    )).scalars().all()

    response = (await db.execute(
        select(QueryResponse).where(QueryResponse.query_id == query_id)
    )).scalar_one_or_none()

    media_result = (await db.execute(
        select(QueryMedia).where(QueryMedia.query_id == query_id)
    )).scalars().all()

    response_media = []
    if response:
        rm_result = (await db.execute(
            select(QueryResponseMedia).where(QueryResponseMedia.response_id == response.id)
        )).scalars().all()
        response_media = [{"media_type": m.media_type, "url": m.url, "caption": m.caption} for m in rm_result]

    return {
        "id": query.id,
        "title": query.title,
        "description": query.description,
        "severity": query.severity,
        "crop_cosh_id": query.crop_cosh_id,
        "crop_age": query.crop_age,
        "status": query.status,
        "created_at": query.created_at,
        "expires_at": query.expires_at,
        "days_remaining": max(0, (query.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days),
        "is_holding": query.current_holder_id == profile.id,
        "media": [{"media_type": m.media_type, "url": m.url} for m in media_result],
        "remarks": [
            {
                "action": r.action, "pundit_id": r.pundit_id,
                "forwarded_to_pundit_id": r.forwarded_to_pundit_id,
                "remark": r.remark, "created_at": r.created_at,
            }
            for r in remarks
        ],
        "response": {
            "problem_cosh_id": response.problem_cosh_id,
            "text": response.text,
            "media": response_media,
            "created_at": response.created_at,
        } if response else None,
    }


@router.get("/farmer/queries/{query_id}")
async def get_query_detail_farmer(
    query_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer sees their query with the pundit's response (if responded)."""
    query = (await db.execute(
        select(Query).where(Query.id == query_id, Query.farmer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not query:
        raise HTTPException(status_code=404, detail="Query not found")

    response = (await db.execute(
        select(QueryResponse).where(QueryResponse.query_id == query_id)
    )).scalar_one_or_none()

    response_media = []
    if response:
        rm_result = (await db.execute(
            select(QueryResponseMedia).where(QueryResponseMedia.response_id == response.id)
        )).scalars().all()
        response_media = [{"media_type": m.media_type, "url": m.url, "caption": m.caption} for m in rm_result]

    media_result = (await db.execute(
        select(QueryMedia).where(QueryMedia.query_id == query_id)
    )).scalars().all()

    return {
        "id": query.id,
        "title": query.title,
        "description": query.description,
        "severity": query.severity,
        "crop_cosh_id": query.crop_cosh_id,
        "crop_age": query.crop_age,
        "status": query.status,
        "created_at": query.created_at,
        "expires_at": query.expires_at,
        "media": [{"media_type": m.media_type, "url": m.url} for m in media_result],
        "response": {
            "text": response.text,
            "problem_cosh_id": response.problem_cosh_id,
            "media": response_media,
            "created_at": response.created_at,
            "has_cha_recommendation": bool(response.problem_cosh_id),
        } if response else None,
    }


# ── Farmer: Set preferred FarmPundit ─────────────────────────────────────────

@router.post("/farmer/subscriptions/{subscription_id}/pundit-preference")
async def set_pundit_preference(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer sets their preferred FarmPundit for this subscription."""
    pundit_id = data.get("pundit_id")
    if not pundit_id:
        raise HTTPException(status_code=422, detail="pundit_id required")

    existing = (await db.execute(
        select(FarmPunditPreference).where(FarmPunditPreference.subscription_id == subscription_id)
    )).scalar_one_or_none()

    if existing:
        existing.pundit_id = pundit_id
        existing.set_at = datetime.now(timezone.utc)
    else:
        db.add(FarmPunditPreference(
            subscription_id=subscription_id,
            pundit_id=pundit_id,
        ))
    await db.commit()
    return {"detail": "Preference set", "pundit_id": pundit_id}


# ── Company Queries Monitoring ────────────────────────────────────────────────

@router.get("/client/{client_id}/queries")
async def list_company_queries(
    client_id: str,
    status_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(Query).where(Query.client_id == client_id).order_by(Query.created_at.desc())
    if status_filter:
        q = q.where(Query.status == status_filter)
    queries = (await db.execute(q)).scalars().all()
    return [
        {
            "id": query.id, "title": query.title, "status": query.status,
            "severity": query.severity, "created_at": query.created_at,
            "expires_at": query.expires_at, "farmer_user_id": query.farmer_user_id,
            "current_holder_id": query.current_holder_id,
        }
        for query in queries
    ]


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


async def _get_next_pundit_for_query(
    db: AsyncSession,
    client_id: str,
    subscription_id: str,
) -> Optional[FarmPunditProfile]:
    """BL-12a: Full priority routing — preference → Promoter-Pundit → round-robin."""
    # Load all company pundits
    all_cp = (await db.execute(
        select(ClientFarmPundit).where(ClientFarmPundit.client_id == client_id)
    )).scalars().all()

    experts = [
        ExpertSlot(
            pundit_id=cp.pundit_id,
            role=cp.role.value if hasattr(cp.role, 'value') else str(cp.role),
            status=cp.status,
            round_robin_sequence=cp.round_robin_sequence or 0,
            is_promoter_pundit=cp.is_promoter_pundit,
            onboarded_at=cp.onboarded_at,
        )
        for cp in all_cp
    ]

    # Priority 1: Farmer preference
    pref = (await db.execute(
        select(FarmPunditPreference).where(FarmPunditPreference.subscription_id == subscription_id)
    )).scalar_one_or_none()
    farmer_preferred = pref.pundit_id if pref else None

    # Priority 2: Promoter-Pundit (from promoter_assignments)
    from app.modules.subscriptions.models import PromoterAssignment
    assignment = (await db.execute(
        select(PromoterAssignment).where(
            PromoterAssignment.subscription_id == subscription_id,
            PromoterAssignment.status == "ACTIVE",
        ).order_by(PromoterAssignment.assigned_at.desc()).limit(1)
    )).scalar_one_or_none()

    promoter_pundit_id = None
    if assignment:
        # Check if promoter is also a Promoter-Pundit for this client
        promoter_user = (await db.execute(
            select(FarmPunditProfile).where(FarmPunditProfile.user_id == assignment.promoter_user_id)
        )).scalar_one_or_none()
        if promoter_user:
            pp_slot = next(
                (e for e in experts if e.pundit_id == promoter_user.id and e.is_promoter_pundit),
                None,
            )
            if pp_slot:
                promoter_pundit_id = promoter_user.id

    # Last received pundit (for round-robin)
    last_remark = (await db.execute(
        select(QueryRemark)
        .join(Query, Query.id == QueryRemark.query_id)
        .where(Query.client_id == client_id, QueryRemark.action == QueryRemarkAction.RECEIVED)
        .order_by(QueryRemark.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    last_received_id = last_remark.pundit_id if last_remark else None

    # Run BL-12a service
    result = route_query(experts, farmer_preferred, promoter_pundit_id, last_received_id)
    if not result.pundit_id:
        return None

    return (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.id == result.pundit_id)
    )).scalar_one_or_none()


# Keep old name as alias for backward compat
async def _get_next_round_robin_pundit(db: AsyncSession, client_id: str) -> Optional[FarmPunditProfile]:
    return await _get_next_pundit_for_query(db, client_id, "")


async def _next_round_robin_sequence(db: AsyncSession, client_id: str) -> int:
    result = await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.client_id == client_id,
            ClientFarmPundit.role == PunditRole.PRIMARY,
        )
    )
    existing = result.scalars().all()
    return len(existing) + 1


async def _trigger_cha_for_query(db: AsyncSession, query: Query, problem_cosh_id: str):
    """
    §14.7/14.8: When pundit identifies a problem, deliver the corresponding
    CHA recommendation using the full SP→PG hierarchy:
    1. SP (client-specific for exact specific_problem_cosh_id)
    2. PG (client-specific for parent problem_group)
    3. PG (global for parent problem_group)
    """
    from app.modules.subscriptions.models import TriggeredCHAEntry
    from app.services.cha_hierarchy import resolve_cha_recommendation

    sub = (await db.execute(
        select(Subscription).where(
            Subscription.farmer_user_id == query.farmer_user_id,
            Subscription.client_id == query.client_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        ).order_by(Subscription.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not sub:
        return

    resolved = await resolve_cha_recommendation(db, query.client_id, problem_cosh_id)
    if not resolved:
        return

    db.add(TriggeredCHAEntry(
        subscription_id=sub.id,
        farmer_user_id=query.farmer_user_id,
        client_id=query.client_id,
        problem_cosh_id=problem_cosh_id,
        recommendation_type=resolved.recommendation_type,
        recommendation_id=resolved.recommendation_id,
        triggered_by="QUERY",
        problem_name=resolved.problem_name,
        parent_pg_cosh_id=resolved.parent_pg_cosh_id,
    ))
