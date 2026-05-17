"""Batch 39W (2026-05-17) — SA-side Global write guard.

`_assert_sa_or_cm` closes the cross-portal hole flagged by user
2026-05-17: pre-batch, every /advisory/global/* and /admin/* write
endpoint was reachable by any authenticated user — including
CA-Portal SUBJECT_EXPERTs with only a ClientUser row, since the
handlers were guarded by `Depends(get_current_user)` only.

Eligible (V1 boundary):
  • SA identity — `current_user.email == settings.sa_email`.
  • ACTIVE UserRole(role_type=CONTENT_MANAGER).

Rejected with 403 `global_edit_forbidden`:
  • RELATIONSHIP_MANAGER / BUSINESS_MANAGER roles (no SA-Portal
    content-authoring privilege).
  • FARMER / DEALER / FACILITATOR / FARM_PUNDIT PWA roles.
  • CA-Portal-only ClientUsers with no SA-Portal role.
  • Inactive CONTENT_MANAGER UserRole.

These tests exercise the helper directly (no HTTP) for tight
signal. A representative Global write (`create_global_package`)
is also exercised end-to-end to prove the guard fires before any
business logic.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.config import settings
from app.modules.advisory.models import PackageType
from app.modules.advisory.router import (
    _assert_sa_or_cm, create_global_package,
)
from app.modules.advisory.schemas import PackageCreate
from app.modules.platform.models import RoleType, StatusEnum, UserRole
from tests.conftest import requires_docker
from tests.factories import make_user


# ── Eligibility ──────────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_sa_email_passes(db, monkeypatch):
    monkeypatch.setattr(settings, "sa_email", "sa@example.com")
    user = await make_user(db, name="SA", skip_auto_link=True, skip_auto_cm=True)
    user.email = "sa@example.com"
    await db.commit()
    # No raise.
    await _assert_sa_or_cm(db, user)


@requires_docker
@pytest.mark.asyncio
async def test_active_cm_userrole_passes(db):
    user = await make_user(db, name="CM", skip_auto_link=True, skip_auto_cm=True)
    db.add(UserRole(
        user_id=user.id, role_type=RoleType.CONTENT_MANAGER,
        status=StatusEnum.ACTIVE,
    ))
    await db.commit()
    await _assert_sa_or_cm(db, user)


# ── Rejection ────────────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_rm_userrole_rejected(db):
    user = await make_user(db, name="RM", skip_auto_link=True, skip_auto_cm=True)
    db.add(UserRole(
        user_id=user.id, role_type=RoleType.RELATIONSHIP_MANAGER,
        status=StatusEnum.ACTIVE,
    ))
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await _assert_sa_or_cm(db, user)
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "global_edit_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_bm_userrole_rejected(db):
    user = await make_user(db, name="BM", skip_auto_link=True, skip_auto_cm=True)
    db.add(UserRole(
        user_id=user.id, role_type=RoleType.BUSINESS_MANAGER,
        status=StatusEnum.ACTIVE,
    ))
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await _assert_sa_or_cm(db, user)
    assert ei.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_farmer_userrole_rejected(db):
    user = await make_user(db, name="Farmer", skip_auto_link=True, skip_auto_cm=True)
    db.add(UserRole(
        user_id=user.id, role_type=RoleType.FARMER,
        status=StatusEnum.ACTIVE,
    ))
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await _assert_sa_or_cm(db, user)
    assert ei.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_inactive_cm_rejected(db):
    user = await make_user(db, name="CM-Off", skip_auto_link=True, skip_auto_cm=True)
    db.add(UserRole(
        user_id=user.id, role_type=RoleType.CONTENT_MANAGER,
        status=StatusEnum.INACTIVE,
    ))
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await _assert_sa_or_cm(db, user)
    assert ei.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_user_with_no_role_rejected(db):
    """Closes the headline hole: a bare authenticated User (no SA
    email, no UserRole, no ClientUser) cannot write Global content."""
    user = await make_user(db, name="Stranger", skip_auto_link=True, skip_auto_cm=True)
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await _assert_sa_or_cm(db, user)
    assert ei.value.status_code == 403


# ── End-to-end: guard fires before business logic on a real route ────────


@requires_docker
@pytest.mark.asyncio
async def test_create_global_package_rejects_role_less_user(db):
    """Cross-validation: the guard runs BEFORE any business logic on
    create_global_package. Pre-batch this would have created a row;
    post-batch it raises 403 with code global_edit_forbidden."""
    stranger = await make_user(db, name="Stranger", skip_auto_link=True, skip_auto_cm=True)
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await create_global_package(
            request=PackageCreate(
                crop_cosh_id="cosh-fake-crop",
                name="Anything", package_type=PackageType.ANNUAL,
                duration_days=180,
                start_date_label_cosh_id="cosh-fake-label",
            ),
            db=db, current_user=stranger,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "global_edit_forbidden"
