"""Batch K (2026-05-18) — view-guard on advisory pipes + multi-role
support + CA-exclusivity.

Per user 2026-05-18: "non-SE (except for the CA) shouldn't have any
access, not even view. CCA, PG, SP, and QA shouldn't even show up on
their sidebars."

Backend backstop: `_assert_can_view_client_advisory` refuses non-SE,
non-CA roles with 403 advisory_view_forbidden. CM-EDIT assignees
retain view (they administer the client).

CA-exclusivity rule: a user cannot hold CA AND any other role at the
same client. Enforced at user-add time with 409 ca_role_exclusive.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.router import (
    _assert_can_view_client_advisory, list_packages,
)
from app.modules.auth.router import get_me
from app.modules.clients.models import (
    CMClientAssignment, CMRights, ClientUser, ClientUserRole,
)
from app.modules.clients.router import add_portal_user
from app.modules.clients.schemas import PortalUserCreate
from app.modules.platform.models import StatusEnum
from tests.conftest import requires_docker
from tests.factories import make_client, make_client_user, make_user


# ── _assert_can_view_client_advisory ─────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_view_guard_permits_subject_expert(db):
    client = await make_client(db)
    user = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    await _assert_can_view_client_advisory(db, user.id, client.id)


@requires_docker
@pytest.mark.asyncio
async def test_view_guard_permits_ca(db):
    client = await make_client(db)
    user = await make_user(db, name="CA", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.CA,
    )
    await _assert_can_view_client_advisory(db, user.id, client.id)


@requires_docker
@pytest.mark.asyncio
async def test_view_guard_permits_cm_edit_assignee(db):
    client = await make_client(db)
    user = await make_user(db, name="CM", skip_auto_link=True)
    db.add(CMClientAssignment(
        cm_user_id=user.id, client_id=client.id,
        rights=CMRights.EDIT, status=StatusEnum.ACTIVE,
    ))
    await db.commit()
    await _assert_can_view_client_advisory(db, user.id, client.id)


@requires_docker
@pytest.mark.asyncio
async def test_view_guard_refuses_field_manager(db):
    client = await make_client(db)
    user = await make_user(db, name="FM", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.FIELD_MANAGER,
    )
    with pytest.raises(HTTPException) as exc:
        await _assert_can_view_client_advisory(db, user.id, client.id)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "advisory_view_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_view_guard_refuses_report_user(db):
    client = await make_client(db)
    user = await make_user(db, name="RU", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.REPORT_USER,
    )
    with pytest.raises(HTTPException) as exc:
        await _assert_can_view_client_advisory(db, user.id, client.id)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "advisory_view_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_view_guard_permits_multi_role_user_with_se(db):
    """A user with FIELD_MANAGER + SUBJECT_EXPERT roles passes the
    view guard — the SE role qualifies them, FM doesn't disqualify."""
    client = await make_client(db)
    user = await make_user(db, name="FM+SE", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.FIELD_MANAGER,
    )
    # Add second role directly — make_client_user DELETEs and re-INSERTs
    # so we use raw db.add for the additive case the model supports.
    db.add(ClientUser(
        client_id=client.id, user_id=user.id,
        role=ClientUserRole.SUBJECT_EXPERT, status=StatusEnum.ACTIVE,
    ))
    await db.commit()
    await _assert_can_view_client_advisory(db, user.id, client.id)


# ── End-to-end: GET endpoint refuses a non-SE/non-CA user ─────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_packages_refuses_report_user(db):
    """The list_packages GET (a representative advisory read) MUST
    refuse a REPORT_USER. End-to-end check that the view guard is
    actually wired into the endpoint, not just available as a helper."""
    client = await make_client(db)
    user = await make_user(db, name="RU", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.REPORT_USER,
    )
    with pytest.raises(HTTPException) as exc:
        await list_packages(
            client_id=client.id, db=db, current_user=user,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "advisory_view_forbidden"


# ── CA-exclusivity ───────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_add_ca_to_user_with_se_role_refused(db):
    """A user already holding SUBJECT_EXPERT cannot be promoted to CA
    at the same client — CA is mutually exclusive."""
    client = await make_client(db)
    # CA-creator (some user with auth) — needs to exist; not relevant.
    creator = await make_user(db, name="creator", skip_auto_link=True)
    target = await make_user(db, name="target", skip_auto_link=True)
    target.email = "target@kingcorp.example.com"
    await make_client_user(
        db, user=target, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await add_portal_user(
            client_id=client.id,
            request=PortalUserCreate(
                email=target.email,
                name="target",
                password="pw_unused_existing_user",
                role=ClientUserRole.CA,
            ),
            db=db, current_user=creator,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "ca_role_exclusive"


@requires_docker
@pytest.mark.asyncio
async def test_add_se_to_user_who_is_ca_refused(db):
    """Reverse of the above — can't add SE to an existing CA."""
    client = await make_client(db)
    creator = await make_user(db, name="creator", skip_auto_link=True)
    target = await make_user(db, name="ca-target", skip_auto_link=True)
    target.email = "ca-target@kingcorp.example.com"
    await make_client_user(
        db, user=target, client=client, role=ClientUserRole.CA,
    )
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await add_portal_user(
            client_id=client.id,
            request=PortalUserCreate(
                email=target.email,
                name="ca-target",
                password="pw_unused",
                role=ClientUserRole.SUBJECT_EXPERT,
            ),
            db=db, current_user=creator,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "ca_role_exclusive"


@requires_docker
@pytest.mark.asyncio
async def test_add_multiple_non_ca_roles_to_same_user_ok(db):
    """A user can hold FIELD_MANAGER + SUBJECT_EXPERT at the same
    client — only CA is exclusive."""
    client = await make_client(db)
    creator = await make_user(db, name="creator", skip_auto_link=True)
    target = await make_user(db, name="multi", skip_auto_link=True)
    target.email = "multi@kingcorp.example.com"
    await make_client_user(
        db, user=target, client=client, role=ClientUserRole.FIELD_MANAGER,
    )
    await db.commit()
    # Should not raise.
    await add_portal_user(
        client_id=client.id,
        request=PortalUserCreate(
            email=target.email,
            name="multi",
            password="pw_unused",
            role=ClientUserRole.SUBJECT_EXPERT,
        ),
        db=db, current_user=creator,
    )
    # Verify both roles persist.
    rows = (await db.execute(
        select(ClientUser).where(
            ClientUser.client_id == client.id,
            ClientUser.user_id == target.id,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalars().all()
    roles = {r.role for r in rows}
    assert ClientUserRole.FIELD_MANAGER in roles
    assert ClientUserRole.SUBJECT_EXPERT in roles
