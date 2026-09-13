import random
import string
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, or_
from sqlalchemy.orm import selectinload
from app.config import settings
from app.modules.platform.models import User, UserRole, RoleType, StatusEnum
from app.modules.auth.models import PhoneOTP

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 2026-08-31 — Tightened from 10 minutes to 30 seconds to match the
# DLT-approved SMS template ("Valid for 30 seconds"). Aligning the
# server-side TTL with the copy prevents the mismatch where users
# who take longer than 30s see no error but their OTP would silently
# be accepted — the DLT copy is defensive/anti-social-engineering,
# and enforcing it server-side keeps the promise consistent.
OTP_EXPIRE_SECONDS = 30


# ── Passwords ──────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT ────────────────────────────────────────────────────────────────────────

def create_access_token(data: dict) -> str:
    payload = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload["exp"] = expire
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_coach_view_token(
    *,
    student_user: User,
    workspace_client_id: str,
    workspace_short_name: str,
    coach_user_id: str,
    ttl_minutes: int = 60,
) -> str:
    """Mint a short-lived, read-only JWT that lets a coach open a
    student's workspace in the CA portal without logging the student
    out or handing over the student's real credentials.

    Shape mirrors a normal portal-issued token: `sub=student.user_id`
    + `client_id` + `client_short_name` bind the token to the
    workspace. Two coach-view-specific claims:

    - `coach_view=True`: `get_current_user` refuses any mutating
      HTTP method on tokens carrying this claim (403
      coach_view_read_only) and skips the coaching-session-status
      force-logout check so the coach can revisit CLOSED workspaces.
    - `coach_view_by`: the coach's user_id, kept for audit.

    Deliberately omits `jti`: the student's single-device session
    binding stays whatever it was, and this token doesn't rotate it.
    Short TTL (default 1h) keeps blast radius small.
    """
    active_roles = [r.role_type.value for r in student_user.roles if r.status == StatusEnum.ACTIVE]
    payload = {
        "sub": student_user.id,
        "roles": active_roles,
        "client_id": workspace_client_id,
        "client_short_name": workspace_short_name,
        "coach_view": True,
        "coach_view_by": coach_user_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return None


def _build_token(
    user: User,
    *,
    client_id: Optional[str] = None,
    client_short_name: Optional[str] = None,
) -> str:
    """Build a JWT for the user.

    When the user logs in via a client portal (i.e. with a
    `client_short_name`), the token carries `client_id` +
    `client_short_name` claims and the access scope is bound to
    that single client for the session. Calls to `/client/{cid}/...`
    with cid != token.client_id are refused 403 cross_client_forbidden
    in `get_current_user`.

    SA / CM / PWA logins (no client_short_name) get tokens without
    the claim — those identities are not tenant-bound by design
    (CMs reach `/client/{cid}/...` for clients they're assigned to,
    via the per-endpoint CMClientAssignment gate).
    """
    active_roles = [r.role_type.value for r in user.roles if r.status == StatusEnum.ACTIVE]
    payload: dict = {
        "sub": user.id,
        "roles": active_roles,
    }
    # JWT spec requires jti to be a string; jose refuses to decode
    # tokens carrying jti=null. Omit when the user has no session id.
    if user.current_session_id:
        payload["jti"] = user.current_session_id
    if client_id:
        payload["client_id"] = client_id
    if client_short_name:
        payload["client_short_name"] = client_short_name
    return create_access_token(payload)


async def start_new_session(db: AsyncSession, user: User) -> str:
    """Generate a new session_id, store on user, return it. Invalidates all previous tokens.

    Coaching Sandbox exception: coaching students need CONCURRENT
    sessions across portal (CA authoring) + PWA (farmer / dealer /
    facilitator / expert cross-role practice). Rotating the
    session_id on every login would kick out the other surface —
    the team's "PWA keeps signing out frequently" complaint. Skip
    the rotation for coaching students so both tokens stay valid
    in parallel. Safety floor: `guard_coaching_student_login`
    still refuses login outside ACTIVE sessions, so a closed
    coaching workspace can't onboard new tokens.
    """
    from app.modules.coaching.service import get_coaching_student_for_user
    if await get_coaching_student_for_user(db, user.id) is not None:
        # No-op for coaching students. Preserve any existing session_id
        # (returned unchanged) so their in-flight tokens stay valid;
        # new tokens minted after this call carry the same (or no) jti
        # and pass the single-device check trivially.
        return user.current_session_id or ""

    new_session_id = secrets.token_hex(16)
    user.current_session_id = new_session_id
    await db.commit()
    return new_session_id


# ── User lookups ───────────────────────────────────────────────────────────────

async def get_user_by_phone(db: AsyncSession, phone: str) -> Optional[User]:
    """Find user by phone. Excludes accounts whose 30-day grace period has fully expired
    (so a re-used phone after full deletion looks 'not found' and creates a fresh user)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    result = await db.execute(
        select(User).options(selectinload(User.roles)).where(
            User.phone == phone,
            or_(User.deleted_at.is_(None), User.deleted_at > cutoff),
        )
    )
    return result.scalar_one_or_none()


async def get_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
    result = await db.execute(
        select(User).options(selectinload(User.roles)).where(User.email == email)
    )
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: str) -> Optional[User]:
    result = await db.execute(
        select(User).options(selectinload(User.roles)).where(User.id == user_id)
    )
    return result.scalar_one_or_none()


# ── Phone OTP ──────────────────────────────────────────────────────────────────

def generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))


async def create_phone_otp(db: AsyncSession, phone: str) -> str:
    await db.execute(delete(PhoneOTP).where(PhoneOTP.phone == phone))
    otp_code = generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=OTP_EXPIRE_SECONDS)
    db.add(PhoneOTP(phone=phone, otp_code=otp_code, expires_at=expires_at))
    await db.commit()
    return otp_code


async def verify_phone_otp(db: AsyncSession, phone: str, otp_code: str) -> bool:
    result = await db.execute(
        select(PhoneOTP).where(
            PhoneOTP.phone == phone,
            PhoneOTP.otp_code == otp_code,
            PhoneOTP.used == False,
        )
    )
    otp = result.scalar_one_or_none()
    if not otp:
        return False
    if otp.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return False
    otp.used = True
    await db.commit()
    return True


async def get_or_create_farmer(db: AsyncSession, phone: str) -> User:
    """Get existing user by phone, or create a new Farmer.

    Also stamps `self_registered_at` — either on creation, or on
    the first OTP login for a pre-existing user that was created
    by another flow (typically the dealer's Farmer Ledger manual
    entry). This is the "user proved ownership of this phone"
    signal used to gate whether a dealer may edit their profile.
    """
    from datetime import datetime, timezone
    user = await get_user_by_phone(db, phone)
    now = datetime.now(timezone.utc)
    if not user:
        user = User(phone=phone, self_registered_at=now)
        db.add(user)
        await db.flush()
        db.add(UserRole(user_id=user.id, role_type=RoleType.FARMER, status=StatusEnum.ACTIVE))
        await db.commit()
        user = await get_user_by_phone(db, phone)
    elif user.self_registered_at is None:
        user.self_registered_at = now
        await db.commit()
    return user
