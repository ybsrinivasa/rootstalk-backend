import secrets
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import EnabledLanguage, StatusEnum, User, UserRole, RoleType
from app.modules.platform.schemas import LanguageOut, LanguageToggle
from app.modules.clients.models import CMPrivilegeModel, CMPrivilege
from app.modules.clients.service import _send_email
from app.modules.auth.service import hash_password

router = APIRouter(tags=["Platform"])

NEYTIRI_ROLES = {RoleType.CONTENT_MANAGER, RoleType.RELATIONSHIP_MANAGER, RoleType.BUSINESS_MANAGER}


@router.get("/platform/languages", response_model=list[LanguageOut])
async def list_languages(db: AsyncSession = Depends(get_db)):
    """Return all enabled languages. Used by PWA to populate language selector."""
    result = await db.execute(select(EnabledLanguage).order_by(EnabledLanguage.language_name_en))
    return result.scalars().all()


@router.put("/platform/languages/{code}/status", response_model=LanguageOut)
async def toggle_language(
    code: str,
    request: LanguageToggle,
    db: AsyncSession = Depends(get_db),
    # SA only — auth enforced at main.py level via middleware (to be added)
):
    """SA: enable or disable a language. English cannot be disabled."""
    result = await db.execute(
        select(EnabledLanguage).where(EnabledLanguage.language_code == code)
    )
    lang = result.scalar_one_or_none()
    if not lang:
        raise HTTPException(status_code=404, detail="Language not found")
    if code == "en":
        raise HTTPException(status_code=400, detail="English cannot be disabled")

    lang.status = request.status
    await db.commit()
    await db.refresh(lang)
    return lang


# ── Neytiri Portal User Management (SA only) ─────────────────────────────────

@router.get("/admin/users")
async def list_neytiri_users(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA: list all CM/RM/BM users."""
    result = await db.execute(
        select(UserRole).where(UserRole.role_type.in_(list(NEYTIRI_ROLES)))
    )
    role_rows = result.scalars().all()
    user_ids = list({r.user_id for r in role_rows})
    if not user_ids:
        return []

    users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
    users_map = {u.id: u for u in users_result.scalars().all()}

    privs_result = await db.execute(
        select(CMPrivilegeModel).where(CMPrivilegeModel.cm_user_id.in_(user_ids))
    )
    privs_by_user: dict[str, list[str]] = {}
    for p in privs_result.scalars().all():
        privs_by_user.setdefault(p.cm_user_id, []).append(p.privilege.value)

    out = []
    seen = set()
    for role in role_rows:
        user = users_map.get(role.user_id)
        if not user or role.user_id in seen:
            continue
        seen.add(role.user_id)
        user_roles = [r.role_type.value for r in role_rows if r.user_id == role.user_id]
        out.append({
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "email": user.email,
            "roles": user_roles,
            "privileges": privs_by_user.get(user.id, []),
            "status": role.status.value,
        })
    return out


@router.post("/admin/users", status_code=201)
async def create_neytiri_user(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA: create a CM, RM, or BM user. Sends credentials by email."""
    if not data.get("email"):
        raise HTTPException(status_code=422, detail="email is required")
    roles = data.get("roles", [])
    if not roles:
        raise HTTPException(status_code=422, detail="at least one role required")

    valid_roles = [r for r in roles if r in [t.value for t in NEYTIRI_ROLES]]
    if not valid_roles:
        raise HTTPException(status_code=422, detail="roles must be CM, RM, or BM")

    existing = (await db.execute(select(User).where(User.email == data["email"]))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    temp_password = secrets.token_urlsafe(10)
    user = User(
        name=data.get("name"),
        phone=data.get("phone"),
        email=data["email"],
        password_hash=hash_password(temp_password),
        language_code="en",
    )
    db.add(user)
    await db.flush()

    for role_str in valid_roles:
        db.add(UserRole(user_id=user.id, role_type=RoleType(role_str)))

    await db.commit()

    _send_neytiri_welcome_email(data["email"], data.get("name", ""), temp_password, valid_roles)

    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "roles": valid_roles,
    }


@router.put("/admin/users/{user_id}")
async def update_neytiri_user(
    user_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = await _get_neytiri_user(db, user_id)
    for field in ["name", "phone", "email"]:
        if field in data:
            setattr(user, field, data[field])
    await db.commit()
    return {"id": user.id, "name": user.name, "email": user.email}


@router.put("/admin/users/{user_id}/status")
async def toggle_neytiri_user_status(
    user_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA: activate or deactivate a Neytiri portal user."""
    await _get_neytiri_user(db, user_id)
    new_status = StatusEnum.ACTIVE if data.get("active", True) else StatusEnum.INACTIVE
    result = await db.execute(
        select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_type.in_(list(NEYTIRI_ROLES)),
        )
    )
    roles = result.scalars().all()
    for role in roles:
        role.status = new_status
    await db.commit()
    return {"user_id": user_id, "status": new_status.value}


@router.put("/admin/users/{user_id}/privileges")
async def set_cm_privileges(
    user_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA: set or clear CM privileges (3 toggles). Only applies to Content Manager role."""
    await _get_neytiri_user(db, user_id)

    privileges = data.get("privileges", [])
    valid = {p.value for p in CMPrivilege}
    for p in privileges:
        if p not in valid:
            raise HTTPException(status_code=422, detail=f"Unknown privilege: {p}")

    await db.execute(delete(CMPrivilegeModel).where(CMPrivilegeModel.cm_user_id == user_id))
    for p in privileges:
        db.add(CMPrivilegeModel(cm_user_id=user_id, privilege=CMPrivilege(p)))
    await db.commit()
    return {"user_id": user_id, "privileges": privileges}


@router.put("/admin/users/{user_id}/password-override")
async def override_user_password(
    user_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA: override any user's password. Sends notification email."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not data.get("new_password"):
        raise HTTPException(status_code=422, detail="new_password required")
    user.password_hash = hash_password(data["new_password"])
    await db.commit()
    if user.email:
        _send_email(
            user.email,
            "Your RootsTalk password was changed",
            f"<p>Your password was reset by an administrator. New password: <strong>{data['new_password']}</strong></p>",
            f"Your RootsTalk password was reset by an administrator. New password: {data['new_password']}",
        )
    return {"detail": "Password updated"}


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_neytiri_user(db: AsyncSession, user_id: str) -> User:
    role = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_type.in_(list(NEYTIRI_ROLES)),
        )
    )).scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Neytiri portal user not found")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    return user


def _send_neytiri_welcome_email(email: str, name: str, temp_password: str, roles: list[str]):
    role_str = ", ".join(roles)
    portal_url = "https://coshdev.eywa.farm/admin"
    subject = "Your RootsTalk Admin Portal account"
    plain = f"""Hi {name or ''},

Your RootsTalk Admin Portal account has been created.

Portal: {portal_url}
Email: {email}
Password: {temp_password}
Role(s): {role_str}

Please change your password after first login.

RootsTalk — Neytiri Eywafarm Agritech"""
    html = f"""
<body style="font-family:sans-serif;padding:32px">
  <h2>Welcome to RootsTalk Admin Portal</h2>
  <p>Hi {name or ''},</p>
  <p>Your account has been created. Here are your login details:</p>
  <table style="background:#f8fafc;border-radius:8px;padding:16px;margin:16px 0">
    <tr><td><strong>Portal:</strong></td><td><a href="{portal_url}">{portal_url}</a></td></tr>
    <tr><td><strong>Email:</strong></td><td>{email}</td></tr>
    <tr><td><strong>Password:</strong></td><td>{temp_password}</td></tr>
    <tr><td><strong>Role(s):</strong></td><td>{role_str}</td></tr>
  </table>
  <p style="color:#666;font-size:12px">Please change your password after first login.</p>
</body>"""
    _send_email(email, subject, html, plain)
