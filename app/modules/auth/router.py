import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.config import settings
from app.modules.auth.schemas import (
    PhoneOtpRequest, PhoneOtpVerify, AdminLoginRequest, TokenResponse, UserOut
)
from app.modules.auth.service import (
    create_phone_otp, verify_phone_otp, get_or_create_farmer,
    get_user_by_email, verify_password, _build_token, get_user_by_id
)
from app.dependencies import get_current_user
from app.modules.platform.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])


# ── PWA: Phone OTP ─────────────────────────────────────────────────────────────

@router.post("/request-otp")
async def request_otp(request: PhoneOtpRequest, db: AsyncSession = Depends(get_db)):
    """Step 1: generate and send OTP to farmer's phone."""
    otp_code = await create_phone_otp(db, request.phone)

    if settings.environment == "development":
        logger.info(f"[DEV] OTP for {request.phone}: {otp_code}")
        return {"detail": "OTP sent.", "dev_otp": otp_code}

    # Production: send via SMS gateway (integrate later)
    # await sms_service.send(request.phone, otp_code)
    return {"detail": "OTP sent."}


@router.post("/verify-otp", response_model=TokenResponse)
async def verify_otp(request: PhoneOtpVerify, db: AsyncSession = Depends(get_db)):
    """Step 2: verify OTP and return JWT. Creates user if first login."""
    valid = await verify_phone_otp(db, request.phone, request.otp_code)
    if not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired OTP")

    user = await get_or_create_farmer(db, request.phone)
    return TokenResponse(access_token=_build_token(user))


# ── Admin Portal: Email + Password ─────────────────────────────────────────────

@router.post("/admin/login", response_model=TokenResponse)
async def admin_login(request: AdminLoginRequest, db: AsyncSession = Depends(get_db)):
    """SA and portal user login (email + password)."""
    user = await get_user_by_email(db, request.email)
    if not user or not user.password_hash:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return TokenResponse(access_token=_build_token(user))


# ── Shared ─────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user
