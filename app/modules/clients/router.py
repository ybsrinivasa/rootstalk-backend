from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.config import settings
from app.dependencies import get_current_user
from app.modules.platform.models import User, StatusEnum
from app.modules.clients.models import (
    Client, ClientOrganisationType, ClientUser, ClientUserRole,
    ClientLocation, ClientCrop, ClientStatus,
    CMClientAssignment, CMPrivilegeModel, CMRights, CMPrivilege
)
from app.modules.clients.schemas import (
    ClientInitiate, ClientCASubmit, ClientReject, ClientEdit,
    ClientStatusUpdate, ClientOut, OnboardingLinkOut, CMAssignment, CMPrivilegeGrant,
    LocationCreate, LocationOut, CropCreate, CropOut,
    PortalUserCreate, PortalUserOut,
)
from app.modules.clients.service import (
    generate_token, send_onboarding_email, send_ca_credentials_email,
    get_client_by_token, create_ca_user
)

router = APIRouter(tags=["Clients"])


def _require_sa(current_user: User):
    if current_user.email != settings.sa_email:
        raise HTTPException(status_code=403, detail="Super Admin access required")


def _base_url() -> str:
    if settings.environment == "development":
        return "http://localhost:3000"
    return "https://rootstalk.in"


# ── SA: List all clients ───────────────────────────────────────────────────────

@router.get("/admin/clients", response_model=list[ClientOut])
async def list_clients(
    status_filter: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    q = select(Client).order_by(Client.created_at.desc())
    if status_filter:
        q = q.where(Client.status == status_filter)
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/admin/clients/{client_id}", response_model=ClientOut)
async def get_client(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


# ── SA: Initiate onboarding ────────────────────────────────────────────────────

@router.post("/admin/clients/initiate", response_model=OnboardingLinkOut, status_code=201)
async def initiate_onboarding(
    request: ClientInitiate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)

    # Validate short_name uniqueness
    existing = (await db.execute(
        select(Client).where(Client.short_name == request.short_name.lower())
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="This short name is already taken")

    token = generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    client = Client(
        full_name=request.full_name,
        short_name=request.short_name.lower(),
        ca_name=request.ca_name,
        ca_phone=request.ca_phone,
        ca_email=request.ca_email,
        is_manufacturer=request.is_manufacturer,
        status=ClientStatus.PENDING_REVIEW,
        onboarding_link_token=token,
        onboarding_link_expires_at=expires_at,
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)

    link = f"{_base_url()}/onboarding/{token}"

    if settings.environment != "development" and settings.email_smtp_user:
        await send_onboarding_email(client, link)

    return OnboardingLinkOut(
        client_id=client.id,
        short_name=client.short_name,
        onboarding_link=link,
        expires_at=expires_at,
    )


@router.post("/admin/clients/{client_id}/regenerate-link", response_model=OnboardingLinkOut)
async def regenerate_link(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    token = generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    client.onboarding_link_token = token
    client.onboarding_link_expires_at = expires_at
    await db.commit()

    link = f"{_base_url()}/onboarding/{token}"
    if settings.environment != "development" and settings.email_smtp_user:
        await send_onboarding_email(client, link)

    return OnboardingLinkOut(
        client_id=client.id,
        short_name=client.short_name,
        onboarding_link=link,
        expires_at=expires_at,
    )


# ── CA: Submit onboarding form (public) ───────────────────────────────────────

@router.get("/onboarding/{token}")
async def get_onboarding_context(token: str, db: AsyncSession = Depends(get_db)):
    """Return basic client info for pre-filling the onboarding form."""
    client = await get_client_by_token(db, token)
    if not client:
        raise HTTPException(status_code=404, detail="Invalid or expired onboarding link")
    return {
        "full_name": client.full_name,
        "short_name": client.short_name,
        "ca_name": client.ca_name,
        "ca_email": client.ca_email,
        "is_manufacturer": client.is_manufacturer,
    }


@router.post("/onboarding/{token}/submit", response_model=ClientOut)
async def submit_onboarding(
    token: str,
    request: ClientCASubmit,
    db: AsyncSession = Depends(get_db),
):
    client = await get_client_by_token(db, token)
    if not client:
        raise HTTPException(status_code=404, detail="Invalid or expired onboarding link")
    if client.status != ClientStatus.PENDING_REVIEW:
        raise HTTPException(status_code=400, detail="This onboarding link has already been used")

    # Validate GST (15 chars alphanumeric) and PAN (10 chars)
    if len(request.gst_number) != 15:
        raise HTTPException(status_code=422, detail="GST number must be 15 characters")
    if len(request.pan_number) != 10:
        raise HTTPException(status_code=422, detail="PAN number must be 10 characters")

    client.display_name = request.display_name
    client.tagline = request.tagline
    client.primary_colour = request.primary_colour
    client.secondary_colour = request.secondary_colour
    client.hq_address = request.hq_address
    client.gst_number = request.gst_number.upper()
    client.pan_number = request.pan_number.upper()
    client.website = request.website
    client.support_phone = request.support_phone
    client.office_phone = request.office_phone
    client.social_links = request.social_links
    client.onboarding_link_token = None  # invalidate link after use

    for cosh_id in request.org_type_cosh_ids:
        db.add(ClientOrganisationType(client_id=client.id, org_type_cosh_id=cosh_id))

    await db.commit()
    await db.refresh(client)
    return client


# ── SA: Approve / Reject ───────────────────────────────────────────────────────

@router.put("/admin/clients/{client_id}/approve", response_model=ClientOut)
async def approve_client(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    if client.status != ClientStatus.PENDING_REVIEW:
        raise HTTPException(status_code=400, detail="Client is not pending review")
    if not client.display_name:
        raise HTTPException(status_code=400, detail="CA has not submitted their details yet")

    ca_user, plain_password = await create_ca_user(db, client)
    client.status = ClientStatus.ACTIVE
    client.approved_at = datetime.now(timezone.utc)
    client.approved_by = current_user.id
    await db.commit()
    await db.refresh(client)

    if settings.email_smtp_user:
        await send_ca_credentials_email(
            client.ca_email, client.ca_name, client.short_name, plain_password
        )

    return client


@router.put("/admin/clients/{client_id}/reject", response_model=ClientOut)
async def reject_client(
    client_id: str,
    request: ClientReject,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    client.status = ClientStatus.REJECTED
    client.rejection_reason = request.reason
    await db.commit()
    await db.refresh(client)
    return client


# ── SA: Edit and toggle status ─────────────────────────────────────────────────

@router.put("/admin/clients/{client_id}", response_model=ClientOut)
async def edit_client(
    client_id: str,
    request: ClientEdit,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    for field, value in request.model_dump(exclude_unset=True).items():
        setattr(client, field, value)
    await db.commit()
    await db.refresh(client)
    return client


@router.put("/admin/clients/{client_id}/status", response_model=ClientOut)
async def toggle_client_status(
    client_id: str,
    request: ClientStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    client.status = request.status
    await db.commit()
    await db.refresh(client)
    return client


# ── Client Portal Login ────────────────────────────────────────────────────────

@router.get("/portal/{short_name}/branding")
async def get_portal_branding(short_name: str, db: AsyncSession = Depends(get_db)):
    """Public endpoint — returns branding for the login page."""
    result = await db.execute(
        select(Client).where(Client.short_name == short_name, Client.status == ClientStatus.ACTIVE)
    )
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Company not found")
    return {
        "display_name": client.display_name,
        "tagline": client.tagline,
        "logo_url": client.logo_url,
        "primary_colour": client.primary_colour,
    }


# ── Portal: Locations ──────────────────────────────────────────────────────────

@router.get("/client/{client_id}/locations", response_model=list[LocationOut])
async def list_locations(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ClientLocation).where(ClientLocation.client_id == client_id)
        .order_by(ClientLocation.added_at)
    )
    return result.scalars().all()


@router.post("/client/{client_id}/locations", response_model=LocationOut, status_code=201)
async def add_location(
    client_id: str,
    request: LocationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    loc = ClientLocation(
        client_id=client_id,
        state_cosh_id=request.state_cosh_id,
        district_cosh_id=request.district_cosh_id,
    )
    db.add(loc)
    await db.commit()
    await db.refresh(loc)
    return loc


# ── Portal: Crops ──────────────────────────────────────────────────────────────

@router.get("/client/{client_id}/crops", response_model=list[CropOut])
async def list_crops(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ClientCrop).where(ClientCrop.client_id == client_id)
        .order_by(ClientCrop.added_at)
    )
    return result.scalars().all()


@router.post("/client/{client_id}/crops", response_model=CropOut, status_code=201)
async def add_crop(
    client_id: str,
    request: CropCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing = (await db.execute(
        select(ClientCrop).where(
            ClientCrop.client_id == client_id,
            ClientCrop.crop_cosh_id == request.crop_cosh_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="This crop is already added")
    crop = ClientCrop(client_id=client_id, crop_cosh_id=request.crop_cosh_id)
    db.add(crop)
    await db.commit()
    await db.refresh(crop)
    return crop


# ── Portal: Users ──────────────────────────────────────────────────────────────

@router.get("/client/{client_id}/users", response_model=list[PortalUserOut])
async def list_portal_users(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ClientUser, User)
        .join(User, User.id == ClientUser.user_id)
        .where(ClientUser.client_id == client_id)
        .order_by(ClientUser.created_at)
    )
    rows = result.all()
    out = []
    for cu, user in rows:
        out.append(PortalUserOut(
            id=user.id,
            email=user.email,
            name=user.name,
            role=cu.role.value,
            status=cu.status.value,
            created_at=cu.created_at,
        ))
    return out


@router.post("/client/{client_id}/users", response_model=PortalUserOut, status_code=201)
async def add_portal_user(
    client_id: str,
    request: PortalUserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.modules.auth.service import hash_password
    existing_user = (await db.execute(
        select(User).where(User.email == request.email)
    )).scalar_one_or_none()

    if existing_user:
        user = existing_user
    else:
        user = User(
            email=request.email,
            name=request.name,
            password_hash=hash_password(request.password),
            language_code="en",
        )
        db.add(user)
        await db.flush()

    conflict = (await db.execute(
        select(ClientUser).where(
            ClientUser.client_id == client_id,
            ClientUser.user_id == user.id,
            ClientUser.role == request.role,
        )
    )).scalar_one_or_none()
    if conflict:
        raise HTTPException(status_code=409, detail="This user already has this role for this client")

    cu = ClientUser(
        client_id=client_id,
        user_id=user.id,
        role=request.role,
        status=StatusEnum.ACTIVE,
    )
    db.add(cu)
    await db.commit()
    await db.refresh(cu)

    return PortalUserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=cu.role.value,
        status=cu.status.value,
        created_at=cu.created_at,
    )
