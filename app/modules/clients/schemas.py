from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime
from app.modules.clients.models import (
    ClientStatus, ClientUserRole, CMRights, CMPrivilege, PaymentModel,
)
from app.modules.platform.models import StatusEnum


# ── SA initiates onboarding ────────────────────────────────────────────────────

class ClientInitiate(BaseModel):
    full_name: str
    short_name: str
    ca_name: str
    ca_phone: str
    ca_email: EmailStr
    is_manufacturer: bool = False
    payment_model: PaymentModel  # mandatory — spec §11.1


# ── CA submits their side ──────────────────────────────────────────────────────

class ClientCASubmit(BaseModel):
    display_name: str
    tagline: Optional[str] = None
    primary_colour: str
    secondary_colour: Optional[str] = None
    hq_address: str
    gst_number: str
    pan_number: str
    website: Optional[str] = None
    support_phone: Optional[str] = None
    office_phone: Optional[str] = None
    social_links: Optional[dict] = None
    org_type_cosh_ids: List[str] = []


# ── SA approves / rejects ──────────────────────────────────────────────────────

class ClientApprove(BaseModel):
    pass


class ClientReject(BaseModel):
    reason: str


# ── SA edits ───────────────────────────────────────────────────────────────────

class ClientEdit(BaseModel):
    # SA-side fields
    full_name: Optional[str] = None
    ca_name: Optional[str] = None
    ca_phone: Optional[str] = None
    ca_email: Optional[EmailStr] = None
    is_manufacturer: Optional[bool] = None
    payment_model: Optional[PaymentModel] = None
    # CA-side fields (SA can update post-approval)
    display_name: Optional[str] = None
    tagline: Optional[str] = None
    logo_url: Optional[str] = None
    primary_colour: Optional[str] = None
    secondary_colour: Optional[str] = None
    hq_address: Optional[str] = None
    website: Optional[str] = None
    support_phone: Optional[str] = None
    office_phone: Optional[str] = None
    social_links: Optional[dict] = None
    # Org types — replaces the existing list when provided
    org_type_cosh_ids: Optional[List[str]] = None


class ClientStatusUpdate(BaseModel):
    status: StatusEnum


# ── CM assignment ──────────────────────────────────────────────────────────────

class CMAssignment(BaseModel):
    cm_user_id: str
    rights: CMRights = CMRights.EDIT


class CMPrivilegeGrant(BaseModel):
    privilege: CMPrivilege


# ── Output ─────────────────────────────────────────────────────────────────────

class ClientOut(BaseModel):
    id: str
    full_name: str
    short_name: str
    display_name: Optional[str] = None
    tagline: Optional[str] = None
    logo_url: Optional[str] = None
    primary_colour: Optional[str] = None
    secondary_colour: Optional[str] = None
    gst_number: Optional[str] = None
    pan_number: Optional[str] = None
    hq_address: Optional[str] = None
    website: Optional[str] = None
    support_phone: Optional[str] = None
    office_phone: Optional[str] = None
    is_manufacturer: bool
    payment_model: PaymentModel
    status: ClientStatus
    ca_name: str
    ca_phone: str
    ca_email: str
    rejection_reason: Optional[str] = None
    approved_at: Optional[datetime] = None
    created_at: datetime
    # Computed by the route — env-driven (`{FRONTEND_BASE_URL}/login/<short_name>`).
    # Reserved Optional in the schema so legacy callers / response_model
    # validation tolerate rows where the route hasn't computed it.
    login_url: Optional[str] = None

    class Config:
        from_attributes = True


class OnboardingLinkOut(BaseModel):
    client_id: str
    short_name: str
    onboarding_link: str
    expires_at: datetime


# ── Portal: Locations ──────────────────────────────────────────────────────────

class LocationCreate(BaseModel):
    state_cosh_id: str
    district_cosh_id: str


class LocationOut(BaseModel):
    id: str
    state_cosh_id: str
    district_cosh_id: str
    status: str
    added_at: datetime

    class Config:
        from_attributes = True


# ── Portal: Crops ──────────────────────────────────────────────────────────────

class CropCreate(BaseModel):
    crop_cosh_id: str


class ClientBrandingOut(BaseModel):
    """Public branding payload for the per-client login page.

    Surfaced at GET /public/clients/{short_name}/branding so the CA
    portal can render the right logo + tagline + colours BEFORE the
    user has authenticated. Only ACTIVE clients are surfaced; non-
    existent or non-ACTIVE short_names return 404 (avoids leaking
    that pre-launch clients exist).
    """
    short_name: str
    full_name: str
    tagline: Optional[str] = None
    logo_url: Optional[str] = None
    primary_colour: Optional[str] = None
    secondary_colour: Optional[str] = None


class CropOut(BaseModel):
    id: str
    crop_cosh_id: str
    status: str  # derived: "ACTIVE" if any ACTIVE PoP exists, else "INACTIVE"
    is_active: bool  # same signal as status, typed for the portal
    added_at: datetime
    removed_at: Optional[datetime] = None
    crop_name_en: Optional[str] = None
    crop_scientific_name: Optional[str] = None
    crop_area_or_plant: Optional[str] = None

    class Config:
        from_attributes = True


# ── Portal: Users ──────────────────────────────────────────────────────────────

class PortalUserCreate(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    role: ClientUserRole
    password: str
    # Author bio (Batch D, 2026-05-18). Persists to User.designation /
    # User.professional_profile. The PWA renders these next to the
    # author's name on advisory practice cards. Both optional at
    # create time; the CA can fill them in later via the update
    # endpoint.
    designation: Optional[str] = None
    professional_profile: Optional[str] = None


class PortalUserUpdate(BaseModel):
    """PATCH-style update for portal user details (CA-managed).

    Only name + designation + professional_profile are editable here.
    Role changes go through a separate flow (CA-exclusivity gate).
    Status changes use the existing /status endpoint.
    """
    name: Optional[str] = None
    designation: Optional[str] = None
    professional_profile: Optional[str] = None


class PortalUserOut(BaseModel):
    id: str
    # User.email is nullable in the model (some test fixtures and
    # phone-only users have None). Optional here so the response
    # serialiser doesn't 500 on those rows.
    email: Optional[str] = None
    name: Optional[str] = None
    role: str
    status: str
    created_at: datetime
    designation: Optional[str] = None
    professional_profile: Optional[str] = None

    class Config:
        from_attributes = True
