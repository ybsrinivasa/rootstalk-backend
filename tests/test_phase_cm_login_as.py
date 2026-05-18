"""Batch Q (2026-05-18) — CM SSO into CA Portal.

Per user 2026-05-18: "It is now taking the User to the client login
page, but the CM cannot login with that URL. … the CM should be
directly logged into that client. He shouldn't be logging in once
again." And: "The CM will have all the privileges inside the
Client — that of the CA, Subject Experts, and all other roles."

New POST /admin/cm/clients/{cid}/login-as issues a fresh JWT
bound to the target client_id; the CA Portal /cm-login route
consumes it and lands the CM inside the client with full
CA-equivalent access.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.config import settings
from app.modules.auth.service import decode_token, get_user_by_id
from app.modules.clients.models import (
    CMClientAssignment, CMRights, Client, ClientStatus,
)
from app.modules.clients.router import cm_login_as
from app.modules.platform.models import StatusEnum
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


async def _activate(db, client: Client) -> None:
    client.status = ClientStatus.ACTIVE
    await db.flush()


async def _make_cm(db, *, name="CM", email_suffix="cm@platform.example.com"):
    cm = await make_user(db, name=name, skip_auto_link=True)
    cm.email = email_suffix
    await db.flush()
    return cm


async def _reload(db, user):
    """Pre-load user.roles via selectinload — _build_token lazy-loads
    that relationship and fails outside a greenlet context otherwise.
    Mirror of the helper in test_phase_tenant_isolation.py."""
    fresh = await get_user_by_id(db, user.id)
    assert fresh is not None
    return fresh


async def _assign_cm(db, *, cm, client, rights=CMRights.EDIT, status=StatusEnum.ACTIVE):
    db.add(CMClientAssignment(
        cm_user_id=cm.id, client_id=client.id,
        rights=rights, status=status,
    ))
    await db.flush()


@requires_docker
@pytest.mark.asyncio
async def test_cm_login_as_issues_token_with_client_claim(db):
    client = await make_client(db)
    await _activate(db, client)
    client.short_name = "kingcorp"
    cm = await _make_cm(db, name="CM-A")
    await _assign_cm(db, cm=cm, client=client)
    await db.commit()
    cm = await _reload(db, cm)

    out = await cm_login_as(
        client_id=client.id, db=db, current_user=cm,
    )
    payload = decode_token(out["access_token"])
    assert payload is not None
    assert payload["client_id"] == client.id
    assert payload["client_short_name"] == "kingcorp"
    assert out["client_short_name"] == "kingcorp"
    assert out["ca_portal_url"]  # env-driven base


@requires_docker
@pytest.mark.asyncio
async def test_cm_login_as_refuses_without_assignment(db):
    client = await make_client(db)
    await _activate(db, client)
    cm = await _make_cm(db, name="Rogue-CM")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await cm_login_as(
            client_id=client.id, db=db, current_user=cm,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "cm_login_as_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_cm_login_as_refuses_view_only_assignment(db):
    """VIEW-rights CMs can browse the client through SA Portal but
    not SSO into the CA Portal as the client. Read-only support
    role; impersonation requires EDIT."""
    client = await make_client(db)
    await _activate(db, client)
    cm = await _make_cm(db, name="ViewCM")
    await _assign_cm(db, cm=cm, client=client, rights=CMRights.VIEW)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await cm_login_as(
            client_id=client.id, db=db, current_user=cm,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "cm_login_as_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_cm_login_as_refuses_inactive_assignment(db):
    client = await make_client(db)
    await _activate(db, client)
    cm = await _make_cm(db, name="ExCM")
    await _assign_cm(db, cm=cm, client=client, status=StatusEnum.INACTIVE)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await cm_login_as(
            client_id=client.id, db=db, current_user=cm,
        )
    assert exc.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_cm_login_as_permits_sa_for_any_client(db):
    """SA can login-as any client — support walkthrough use case."""
    client = await make_client(db)
    await _activate(db, client)
    sa = await _make_cm(db, name="SA", email_suffix=settings.sa_email)
    # No CM assignment needed; SA bypass.
    await db.commit()
    sa = await _reload(db, sa)
    out = await cm_login_as(
        client_id=client.id, db=db, current_user=sa,
    )
    payload = decode_token(out["access_token"])
    assert payload["client_id"] == client.id


@requires_docker
@pytest.mark.asyncio
async def test_cm_login_as_404_for_missing_client(db):
    cm = await _make_cm(db, name="CM")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await cm_login_as(
            client_id="nonexistent-id", db=db, current_user=cm,
        )
    assert exc.value.status_code == 404
