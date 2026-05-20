from pydantic import BaseModel, EmailStr
from typing import Optional, List
from app.modules.platform.models import RoleType, StatusEnum


# ── PWA (Phone OTP) ────────────────────────────────────────────────────────────

class PhoneOtpRequest(BaseModel):
    phone: str


class PhoneOtpVerify(BaseModel):
    phone: str
    otp_code: str


# ── Admin Portal (Email + Password) ────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str
    client_short_name: str | None = None


# ── Shared ─────────────────────────────────────────────────────────────────────

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RoleOut(BaseModel):
    role_type: RoleType
    status: StatusEnum

    class Config:
        from_attributes = True


class UserOut(BaseModel):
    id: str
    phone: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    language_code: str
    roles: List[RoleOut] = []
    portal_role: Optional[str] = None  # deprecated; use portal_roles
    portal_roles: List[str] = []  # all ACTIVE ClientUser roles at the bound client
    pwa_roles: List[str] = []
    is_sa: bool = False  # True iff email matches settings.sa_email
    # Tenant binding (2026-05-18). Surfaced from the JWT claim set at
    # portal-login time. Frontend MUST seed its local rt_cp_client
    # from these — pre-login branding fetches drift; the token can't.
    client_id: Optional[str] = None
    client_short_name: Optional[str] = None
    # CM impersonation flag (2026-05-18). True when the bound user
    # has an ACTIVE CM-EDIT assignment for the bound client. Frontend
    # uses this to grant full CA-equivalent sidebar visibility.
    is_cm_for_this_client: bool = False
    # Farmer profile fields (2026-05-20). Surfaced so the PWA Profile
    # page can render name + location + GPS in one `/auth/me` call
    # instead of stitching together /auth/me + /auth/me/location.
    # All optional — fresh farmer accounts won't have them set yet.
    state_cosh_id: Optional[str] = None
    district_cosh_id: Optional[str] = None
    sub_district_cosh_id: Optional[str] = None
    gps_lat: Optional[float] = None
    gps_lng: Optional[float] = None

    class Config:
        from_attributes = True
