import logging
import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, status
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
from app.modules.platform.models import User, StatusEnum, UserRole, RoleType
from app.services.sms_service import send_otp_sms
from app.dependencies import get_current_user

EMAIL_OTP_EXPIRY_MINUTES = 10

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])


def _surface_dev_otp() -> bool:
    """Return True when the OTP response should include the raw code
    for test convenience. Production NEVER leaks codes regardless of
    flag; every other environment defers to settings.surface_dev_otp
    (default True for legacy staging behaviour).

    2026-05-20: broadened from `settings.environment == "development"`
    to non-production so testing surfaced dev_otp.
    2026-06-24: gated on settings.surface_dev_otp so testing can opt
    out for demos (real SMS-only) without flipping ENVIRONMENT off
    staging — keeps CORS, dev defaults, the staging-bypass button,
    etc. intact.
    """
    if settings.environment == "production":
        return False
    return settings.surface_dev_otp


async def _check_client_user(db: AsyncSession, user: User, short_name: str) -> Client:
    """When logging in via client portal: verify client is ACTIVE and
    user belongs to it. Returns the Client row so callers can bind
    the issued JWT to this client_id (tenant isolation — see
    `_build_token` and `get_current_user`)."""
    client = (await db.execute(
        select(Client).where(
            Client.short_name == short_name.lower(),
            Client.status == ClientStatus.ACTIVE,
            # 2026-07-24 — Portal login must never resolve to a training
            # child. Training clients have synthetic short_names like
            # 'TR<hex>' and no ClientUser rows (the parent's CA acts on
            # them via CA endpoints), so a match is either accidental
            # or an attack surface. Real login flows use the parent's
            # short_name.
            Client.is_training.is_(False),
        )
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
    # 2026-09-01 — Coaching Sandbox lockout: if this workspace is a
    # coaching workspace whose session has CLOSED, refuse login for
    # every user (student + every SE/FM/RM the student invited).
    # Without this gate, a Subject Expert the student added would
    # still be able to log in after the coach closed the session and
    # continue authoring in an unsupervised "zombie" workspace.
    # Student's own login is already blocked by
    # guard_coaching_student_login; this covers every other
    # ClientUser in the same workspace.
    if client.is_coaching and client.parent_session_id:
        from app.modules.coaching.models import (
            CoachingSession, OPEN_SESSION_STATUSES,
        )
        session_status = (await db.execute(
            select(CoachingSession.status)
            .where(CoachingSession.id == client.parent_session_id)
        )).scalar_one_or_none()
        if session_status and session_status not in {s.value for s in OPEN_SESSION_STATUSES}:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "coaching_workspace_closed",
                    "message": (
                        "This coaching workspace has been closed by the "
                        "coach. If you need continued access, please "
                        "contact your coach."
                    ),
                },
            )
    return client


# ── PWA: Phone OTP ─────────────────────────────────────────────────────────────

@router.post("/request-otp")
async def request_otp(request: PhoneOtpRequest, db: AsyncSession = Depends(get_db)):
    """Step 1: generate and send OTP to farmer's phone via Draft4SMS."""
    # Coaching Sandbox guard: refuse OTP send if this phone belongs to
    # a coaching student whose session isn't ACTIVE. Prevents students
    # from triggering (and paying for) an SMS they can't complete
    # login with. Non-coaching users pass through unchanged.
    from app.modules.coaching.service import guard_otp_request_for_coaching_phone
    await guard_otp_request_for_coaching_phone(db, request.phone)

    otp_code = await create_phone_otp(db, request.phone)

    if _surface_dev_otp():
        logger.info(f"[NON-PROD] OTP for {request.phone}: {otp_code}")
        # Still attempt SMS if a gateway key is configured — useful
        # for testing real-phone delivery. Failure is non-fatal in
        # non-prod since the caller also gets dev_otp.
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
    # Coaching Sandbox guard — same rationale as /request-otp above,
    # applied here as belt-and-braces in case the OTP was requested
    # while the session was ACTIVE and consumed after it closed
    # (rare, but possible with a 30-second OTP + a 30-day session).
    from app.modules.coaching.service import guard_coaching_student_login
    await guard_coaching_student_login(db, user.id)
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
    client: Client | None = None
    if request.client_short_name:
        client = await _check_client_user(db, user, request.client_short_name)
    # Coaching Sandbox guard — student portal logins refused pre-ACTIVE
    # and post-CLOSED. Coach + non-student portal users pass through.
    from app.modules.coaching.service import guard_coaching_student_login
    await guard_coaching_student_login(db, user.id)
    await start_new_session(db, user)
    return TokenResponse(access_token=_build_token(
        user,
        client_id=client.id if client else None,
        client_short_name=client.short_name if client else None,
    ))


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

    # In non-prod we return the OTP in the response so the user can
    # log in even when SMTP isn't wired or silently drops mail. In
    # production we surface the SMTP failure as a 503 so the user
    # knows to retry — pre-fix the helper swallowed the error and
    # the API silently returned 200.
    if _surface_dev_otp():
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
    client: Client | None = None
    if client_short_name and otp.purpose == "LOGIN":
        client = await _check_client_user(db, user, client_short_name)
    # Coaching Sandbox guard — student portal-OTP logins refused
    # pre-ACTIVE / post-CLOSED, same rule as password login above.
    from app.modules.coaching.service import guard_coaching_student_login
    await guard_coaching_student_login(db, user.id)
    await db.commit()
    await start_new_session(db, user)
    return TokenResponse(access_token=_build_token(
        user,
        client_id=client.id if client else None,
        client_short_name=client.short_name if client else None,
    ))


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
        if _surface_dev_otp():
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
    if _surface_dev_otp():
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


# ── Self-claim a PWA ecosystem role ────────────────────────────────────────────

# Roles a User can claim for themselves via PWA. FARMER is implicit
# (every PWA user starts as one); FARM_PUNDIT has its own richer
# registration flow at `/pundit/profile`; the rest (CONTENT_MANAGER,
# RELATIONSHIP_MANAGER, BUSINESS_MANAGER) are Neytiri-side internal
# roles assigned by the SA admin UI, not self-claimable.
SELF_CLAIMABLE_ROLES = {"DEALER", "FACILITATOR"}


@router.post("/me/claim-role", status_code=200)
async def claim_role(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Self-claim a PWA ecosystem role (Dealer / Facilitator).

    Per the five-ecosystem architecture (see five_ecosystems memory):
    a user *registers themselves* in their ecosystem before any
    company recognises them. This endpoint creates the UserRole row
    that flips the role on for the PWA. Profile completion happens
    on the role-specific profile page (/dealer/profile or
    /facilitator/profile) which already exists.

    The user can act in their ecosystem after this — but most
    consequential actions (receiving orders, assigning advisories
    as a Promoter) gate on company-onboarding via ClientPromoter,
    which is a separate step the FM does. Until then, the user is
    "available for company recognition."

    Idempotent: claiming an already-held role is a 200 no-op so
    pages can fire-and-forget on landing.
    """
    role_str = (data.get("role") or "").upper()
    if role_str not in SELF_CLAIMABLE_ROLES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "role_not_self_claimable",
                "message": (
                    f"Only {sorted(SELF_CLAIMABLE_ROLES)} can be self-claimed. "
                    "FARMER is implicit; FARM_PUNDIT has its own registration "
                    "flow; internal roles are assigned by RootsTalk admin."
                ),
            },
        )

    role_type = RoleType[role_str]

    # 2026-06-23 — Dealer + Facilitator exclusivity. These two roles
    # sit on opposite ends of the order-routing chain (facilitators
    # route farmer orders to dealers). Letting the same user hold
    # both creates a conflict of interest: every order they route as
    # facilitator can land at their own dealer shop. All other role
    # combinations are fine; only this pair is mutually exclusive.
    # Per the "first deliberate exception" to
    # feedback_users_wear_multiple_role_hats.md.
    #
    # 2026-09-03 — Coaching Sandbox bypass. Students need to
    # practise cross-role scenarios (farmer sends order to their own
    # dealer / facilitator persona). The conflict-of-interest concern
    # doesn't apply inside an isolated coaching workspace — every
    # order originates from and routes to the same student. Real
    # users still hit the exclusion below.
    from app.modules.coaching.service import get_coaching_student_for_user
    coaching_student = await get_coaching_student_for_user(db, current_user.id)
    CONFLICT = {
        RoleType.DEALER: RoleType.FACILITATOR,
        RoleType.FACILITATOR: RoleType.DEALER,
    }
    if role_type in CONFLICT and coaching_student is None:
        conflicting = CONFLICT[role_type]
        has_conflict_role = (await db.execute(
            select(UserRole).where(
                UserRole.user_id == current_user.id,
                UserRole.role_type == conflicting,
            )
        )).scalar_one_or_none() is not None
        # 2026-06-23 — Widen past UserRole. Legacy sessions may have
        # `dealer_profile_complete` true or `facilitator_declared_at`
        # set without the corresponding UserRole row (e.g. row went
        # INACTIVE, predates self-claim, or was created via a path
        # that didn't write the role). Catch those too so the gate
        # cannot be sidestepped.
        has_conflict_legacy = False
        if conflicting == RoleType.DEALER:
            has_conflict_legacy = await _is_dealer_profile_complete(db, current_user.id)
        elif conflicting == RoleType.FACILITATOR:
            has_conflict_legacy = current_user.facilitator_declared_at is not None
        if has_conflict_role or has_conflict_legacy:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "dealer_facilitator_exclusive",
                    "message": (
                        f"You already hold the {conflicting.value} role. "
                        f"A single user cannot be both a Dealer and a "
                        f"Facilitator — they sit on opposite ends of the "
                        f"order-routing chain. Ability to end a role "
                        f"voluntarily is coming in a future update."
                    ),
                    "current_role": conflicting.value,
                    "requested_role": role_type.value,
                },
            )

    existing = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == current_user.id,
            UserRole.role_type == role_type,
        )
    )).scalar_one_or_none()
    if existing is None:
        db.add(UserRole(user_id=current_user.id, role_type=role_type))
        await db.commit()

    return {"role": role_str, "status": "ACTIVE"}


@router.post("/me/facilitator-declaration", status_code=200)
async def confirm_facilitator_declaration(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stamp users.facilitator_declared_at = now(). Required before
    the PWA lets the user into /facilitator/home.

    Idempotent: re-confirming preserves the original timestamp so
    we don't reset the moment of declaration when the page is
    re-saved (e.g. a noisy onClick).

    Caller must already hold the FACILITATOR UserRole (granted via
    /auth/me/claim-role); 409 otherwise so the PWA can route the
    user back through /become-facilitator first.
    """
    from datetime import datetime as _dt, timezone as _tz
    has_role = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == current_user.id,
            UserRole.role_type == RoleType.FACILITATOR,
            UserRole.status == StatusEnum.ACTIVE,
        )
    )).scalar_one_or_none() is not None
    if not has_role:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "facilitator_role_not_claimed",
                "message": "Register as a Facilitator first.",
            },
        )
    if current_user.facilitator_declared_at is None:
        current_user.facilitator_declared_at = _dt.now(_tz.utc)
        await db.commit()
    return {"facilitator_declared_at": current_user.facilitator_declared_at}


# ── Shared ─────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserOut)
async def get_me(request: Request, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.modules.clients.models import ClientPromoter
    from app.modules.farmpundit.models import FarmPunditProfile

    # Tenant binding pre-fetch (Batch K, 2026-05-18). With the
    # client_id claim now on the JWT, we can scope the ClientUser
    # query to the bound client AND surface ALL active roles
    # (previously the code did scalar_one_or_none, which crashed
    # on users with multiple roles — a latent bug exposed by the
    # multi-role rule).
    token_client_id_pre = getattr(request.state, "token_client_id", None)
    cu_query = select(ClientUser).where(
        ClientUser.user_id == current_user.id,
        ClientUser.status == StatusEnum.ACTIVE,
    )
    if token_client_id_pre:
        cu_query = cu_query.where(ClientUser.client_id == token_client_id_pre)
    cus = (await db.execute(cu_query)).scalars().all()
    # Preserve the legacy single `portal_role` field for backward
    # compat — caller can migrate to `portal_roles` (the new list).
    cu = cus[0] if cus else None
    portal_roles = sorted({c.role.value for c in cus})

    # CM impersonation (2026-05-18). When a CM SSOs into the CA
    # Portal via /admin/cm/clients/{cid}/login-as, the JWT carries
    # `client_id` but the CM has no ClientUser row for that client —
    # they're SA-side staff. Surface their ACTIVE CM-EDIT assignment
    # as the synthetic role `CONTENT_MANAGER` in portal_roles so the
    # CA Portal sidebar renders full CA-equivalent access. Per user:
    # "The CM will have all the privileges inside the Client — that
    # of the CA, Subject Experts, and all other roles."
    is_cm_for_this_client = False
    if token_client_id_pre:
        from app.modules.clients.models import CMClientAssignment, CMRights
        cm_assignment = (await db.execute(
            select(CMClientAssignment.id).where(
                CMClientAssignment.cm_user_id == current_user.id,
                CMClientAssignment.client_id == token_client_id_pre,
                CMClientAssignment.status == StatusEnum.ACTIVE,
                CMClientAssignment.rights == CMRights.EDIT,
            ).limit(1)
        )).scalar_one_or_none()
        if cm_assignment is not None:
            is_cm_for_this_client = True
            if "CONTENT_MANAGER" not in portal_roles:
                portal_roles = sorted([*portal_roles, "CONTENT_MANAGER"])

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

    # Self-claimed roles (DEALER / FACILITATOR) — added 2026-05-09
    # alongside the /me/claim-role endpoint. Without this, a user
    # who registered as a Dealer but isn't yet onboarded by any
    # company wouldn't see DEALER in their pwa_roles and the PWA
    # would gate them out of /dealer/* pages.
    self_claimed = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == current_user.id,
            UserRole.status == StatusEnum.ACTIVE,
            UserRole.role_type.in_([RoleType.DEALER, RoleType.FACILITATOR]),
        )
    )).scalars().all()
    for r in self_claimed:
        role = r.role_type.value if hasattr(r.role_type, "value") else str(r.role_type)
        if role not in pwa_roles:
            pwa_roles.append(role)

    pundit = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.user_id == current_user.id)
    )).scalar_one_or_none()
    if pundit:
        pwa_roles.append("FARM_PUNDIT")

    # Tenant binding (2026-05-18). When the user logged in via a
    # client portal the JWT carries client_id + client_short_name
    # claims — surface them here as the authoritative source for the
    # frontend's `setClient` call. Pre-login branding fetches can
    # drift; the token can't.
    token_client_id = token_client_id_pre
    token_client_short_name = getattr(request.state, "token_client_short_name", None)

    # Coaching Sandbox context (2026-09-01). Returns non-null only
    # for users who are coaching students in a currently-OPEN session.
    # PWA + Portal use this to render the persistent purple banner
    # 'You're in a coaching session — this is practice.' Non-students
    # get None and no banner ever renders.
    from app.modules.coaching.service import load_coaching_context
    coaching_context = await load_coaching_context(db, current_user)

    return {
        "id": current_user.id,
        "email": current_user.email,
        "name": current_user.name,
        "phone": current_user.phone,
        "language_code": current_user.language_code,
        "roles": current_user.roles,
        "portal_role": cu.role.value if cu else None,
        "portal_roles": portal_roles,
        "pwa_roles": pwa_roles,
        "is_sa": bool(current_user.email and current_user.email == settings.sa_email),
        "client_id": token_client_id,
        "client_short_name": token_client_short_name,
        "is_cm_for_this_client": is_cm_for_this_client,
        # 2026-05-20: PWA Profile + farmer-context screens read these
        # from the cached /auth/me so they don't need a second hop
        # to /auth/me/location.
        "state_cosh_id": current_user.state_cosh_id,
        "district_cosh_id": current_user.district_cosh_id,
        "sub_district_cosh_id": current_user.sub_district_cosh_id,
        "gps_lat": float(current_user.gps_lat) if current_user.gps_lat is not None else None,
        "gps_lng": float(current_user.gps_lng) if current_user.gps_lng is not None else None,
        "photo_url": current_user.photo_url,
        # PWA gate: /facilitator/home checks this before letting
        # the user in. Self-claiming the FACILITATOR role grants
        # /facilitator/profile access; declaring there sets this
        # timestamp and unlocks /facilitator/home.
        "facilitator_declared_at": current_user.facilitator_declared_at,
        # PWA gate: drawer + /dealer/home check this. True only
        # when the user has a DealerProfile row with every
        # required field filled (mirrors GET /dealer/profile's
        # is_profile_complete). Lets the right-drawer show
        # "Open my Shop / Set up →" instead of "Switch to my
        # Shop" until the setup is finished.
        "dealer_profile_complete": await _is_dealer_profile_complete(db, current_user.id),
        # 2026-06-24 — Live pricing exposed so the PWA can render
        # the actual amount in confirm / payment screens instead of
        # the hardcoded "Rs. 199". Server is the source of truth;
        # PWA reads these on /auth/me load and feeds them into the
        # subscribe + ask-expert i18n templates.
        "subscription_amount_inr": settings.subscription_amount_paise // 100,
        "query_amount_inr": settings.query_amount_paise // 100,
        "coaching_context": coaching_context,
    }


async def _is_dealer_profile_complete(db, user_id: str) -> bool:
    """Mirror of orders.router._dealer_profile_complete — kept here
    to avoid pulling the orders router into auth at import time."""
    from app.modules.orders.models import DealerProfile
    profile = (await db.execute(
        select(DealerProfile).where(DealerProfile.user_id == user_id)
    )).scalar_one_or_none()
    if profile is None:
        return False
    return (
        bool(profile.shop_name and profile.shop_name.strip())
        and bool(profile.shop_address and profile.shop_address.strip())
        and bool(profile.sell_categories)
        and profile.shop_gps_lat is not None
        and profile.shop_gps_lng is not None
        and bool(profile.shop_registration_url and profile.shop_registration_url.strip())
        and bool(profile.shop_photo_url and profile.shop_photo_url.strip())
    )


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
                  "sub_district_cosh_id", "address_line", "locality", "town", "pin_code",
                  "photo_url"]:
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
