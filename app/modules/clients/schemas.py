from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime
from app.modules.clients.models import ClientStatus, ClientUserRole, CMRights, CMPrivilege
from app.modules.platform.models import StatusEnum


# ── SA initiates onboarding ────────────────────────────────────────────────────

class ClientInitiate(BaseModel):
    full_name: str
    short_name: str
    ca_name: str
    ca_phone: str
    ca_email: EmailStr
    is_manufacturer: bool = False


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
    full_name: Optional[str] = None
    display_name: Optional[str] = None
    tagline: Optional[str] = None
    ca_name: Optional[str] = None
    ca_phone: Optional[str] = None
    is_manufacturer: Optional[bool] = None


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
    status: ClientStatus
    ca_name: str
    ca_phone: str
    ca_email: str
    rejection_reason: Optional[str] = None
    approved_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class OnboardingLinkOut(BaseModel):
    client_id: str
    short_name: str
    onboarding_link: str
    expires_at: datetime
