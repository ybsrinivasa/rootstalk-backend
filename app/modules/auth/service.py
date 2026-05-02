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

OTP_EXPIRE_MINUTES = 10


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


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return None


def _build_token(user: User) -> str:
    active_roles = [r.role_type.value for r in user.roles if r.status == StatusEnum.ACTIVE]
    return create_access_token({"sub": user.id, "roles": active_roles, "jti": user.current_session_id})


async def start_new_session(db: AsyncSession, user: User) -> str:
    """Generate a new session_id, store on user, return it. Invalidates all previous tokens."""
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
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES)
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
    """Get existing user by phone, or create a new Farmer."""
    user = await get_user_by_phone(db, phone)
    if not user:
        user = User(phone=phone)
        db.add(user)
        await db.flush()
        db.add(UserRole(user_id=user.id, role_type=RoleType.FARMER, status=StatusEnum.ACTIVE))
        await db.commit()
        user = await get_user_by_phone(db, phone)
    return user
