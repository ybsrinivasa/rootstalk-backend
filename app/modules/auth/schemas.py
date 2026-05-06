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
    portal_role: Optional[str] = None
    pwa_roles: List[str] = []
    is_sa: bool = False  # True iff email matches settings.sa_email

    class Config:
        from_attributes = True
