import logging
import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.config import settings
from app.modules.auth.schemas import (
    PhoneOtpRequest, PhoneOtpVerify, AdminLoginRequest, TokenResponse, UserOut
)
from app.modules.auth.service import (
    create_phone_otp, verify_phone_otp, get_or_create_farmer,
    get_user_by_email, verify_password, hash_password, _build_token, get_user_by_id,
    start_new_session,
)
from app.modules.auth.models import EmailOTP
from app.modules.clients.service import _send_email
from app.modules.clients.models import Client, ClientUser, ClientStatus
from app.modules.platform.models import User, StatusEnum
from app.services.sms_service import send_otp_sms
from app.dependencies import get_current_user

EMAIL_OTP_EXPIRY_MINUTES = 10

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])


async def _check_client_user(db: AsyncSession, user: User, short_name: str) -> None:
    """When logging in via client portal: verify client is ACTIVE and user belongs to it."""
    client = (await db.execute(
        select(Client).where(Client.short_name == short_name.lower(), Client.status == ClientStatus.ACTIVE)
    )).scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=403, detail="This company account is inactive or not found")
    cu = (await db.execute(
        select(ClientUser).where(
            ClientUser.client_id == client.id,
            ClientUser.user_id == user.id,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalar_one_or_none()
    if not cu:
        raise HTTPException(status_code=401, detail="This email is not registered with this company")


# ── PWA: Phone OTP ─────────────────────────────────────────────────────────────

@router.post("/request-otp")
async def request_otp(request: PhoneOtpRequest, db: AsyncSession = Depends(get_db)):
    """Step 1: generate and send OTP to farmer's phone via Draft4SMS."""
    otp_code = await create_phone_otp(db, request.phone)

    if settings.environment == "development":
        logger.info(f"[DEV] OTP for {request.phone}: {otp_code}")
        # In dev, also attempt SMS if key is configured — useful for testing with real phones
        if settings.draft_sms_key:
            await send_otp_sms(request.phone, otp_code)
        return {"detail": "OTP sent.", "dev_otp": otp_code}

    sent = await send_otp_sms(request.phone, otp_code)
    if not sent:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to send OTP. Please try again.",
        )
    return {"detail": "OTP sent."}


@router.post("/verify-otp", response_model=TokenResponse)
async def verify_otp(request: PhoneOtpVerify, db: AsyncSession = Depends(get_db)):
    """Step 2: verify OTP and return JWT. Creates user if first login.

    If the account is within the 30-day grace period (deleted_at set),
    auto-restore it on login. Beyond that, get_user_by_phone hides the
    record and a fresh account is created.
    """
    valid = await verify_phone_otp(db, request.phone, request.otp_code)
    if not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired OTP")

    user = await get_or_create_farmer(db, request.phone)
    # Auto-restore if soft-deleted within grace window
    if user.deleted_at:
        user.deleted_at = None
    await start_new_session(db, user)
    return TokenResponse(access_token=_build_token(user))


# ── Admin Portal: Email + Password ─────────────────────────────────────────────

@router.post("/admin/login", response_model=TokenResponse)
async def admin_login(request: AdminLoginRequest, db: AsyncSession = Depends(get_db)):
    """SA and portal user login (email + password). Pass client_short_name for client portal logins."""
    user = await get_user_by_email(db, request.email)
    if not user or not user.password_hash:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if request.client_short_name:
        await _check_client_user(db, user, request.client_short_name)
    await start_new_session(db, user)
    return TokenResponse(access_token=_build_token(user))


# ── Portal: Email OTP login ────────────────────────────────────────────────────

@router.post("/admin/request-email-otp")
async def request_email_otp(data: dict, db: AsyncSession = Depends(get_db)):
    """Request a 6-digit OTP sent to the user's registered email. purpose: LOGIN or RESET."""
    email = (data.get("email") or "").strip().lower()
    purpose = data.get("purpose", "LOGIN")
    client_short_name = data.get("client_short_name")
    if not email:
        raise HTTPException(status_code=422, detail="email required")
    user = await get_user_by_email(db, email)
    if not user:
        raise HTTPException(status_code=404, detail="No account found for this email")
    if client_short_name and purpose == "LOGIN":
        await _check_client_user(db, user, client_short_name)

    otp_code = "".join(secrets.choice("0123456789") for _ in range(6))
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=EMAIL_OTP_EXPIRY_MINUTES)
    otp = EmailOTP(email=email, otp_code=otp_code, purpose=purpose, expires_at=expires_at)
    db.add(otp)
    await db.commit()

    subject = "Your RootsTalk portal OTP" if purpose == "LOGIN" else "Reset your RootsTalk password"
    body_plain = f"Your OTP is: {otp_code}\nValid for {EMAIL_OTP_EXPIRY_MINUTES} minutes."
    body_html = f"""<body style="font-family:sans-serif;padding:32px">
    <h2>{"Sign in to" if purpose == "LOGIN" else "Reset password for"} RootsTalk</h2>
    <p>Your one-time code:</p>
    <div style="font-size:36px;font-weight:bold;letter-spacing:8px;color:#1A5C2A;margin:16px 0">{otp_code}</div>
    <p style="color:#666;font-size:12px">Valid for {EMAIL_OTP_EXPIRY_MINUTES} minutes. Do not share this code.</p>
    </body>"""
    sent = _send_email(email, subject, body_html, body_plain)

    # In dev we return the OTP in the response so the developer can use
    # it directly even when SMTP isn't wired. In other envs we surface
    # the SMTP failure as a 503 so the user knows to retry — pre-fix
    # the helper swallowed the error and the API silently returned 200.
    if settings.environment == "development":
        return {"detail": "OTP sent", "dev_otp": otp_code}
    if not sent:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to send email OTP. Please try again or contact support.",
        )
    return {"detail": "OTP sent", "dev_otp": None}


@router.post("/admin/verify-email-otp", response_model=TokenResponse)
async def verify_email_otp(data: dict, db: AsyncSession = Depends(get_db)):
    """Verify the OTP and return a JWT. Works for LOGIN and RESET purposes."""
    email = (data.get("email") or "").strip().lower()
    code = (data.get("otp_code") or "").strip()
    if not email or not code:
        raise HTTPException(status_code=422, detail="email and otp_code required")

    now = datetime.now(timezone.utc)
    otp = (await db.execute(
        select(EmailOTP).where(
            EmailOTP.email == email,
            EmailOTP.otp_code == code,
            EmailOTP.used == False,
            EmailOTP.expires_at > now,
        ).order_by(EmailOTP.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    if not otp:
        raise HTTPException(status_code=401, detail="Invalid or expired OTP")

    otp.used = True
    user = await get_user_by_email(db, email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    client_short_name = data.get("client_short_name")
    if client_short_name and otp.purpose == "LOGIN":
        await _check_client_user(db, user, client_short_name)
    await db.commit()
    await start_new_session(db, user)
    return TokenResponse(access_token=_build_token(user))


# ── Forgot & Change Password ────────────────────────────────────────────────────

@router.post("/admin/forgot-password")
async def forgot_password(data: dict, db: AsyncSession = Depends(get_db)):
    """Sends a password reset OTP to the user's registered email."""
    email = (data.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="email required")
    # Silently succeed even if no account — prevents email enumeration
    user = await get_user_by_email(db, email)
    if user:
        otp_code = "".join(secrets.choice("0123456789") for _ in range(6))
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=EMAIL_OTP_EXPIRY_MINUTES)
        db.add(EmailOTP(email=email, otp_code=otp_code, purpose="RESET", expires_at=expires_at))
        await db.commit()
        sent = _send_email(
            email,
            "Reset your RootsTalk password",
            f"<body style='font-family:sans-serif;padding:32px'><h2>Password Reset</h2><p>Your reset code: <strong style='font-size:28px;color:#1A5C2A;letter-spacing:6px'>{otp_code}</strong></p><p style='color:#666;font-size:12px'>Valid for {EMAIL_OTP_EXPIRY_MINUTES} minutes.</p></body>",
            f"Your password reset OTP: {otp_code} (valid {EMAIL_OTP_EXPIRY_MINUTES} min)",
        )
        if settings.environment == "development":
            return {"detail": "OTP sent", "dev_otp": otp_code}
        # Production: surface the SMTP failure to the user instead of
        # the silent "OTP sent" lie, but only if the email actually
        # exists — for unknown emails we still return the generic
        # message to prevent enumeration.
        if not sent:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Failed to send reset email. Please try again or contact support.",
            )
    return {"detail": "If this email is registered, you will receive a reset code shortly"}


@router.post("/admin/reset-password")
async def reset_password(data: dict, db: AsyncSession = Depends(get_db)):
    """Verify reset OTP and set new password."""
    email = (data.get("email") or "").strip().lower()
    code = (data.get("otp_code") or "").strip()
    new_password = data.get("new_password", "")
    if not email or not code or not new_password:
        raise HTTPException(status_code=422, detail="email, otp_code, and new_password required")
    if len(new_password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    now = datetime.now(timezone.utc)
    otp = (await db.execute(
        select(EmailOTP).where(
            EmailOTP.email == email,
            EmailOTP.otp_code == code,
            EmailOTP.purpose == "RESET",
            EmailOTP.used == False,
            EmailOTP.expires_at > now,
        ).order_by(EmailOTP.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not otp:
        raise HTTPException(status_code=401, detail="Invalid or expired reset code")

    otp.used = True
    user = await get_user_by_email(db, email)
    if not user:
        raise HTTPException(status_code=404)
    user.password_hash = hash_password(new_password)
    await db.commit()
    return {"detail": "Password updated. You can now sign in."}


@router.put("/admin/change-password")
async def change_password(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Authenticated user changes their own password (current + new)."""
    current = data.get("current_password", "")
    new_pw = data.get("new_password", "")
    if not current or not new_pw:
        raise HTTPException(status_code=422, detail="current_password and new_password required")
    if len(new_pw) < 8:
        raise HTTPException(status_code=422, detail="New password must be at least 8 characters")
    if not current_user.password_hash or not verify_password(current, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user = (await db.execute(select(User).where(User.id == current_user.id))).scalar_one()
    user.password_hash = hash_password(new_pw)
    await db.commit()
    return {"detail": "Password changed successfully"}


@router.post("/me/request-delete-otp")
async def request_delete_otp(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send OTP to farmer's phone to confirm account deletion."""
    if not current_user.phone:
        raise HTTPException(status_code=422, detail="No phone number on this account")
    otp_code = await create_phone_otp(db, current_user.phone)
    response = {"detail": "OTP sent to your phone"}
    if settings.environment == "development":
        response["dev_otp"] = otp_code
    return response


@router.post("/me/confirm-delete")
async def confirm_delete_account(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Verify OTP and soft-delete the account. Active subscriptions cancelled. Data retained 30 days."""
    otp_code = (data.get("otp_code") or "").strip()
    if not otp_code:
        raise HTTPException(status_code=422, detail="OTP required")
    if not current_user.phone:
        raise HTTPException(status_code=422, detail="No phone number on this account")

    valid = await verify_phone_otp(db, current_user.phone, otp_code)
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid or expired OTP")

    # Soft delete: mark deleted_at — actual anonymisation happens after 30 days
    # via the daily Celery task. Within 30 days, signing in restores the account.
    from app.modules.subscriptions.models import Subscription, SubscriptionStatus

    current_user.deleted_at = datetime.now(timezone.utc)

    # Cancel active subscriptions immediately
    subs = (await db.execute(
        select(Subscription).where(
            Subscription.farmer_user_id == current_user.id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )).scalars().all()
    for sub in subs:
        sub.status = SubscriptionStatus.CANCELLED

    # Invalidate current session — any open device gets logged out
    current_user.current_session_id = None

    await db.commit()
    return {"detail": "Account scheduled for deletion. You can restore it by signing in within 30 days."}


# ── Shared ─────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.modules.clients.models import ClientPromoter
    from app.modules.farmpundit.models import FarmPunditProfile

    cu = (await db.execute(
        select(ClientUser).where(
            ClientUser.user_id == current_user.id,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalar_one_or_none()

    # Determine PWA roles from ClientPromoter and FarmPunditProfile
    pwa_roles: list[str] = []

    promoters = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.status == "ACTIVE",
        )
    )).scalars().all()
    for p in promoters:
        role = p.promoter_type.upper()
        if role not in pwa_roles:
            pwa_roles.append(role)

    pundit = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.user_id == current_user.id)
    )).scalar_one_or_none()
    if pundit:
        pwa_roles.append("FARM_PUNDIT")

    return {
        "id": current_user.id,
        "email": current_user.email,
        "name": current_user.name,
        "phone": current_user.phone,
        "language_code": current_user.language_code,
        "roles": current_user.roles,
        "portal_role": cu.role.value if cu else None,
        "pwa_roles": pwa_roles,
        "is_sa": bool(current_user.email and current_user.email == settings.sa_email),
    }


@router.get("/me/location")
async def get_my_location(
    current_user: User = Depends(get_current_user),
):
    """Return the authenticated user's registered location fields."""
    return {
        "state_cosh_id": current_user.state_cosh_id,
        "district_cosh_id": current_user.district_cosh_id,
        "sub_district_cosh_id": current_user.sub_district_cosh_id,
    }


@router.put("/me/profile")
async def update_my_profile(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """PWA: update name and language preference after first login."""
    from sqlalchemy import select
    from app.database import get_db as _get_db
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    for field in ["name", "language_code", "state_cosh_id", "district_cosh_id",
                  "sub_district_cosh_id", "address_line", "locality", "town", "pin_code"]:
        if data.get(field) is not None:
            setattr(user, field, data[field])
    # GPS fields are Decimal
    if data.get("gps_lat") is not None:
        from decimal import Decimal
        user.gps_lat = Decimal(str(data["gps_lat"]))
    if data.get("gps_lng") is not None:
        from decimal import Decimal
        user.gps_lng = Decimal(str(data["gps_lng"]))
    await db.commit()
    return {"detail": "Profile updated"}
