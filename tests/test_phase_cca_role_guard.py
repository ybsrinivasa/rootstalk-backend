"""Batch 39S (2026-05-17) — CA-side CCA write guard.

`_assert_can_edit_client_advisory` replaces the V1 no-op
`_require_client_role` stub on every CCA write endpoint
(20 routes: package CRUD + publish + locations + authors + variables +
timeline CRUD + practice CRUD + relation/CQ create + element CRUD).

Eligible (V1 boundary):
  • Any ACTIVE ClientUser of THIS client (regardless of role).
  • ACTIVE CMClientAssignment with EDIT rights to THIS client.

Rejected with 403 `cca_edit_forbidden`:
  • Authenticated user with no ClientUser AND no CM assignment.
  • Authenticated user with a ClientUser at a DIFFERENT client only.
  • CMClientAssignment with rights != EDIT.
  • INACTIVE ClientUser or INACTIVE CMClientAssignment.

These tests exercise the helper directly (no HTTP) for tight signal.
A representative write endpoint (`create_package`) is also exercised
end-to-end to prove the guard fires before any business logic.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.advisory.router import (
    _assert_can_edit_client_advisory,
    create_package,
)
from app.modules.advisory.schemas import PackageCreate
from app.modules.advisory.models import PackageType
from app.modules.clients.models import (
    CMClientAssignment, CMRights, ClientUser, ClientUserRole,
)
from app.modules.platform.models import StatusEnum
from fastapi import HTTPException
from tests.conftest import requires_docker
from tests.factories import make_client, make_client_user, make_user


# ── Eligibility ──────────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_se_clientuser_passes(db):
    client = await make_client(db)
    user = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    # No raise.
    await _assert_can_edit_client_advisory(db, user.id, client.id)


@requires_docker
@pytest.mark.asyncio
async def test_ca_clientuser_passes(db):
    """V1 boundary: any role on this client passes. SE-only tightening
    is a future refinement."""
    client = await make_client(db)
    user = await make_user(db, name="CA", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.CA,
    )
    await _assert_can_edit_client_advisory(db, user.id, client.id)


@requires_docker
@pytest.mark.asyncio
async def test_field_manager_clientuser_passes(db):
    client = await make_client(db)
    user = await make_user(db, name="FM", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.FIELD_MANAGER,
    )
    await _assert_can_edit_client_advisory(db, user.id, client.id)


@requires_docker
@pytest.mark.asyncio
async def test_cm_with_edit_assignment_passes(db):
    client = await make_client(db)
    user = await make_user(db, name="Ram-CM", skip_auto_link=True)
    db.add(CMClientAssignment(
        cm_user_id=user.id, client_id=client.id,
        rights=CMRights.EDIT, status=StatusEnum.ACTIVE,
    ))
    await db.commit()
    await _assert_can_edit_client_advisory(db, user.id, client.id)


# ── Rejection ────────────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_user_with_no_clientuser_and_no_assignment_rejected(db):
    client = await make_client(db)
    user = await make_user(db, name="Stranger", skip_auto_link=True)
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await _assert_can_edit_client_advisory(db, user.id, client.id)
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "cca_edit_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_clientuser_at_different_client_rejected(db):
    """Member of client A trying to write client B's CCA → 403."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    user = await make_user(db, name="A-only", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client_a, role=ClientUserRole.SUBJECT_EXPERT,
    )
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await _assert_can_edit_client_advisory(db, user.id, client_b.id)
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "cca_edit_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_inactive_clientuser_rejected(db):
    client = await make_client(db)
    user = await make_user(db, name="Deactivated", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.SUBJECT_EXPERT,
        status=StatusEnum.INACTIVE,
    )
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await _assert_can_edit_client_advisory(db, user.id, client.id)
    assert ei.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_cm_with_view_rights_only_rejected(db):
    client = await make_client(db)
    user = await make_user(db, name="CM-View", skip_auto_link=True)
    db.add(CMClientAssignment(
        cm_user_id=user.id, client_id=client.id,
        rights=CMRights.VIEW, status=StatusEnum.ACTIVE,
    ))
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await _assert_can_edit_client_advisory(db, user.id, client.id)
    assert ei.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_inactive_cm_assignment_rejected(db):
    client = await make_client(db)
    user = await make_user(db, name="CM-Off", skip_auto_link=True)
    db.add(CMClientAssignment(
        cm_user_id=user.id, client_id=client.id,
        rights=CMRights.EDIT, status=StatusEnum.INACTIVE,
    ))
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await _assert_can_edit_client_advisory(db, user.id, client.id)
    assert ei.value.status_code == 403


# ── End-to-end: guard fires before business logic on a real route ────────


@requires_docker
@pytest.mark.asyncio
async def test_create_package_rejects_stranger_before_crop_check(db):
    """Cross-validation: the guard runs BEFORE `assert_crop_on_belt`.
    Pre-guard a stranger could probe the crop-belt validator with
    arbitrary crop_cosh_ids by getting 422 errors. Now they get 403
    upfront."""
    client = await make_client(db)
    stranger = await make_user(db, name="Stranger", skip_auto_link=True)
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await create_package(
            client_id=client.id,
            request=PackageCreate(
                crop_cosh_id="cosh-fake-crop",
                name="Anything", package_type=PackageType.ANNUAL,
                duration_days=180,
                start_date_label_cosh_id="cosh-fake-label",
            ),
            db=db, current_user=stranger,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "cca_edit_forbidden"
