from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.config import settings
from app.dependencies import get_current_user
from app.modules.platform.models import User, StatusEnum, RoleType, UserRole
from app.modules.subscriptions.models import Subscription, SubscriptionStatus
from app.modules.clients.models import (
    Client, ClientOrganisationType, ClientUser, ClientUserRole,
    ClientLocation, ClientCrop, ClientStatus, ClientPromoter,
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

@router.get("/admin/clients/check-short-name")
async def check_short_name(
    short_name: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Real-time short name uniqueness check (item #7)."""
    _require_sa(current_user)
    existing = (await db.execute(
        select(Client).where(Client.short_name == short_name.lower().strip())
    )).scalar_one_or_none()
    return {"available": existing is None, "short_name": short_name.lower().strip()}


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

    data = request.model_dump(exclude_unset=True)

    # Handle org_type_cosh_ids separately — replace the existing list
    new_org_types = data.pop("org_type_cosh_ids", None)
    if new_org_types is not None:
        existing_types = (await db.execute(
            select(ClientOrganisationType).where(ClientOrganisationType.client_id == client_id)
        )).scalars().all()
        for ot in existing_types:
            await db.delete(ot)
        for cosh_id in new_org_types:
            db.add(ClientOrganisationType(client_id=client_id, org_type_cosh_id=cosh_id))

    for field, value in data.items():
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

    old_status = client.status
    client.status = request.status

    # BL-11: Suspend or resume subscriptions when client goes inactive/active
    if request.status == ClientStatus.INACTIVE and old_status == ClientStatus.ACTIVE:
        subs_result = await db.execute(
            select(Subscription).where(
                Subscription.client_id == client_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
        )
        for sub in subs_result.scalars().all():
            sub.status = SubscriptionStatus.SUSPENDED

    elif request.status == ClientStatus.ACTIVE and old_status == ClientStatus.INACTIVE:
        subs_result = await db.execute(
            select(Subscription).where(
                Subscription.client_id == client_id,
                Subscription.status == SubscriptionStatus.SUSPENDED,
            )
        )
        for sub in subs_result.scalars().all():
            sub.status = SubscriptionStatus.ACTIVE

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
    org_types = (await db.execute(
        select(ClientOrganisationType.org_type_cosh_id).where(ClientOrganisationType.client_id == client.id)
    )).scalars().all()
    return {
        "id": client.id,
        "short_name": client.short_name,
        "display_name": client.display_name,
        "tagline": client.tagline,
        "logo_url": client.logo_url,
        "primary_colour": client.primary_colour,
        "org_type_cosh_ids": list(org_types),
    }


# ── PWA: Client info by UUID ──────────────────────────────────────────────────

@router.get("/client/{client_id}/info")
async def get_client_info_by_id(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """PWA: fetch client branding and contact info by UUID (used on home screen)."""
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    org_types = (await db.execute(
        select(ClientOrganisationType.org_type_cosh_id).where(ClientOrganisationType.client_id == client_id)
    )).scalars().all()
    return {
        "id": client.id, "short_name": client.short_name,
        "display_name": client.display_name, "tagline": client.tagline,
        "logo_url": client.logo_url, "primary_colour": client.primary_colour,
        "support_phone": client.support_phone, "office_phone": client.office_phone,
        "website": client.website, "social_links": client.social_links or {},
        "org_type_cosh_ids": list(org_types),
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


@router.delete("/client/{client_id}/locations/{location_id}", status_code=204)
async def remove_location(
    client_id: str, location_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    loc = (await db.execute(
        select(ClientLocation).where(ClientLocation.id == location_id, ClientLocation.client_id == client_id)
    )).scalar_one_or_none()
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")
    await db.delete(loc)
    await db.commit()


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


@router.delete("/client/{client_id}/crops/{crop_id}", status_code=204)
async def remove_crop(
    client_id: str, crop_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    crop = (await db.execute(
        select(ClientCrop).where(ClientCrop.id == crop_id, ClientCrop.client_id == client_id)
    )).scalar_one_or_none()
    if not crop:
        raise HTTPException(status_code=404, detail="Crop not found")
    await db.delete(crop)
    await db.commit()


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


@router.put("/client/{client_id}/users/{user_id}/status")
async def toggle_portal_user_status(
    client_id: str, user_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    new_status = data.get("status")  # "ACTIVE" or "INACTIVE"
    if new_status not in ("ACTIVE", "INACTIVE"):
        raise HTTPException(status_code=422, detail="status must be ACTIVE or INACTIVE")
    cu = (await db.execute(
        select(ClientUser).where(ClientUser.client_id == client_id, ClientUser.user_id == user_id)
    )).scalar_one_or_none()
    if not cu:
        raise HTTPException(status_code=404, detail="User not found")
    cu.status = StatusEnum.ACTIVE if new_status == "ACTIVE" else StatusEnum.INACTIVE
    await db.commit()
    return {"detail": f"User status set to {new_status}"}


# ── CA: Self-serve company profile ─────────────────────────────────────────────

@router.get("/client/{client_id}/profile")
async def get_client_profile(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    org_types = (await db.execute(
        select(ClientOrganisationType.org_type_cosh_id).where(ClientOrganisationType.client_id == client_id)
    )).scalars().all()
    return {
        "id": client.id, "short_name": client.short_name, "display_name": client.display_name,
        "tagline": client.tagline, "logo_url": client.logo_url,
        "primary_colour": client.primary_colour, "secondary_colour": client.secondary_colour,
        "hq_address": client.hq_address, "gst_number": client.gst_number, "pan_number": client.pan_number,
        "website": client.website, "support_phone": client.support_phone, "office_phone": client.office_phone,
        "social_links": client.social_links or {},
        "org_type_cosh_ids": list(org_types),
        "ca_name": client.ca_name, "ca_email": client.ca_email,
        "status": client.status.value, "approved_at": client.approved_at,
    }


@router.put("/client/{client_id}/profile")
async def update_client_profile(
    client_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA self-serve: update company branding and contact info. GST and PAN are read-only."""
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    editable = [
        "display_name", "tagline", "logo_url", "primary_colour", "secondary_colour",
        "hq_address", "website", "support_phone", "office_phone", "social_links",
    ]
    for field in editable:
        if field in data and data[field] is not None:
            setattr(client, field, data[field])
    await db.commit()
    await db.refresh(client)
    return {"detail": "Profile updated"}


# ── Field Manager: Dealers and Facilitators ────────────────────────────────────

@router.get("/client/{client_id}/field-manager/promoters")
async def list_promoters(
    client_id: str,
    promoter_type: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(ClientPromoter, User).join(User, User.id == ClientPromoter.user_id).where(
        ClientPromoter.client_id == client_id
    )
    if promoter_type:
        q = q.where(ClientPromoter.promoter_type == promoter_type.upper())
    result = await db.execute(q.order_by(ClientPromoter.registered_at.desc()))
    rows = result.all()
    return [
        {
            "id": cp.id, "user_id": user.id,
            "name": user.name, "phone": user.phone, "email": user.email,
            "promoter_type": cp.promoter_type, "status": cp.status,
            "territory_notes": cp.territory_notes, "registered_at": cp.registered_at,
        }
        for cp, user in rows
    ]


@router.post("/client/{client_id}/field-manager/promoters", status_code=201)
async def register_promoter(
    client_id: str,
    request: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Register a dealer or facilitator with this client. Creates user account if needed."""
    from app.modules.platform.models import RoleType, UserRole
    from app.modules.auth.service import hash_password
    import secrets

    phone = request.get("phone")
    name = request.get("name")
    promoter_type = request.get("promoter_type", "DEALER").upper()
    territory_notes = request.get("territory_notes")

    if promoter_type not in ("DEALER", "FACILITATOR"):
        raise HTTPException(status_code=422, detail="promoter_type must be DEALER or FACILITATOR")

    # Find or create user by phone
    existing_user = None
    if phone:
        existing_user = (await db.execute(
            select(User).where(User.phone == phone)
        )).scalar_one_or_none()

    if existing_user:
        user = existing_user
    else:
        user = User(
            phone=phone,
            name=name,
            language_code="en",
        )
        db.add(user)
        await db.flush()

    # Assign system role (DEALER / FACILITATOR) if not already
    role_type = RoleType.DEALER if promoter_type == "DEALER" else RoleType.FACILITATOR
    existing_role = (await db.execute(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role_type == role_type)
    )).scalar_one_or_none()
    if not existing_role:
        db.add(UserRole(user_id=user.id, role_type=role_type))

    # Link to this client
    existing_cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.client_id == client_id,
            ClientPromoter.user_id == user.id,
            ClientPromoter.promoter_type == promoter_type,
        )
    )).scalar_one_or_none()
    if existing_cp:
        raise HTTPException(status_code=409, detail="This person is already registered as a {promoter_type} for this client")

    cp = ClientPromoter(
        client_id=client_id,
        user_id=user.id,
        promoter_type=promoter_type,
        territory_notes=territory_notes,
        registered_by=current_user.id,
    )
    db.add(cp)
    await db.commit()
    await db.refresh(cp)

    return {
        "id": cp.id, "user_id": user.id,
        "name": user.name, "phone": user.phone,
        "promoter_type": cp.promoter_type, "status": cp.status,
        "territory_notes": cp.territory_notes, "registered_at": cp.registered_at,
    }


@router.put("/client/{client_id}/field-manager/promoters/{promoter_id}/deactivate")
async def deactivate_promoter(
    client_id: str,
    promoter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cp = (await db.execute(
        select(ClientPromoter).where(ClientPromoter.id == promoter_id, ClientPromoter.client_id == client_id)
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Promoter not found")
    cp.status = "INACTIVE"
    await db.commit()
    return {"status": "INACTIVE"}


# ── Field Manager: Get farmers for assignment ──────────────────────────────────

@router.get("/client/{client_id}/field-manager/farmers")
async def list_client_farmers(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all farmers who have subscriptions with this client."""
    from app.modules.subscriptions.models import Subscription
    result = await db.execute(
        select(Subscription, User)
        .join(User, User.id == Subscription.farmer_user_id)
        .where(Subscription.client_id == client_id)
        .order_by(Subscription.created_at.desc())
    )
    rows = result.all()
    # Deduplicate by farmer_user_id
    seen = set()
    out = []
    for sub, user in rows:
        if user.id not in seen:
            seen.add(user.id)
            out.append({
                "user_id": user.id, "name": user.name, "phone": user.phone,
                "subscription_id": sub.id, "package_id": sub.package_id,
                "subscription_status": sub.status,
                "crop_start_date": sub.crop_start_date,
            })
    return out


# ── Client Portal: Alerts dashboard ───────────────────────────────────────────

@router.get("/client/{client_id}/alerts/pending-start-dates")
async def get_pending_start_dates(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmers with ACTIVE subscriptions but no crop start date set."""
    from app.modules.subscriptions.models import Subscription, SubscriptionStatus
    result = await db.execute(
        select(Subscription, User)
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            Subscription.client_id == client_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.crop_start_date == None,  # noqa: E711
        )
        .order_by(Subscription.created_at)
    )
    return [
        {
            "subscription_id": sub.id,
            "farmer_name": user.name,
            "farmer_phone": user.phone,
            "package_id": sub.package_id,
            "subscribed_at": sub.subscription_date,
        }
        for sub, user in result.all()
    ]


@router.get("/client/{client_id}/alerts/overdue-inputs")
async def get_overdue_inputs(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmers whose input practices are due today but have no active order (simplified check)."""
    from app.modules.subscriptions.models import Subscription, SubscriptionStatus
    from app.modules.orders.models import Order, OrderStatus
    from app.modules.advisory.models import Timeline, Practice, PracticeL0
    from datetime import date
    today = date.today()

    result = await db.execute(
        select(Subscription, User)
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            Subscription.client_id == client_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.crop_start_date != None,  # noqa: E711
        )
    )
    rows = result.all()

    overdue = []
    for sub, user in rows:
        crop_start = sub.crop_start_date.date() if hasattr(sub.crop_start_date, 'date') else sub.crop_start_date
        day_offset = (today - crop_start).days

        tl_result = await db.execute(
            select(Timeline).where(Timeline.package_id == sub.package_id)
        )
        for tl in tl_result.scalars().all():
            from_type = tl.from_type.value if hasattr(tl.from_type, 'value') else str(tl.from_type)
            active = False
            if from_type == "DAS" and tl.from_value <= day_offset <= tl.to_value:
                active = True
            elif from_type == "DBS" and -tl.to_value <= day_offset <= -tl.from_value:
                active = True

            if active:
                p_result = await db.execute(
                    select(Practice).where(
                        Practice.timeline_id == tl.id,
                        Practice.l0_type == PracticeL0.INPUT,
                    )
                )
                if p_result.scalars().first():
                    # Check if there's an active (non-cancelled) order
                    order_result = await db.execute(
                        select(Order).where(
                            Order.subscription_id == sub.id,
                            Order.status.notin_(["CANCELLED", "EXPIRED"]),
                        )
                    )
                    if not order_result.scalar_one_or_none():
                        overdue.append({
                            "subscription_id": sub.id,
                            "farmer_name": user.name,
                            "farmer_phone": user.phone,
                            "day_offset": day_offset,
                            "timeline_name": tl.name,
                            "package_id": sub.package_id,
                        })
                        break  # One entry per subscription

    return overdue


# ═══════════════════════════════════════════════════════════════════════════════
# SA: CM Client Assignments
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/clients/{client_id}/cm-assignment")
async def get_cm_assignment(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the current CM assignment for a client."""
    _require_sa(current_user)
    assignment = (await db.execute(
        select(CMClientAssignment).where(
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
        )
    )).scalar_one_or_none()
    if not assignment:
        return {"cm_user_id": None, "cm_name": None, "cm_email": None, "rights": None}
    cm = (await db.execute(select(User).where(User.id == assignment.cm_user_id))).scalar_one_or_none()
    return {
        "assignment_id": assignment.id,
        "cm_user_id": assignment.cm_user_id,
        "cm_name": cm.name if cm else None,
        "cm_email": cm.email if cm else None,
        "rights": assignment.rights.value,
        "assigned_at": assignment.assigned_at,
    }


@router.put("/admin/clients/{client_id}/cm-assignment")
async def assign_cm_to_client(
    client_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA: assign or update CM for a client. One CM per client at a time."""
    _require_sa(current_user)
    cm_user_id = data.get("cm_user_id")
    rights = data.get("rights", "EDIT")
    if not cm_user_id:
        raise HTTPException(status_code=422, detail="cm_user_id required")

    # Verify the user is a Content Manager
    cm_role = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == cm_user_id,
            UserRole.role_type == RoleType.CONTENT_MANAGER,
        )
    )).scalar_one_or_none()
    if not cm_role:
        raise HTTPException(status_code=400, detail="User is not a Content Manager")

    # Deactivate any existing assignment for this client
    existing = (await db.execute(
        select(CMClientAssignment).where(
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
        )
    )).scalar_one_or_none()
    if existing:
        if existing.cm_user_id == cm_user_id:
            existing.rights = CMRights(rights)
            await db.commit()
            return {"detail": "Rights updated", "cm_user_id": cm_user_id, "rights": rights}
        existing.status = StatusEnum.INACTIVE

    assignment = CMClientAssignment(
        cm_user_id=cm_user_id,
        client_id=client_id,
        rights=CMRights(rights),
        status=StatusEnum.ACTIVE,
    )
    db.add(assignment)
    await db.commit()
    return {"detail": "CM assigned", "cm_user_id": cm_user_id, "rights": rights}


@router.delete("/admin/clients/{client_id}/cm-assignment")
async def remove_cm_assignment(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA: remove CM from a client."""
    _require_sa(current_user)
    assignment = (await db.execute(
        select(CMClientAssignment).where(
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
        )
    )).scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="No active CM assignment")
    assignment.status = StatusEnum.INACTIVE
    await db.commit()
    return {"detail": "CM assignment removed"}


@router.get("/admin/cm/my-clients")
async def cm_my_clients(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CM: list all clients assigned to me with rights level."""
    assignments = (await db.execute(
        select(CMClientAssignment).where(
            CMClientAssignment.cm_user_id == current_user.id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
        )
    )).scalars().all()
    out = []
    for a in assignments:
        client = (await db.execute(select(Client).where(Client.id == a.client_id))).scalar_one_or_none()
        if client:
            out.append({
                "client_id": client.id,
                "full_name": client.full_name,
                "display_name": client.display_name,
                "short_name": client.short_name,
                "logo_url": client.logo_url,
                "primary_colour": client.primary_colour,
                "status": client.status.value,
                "rights": a.rights.value,
                "assigned_at": a.assigned_at,
                "portal_url": f"https://rootstalk.in/{client.short_name}",
            })
    return out
