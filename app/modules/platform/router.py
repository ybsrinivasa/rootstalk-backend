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


@router.get("/platform/lookup-user-by-phone")
async def lookup_user_by_phone(
    phone: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Verify a phone number belongs to a real RootsTalk user.

    Used wherever a farmer (or dealer, or facilitator) types
    another person's phone number — alerts recipient, payment
    delegation, custom-order routing, helper assignment — and
    needs to see the matching name + photo + roles before
    committing the action.

    Input is normalised to the canonical `+91XXXXXXXXXX` shape
    (last 10 digits, +91 prefix). 200 always — `found: false`
    when no match. Caller decides whether to allow the action
    on an unregistered number.
    """
    from app.modules.auth.service import get_user_by_phone
    digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
    if len(digits) < 10:
        return {"found": False, "phone": phone}
    normalised = '+91' + digits[-10:]

    user = await get_user_by_phone(db, normalised)
    if not user:
        return {"found": False, "phone": normalised}

    role_values = [
        (r.role_type.value if hasattr(r.role_type, 'value') else str(r.role_type))
        for r in user.roles
    ]
    return {
        "found": True,
        "user_id": user.id,
        "phone": user.phone,
        "name": user.name,
        "photo_url": user.photo_url,
        "roles": role_values,
    }


@router.post("/platform/fcm-token")
async def register_fcm_token(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """PWA registers (or updates) the device's FCM push-notification
    token for the authenticated user.

    Single-device for V1 — the token replaces any previously
    registered one. Multi-device support deferred to V2.

    Body: `{"token": "<fcm-registration-token-from-firebase-messaging>"}`
    Pass `{"token": null}` to clear (e.g. on logout).
    """
    raw = data.get("token")
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(status_code=422, detail="token must be a string or null")
    token = (raw or "").strip() or None
    current_user.fcm_token = token
    await db.commit()
    return {"detail": "FCM token registered" if token else "FCM token cleared"}


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
    """SA: list all CM/RM/BM users.

    2026-06-30 — Suppress the SA from this list. The SA holds implicit
    view rights across every client (whether assigned or not), so they
    should never appear as a selectable CM in the SA portal's "Assign
    CM" dropdown. The data side carries a historical SA-also-has-CM-
    role row that this filter neutralises in code; the migration
    script removes the row itself.
    """
    from app.config import settings as _settings

    result = await db.execute(
        select(UserRole).where(UserRole.role_type.in_(list(NEYTIRI_ROLES)))
    )
    role_rows = result.scalars().all()
    user_ids = list({r.user_id for r in role_rows})
    if not user_ids:
        return []

    users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
    users_map = {u.id: u for u in users_result.scalars().all()}

    # Strip the SA identity from both the index and the role list.
    sa_email = (_settings.sa_email or "").lower()
    if sa_email:
        for uid, u in list(users_map.items()):
            if (u.email or "").lower() == sa_email:
                users_map.pop(uid, None)
        role_rows = [r for r in role_rows if r.user_id in users_map]

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


@router.put("/admin/users/{user_id}/roles")
async def reconcile_neytiri_user_roles(
    user_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA: reconcile a Neytiri portal user's roles to exactly the
    given set. Body ``{"roles": ["CONTENT_MANAGER", ...]}``.

    - Target set must have at least one role, all in NEYTIRI_ROLES.
    - Missing roles are added (status=ACTIVE).
    - Roles present but INACTIVE and in target flip back to ACTIVE.
    - Roles ACTIVE and NOT in target flip to INACTIVE.
    - Deactivating the user entirely goes through /status, not here.
    """
    await _get_neytiri_user(db, user_id)

    target_raw = data.get("roles")
    if not isinstance(target_raw, list) or len(target_raw) == 0:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "roles_required",
                "message": (
                    "Pick at least one role. To remove the user "
                    "entirely, use the Deactivate button."
                ),
            },
        )
    try:
        target_set = {RoleType(r) for r in target_raw}
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown role in payload")

    if not target_set.issubset(NEYTIRI_ROLES):
        raise HTTPException(
            status_code=422,
            detail="Only Neytiri roles (CM / RM / BM) can be assigned here",
        )

    existing_rows = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_type.in_(list(NEYTIRI_ROLES)),
        )
    )).scalars().all()
    existing_by_role = {r.role_type: r for r in existing_rows}

    for role in target_set:
        if role in existing_by_role:
            r = existing_by_role[role]
            if r.status != StatusEnum.ACTIVE:
                r.status = StatusEnum.ACTIVE
        else:
            db.add(UserRole(
                user_id=user_id, role_type=role, status=StatusEnum.ACTIVE,
            ))

    for r in existing_rows:
        if r.role_type in target_set:
            continue
        if r.status == StatusEnum.ACTIVE:
            r.status = StatusEnum.INACTIVE

    await db.commit()
    return {"detail": "roles reconciled",
            "roles": sorted(r.value for r in target_set)}


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
    """SA: set or clear CM privileges (3 toggles). Only applies to Content Manager role.

    Batch U (2026-05-18) — privileges are single-holder. Granting a
    privilege to user X atomically revokes the same privilege from
    any other CM who held it. The DB also enforces this with a
    partial unique index, so concurrent grants race-safe.
    """
    await _get_neytiri_user(db, user_id)

    privileges = data.get("privileges", [])
    valid = {p.value for p in CMPrivilege}
    for p in privileges:
        if p not in valid:
            raise HTTPException(status_code=422, detail=f"Unknown privilege: {p}")

    # Demote anyone else holding the privileges we're about to grant.
    if privileges:
        await db.execute(
            delete(CMPrivilegeModel).where(
                CMPrivilegeModel.privilege.in_([CMPrivilege(p) for p in privileges]),
                CMPrivilegeModel.cm_user_id != user_id,
            )
        )
    # Clear this user's existing privileges + insert the new set.
    await db.execute(delete(CMPrivilegeModel).where(CMPrivilegeModel.cm_user_id == user_id))
    for p in privileges:
        db.add(CMPrivilegeModel(cm_user_id=user_id, privilege=CMPrivilege(p)))
    await db.commit()
    return {"user_id": user_id, "privileges": privileges}


@router.get("/admin/cm-privileges")
async def list_cm_privilege_owners(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA: returns the current owner (or null) for each CM privilege.
    One row per privilege — exactly three rows.
    Batch U (2026-05-18)."""
    rows = (await db.execute(
        select(CMPrivilegeModel, User)
        .join(User, User.id == CMPrivilegeModel.cm_user_id, isouter=False)
    )).all()
    holders: dict[str, dict] = {
        p.value: {"privilege": p.value, "cm_user_id": None, "name": None, "email": None}
        for p in CMPrivilege
    }
    for priv, user in rows:
        holders[priv.privilege.value] = {
            "privilege": priv.privilege.value,
            "cm_user_id": user.id,
            "name": user.name,
            "email": user.email,
        }
    # Stable order matching the UI's display order.
    order = [
        CMPrivilege.CROP_HEALTH_CROPS.value,
        CMPrivilege.BRAND_HANDLING.value,
        CMPrivilege.VOLUME_CALCULATIONS.value,
    ]
    return [holders[p] for p in order]


@router.put("/admin/cm-privileges/{privilege}")
async def set_cm_privilege_holder(
    privilege: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA: assigns a single CM as the holder of a privilege (or
    unassigns by passing `cm_user_id: null`). Atomically demotes
    any other CM who currently holds the privilege.
    Batch U (2026-05-18) — supports the SA Portal owner dropdowns.
    """
    valid = {p.value for p in CMPrivilege}
    if privilege not in valid:
        raise HTTPException(status_code=422, detail=f"Unknown privilege: {privilege}")
    priv = CMPrivilege(privilege)

    cm_user_id = data.get("cm_user_id")
    # Demote any current holder.
    await db.execute(
        delete(CMPrivilegeModel).where(CMPrivilegeModel.privilege == priv)
    )

    if cm_user_id is not None:
        # Confirm target is a Neytiri portal user (404s if not).
        await _get_neytiri_user(db, cm_user_id)
        db.add(CMPrivilegeModel(cm_user_id=cm_user_id, privilege=priv))

    await db.commit()
    return {"privilege": privilege, "cm_user_id": cm_user_id}


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


def _sa_portal_url() -> str:
    """Resolve the SA portal URL used in Neytiri welcome emails.

    Resolution order:
      1. `SA_PORTAL_URL` env var — required in non-dev when the SA
         portal is on a separate subdomain (testing topology).
      2. `FRONTEND_BASE_URL` — fine for production where SA and CA
         share the same host (`rootstalk.eywa.farm`).
      3. Dev fallback `http://localhost:3002` (the SA-portal dev
         port — see project_rootstalk_ports.md).
    """
    from app.config import settings
    if settings.sa_portal_url:
        return settings.sa_portal_url.rstrip("/")
    if settings.frontend_base_url:
        return settings.frontend_base_url.rstrip("/")
    return "http://localhost:3002"


def _send_neytiri_welcome_email(email: str, name: str, temp_password: str, roles: list[str]):
    role_str = ", ".join(roles)
    portal_url = _sa_portal_url()
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
