"""Tenant isolation — JWT client_id claim + path scope guard
(Batch I, 2026-05-18).

Audit-grade backstop: a portal-issued JWT carries a `client_id`
claim set at login from the user's `client_short_name`. Every
authenticated request to `/client/{client_id}/...` is validated
in `get_current_user`:

  - token has no client_id   → permitted (SA / CM / PWA tokens are
                                not tenant-bound; per-endpoint guards
                                like _assert_cm_can_edit_client still
                                apply).
  - token has client_id == path → permitted.
  - token has client_id != path → refused 403 cross_client_forbidden.

This file is the canonical proof. Clients reviewing our isolation
guarantee should be pointed at these tests.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.dependencies import get_current_user
from app.modules.auth.router import admin_login
from app.modules.auth.schemas import AdminLoginRequest
from app.modules.auth.service import _build_token, decode_token, get_user_by_id
from app.modules.clients.models import ClientStatus, ClientUserRole
from tests.conftest import requires_docker
from tests.factories import make_client, make_client_user, make_user


async def _activate(db, client, short_name: str):
    """make_client() defaults to PENDING_REVIEW + auto-generated
    short_name. The login flow needs ACTIVE + a deterministic
    short_name to look the client up."""
    client.short_name = short_name
    client.status = ClientStatus.ACTIVE
    await db.flush()
    return client


async def _reload(db, user):
    """make_user returns a User without `.roles` eager-loaded. _build_token
    iterates user.roles — that triggers async lazy-load which doesn't
    work outside a greenlet context. Reload via the same path admin_login
    uses (selectinload-backed get_user_by_email)."""
    fresh = await get_user_by_id(db, user.id)
    assert fresh is not None
    return fresh


def _fake_request(path_client_id: str | None) -> SimpleNamespace:
    """Build the minimum Request shape `get_current_user` reads from:
    path_params dict + a mutable state object."""
    path_params = {}
    if path_client_id is not None:
        path_params["client_id"] = path_client_id
    return SimpleNamespace(path_params=path_params, state=SimpleNamespace())


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


# ── Token-claim wiring ──────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_admin_login_with_client_short_name_embeds_client_id_in_token(db):
    """A portal login (with client_short_name) must produce a JWT
    whose payload includes client_id + client_short_name — that
    claim is what the per-request gate keys off."""
    user = await make_user(db, name="SE")
    user.email = "se@kingcorp.example.com"
    from app.modules.auth.service import hash_password
    user.password_hash = hash_password("pw")
    client = await make_client(db)
    await _activate(db, client, "kingcorp")
    await make_client_user(db, user=user, client=client, role=ClientUserRole.SUBJECT_EXPERT)
    await db.commit()

    out = await admin_login(
        request=AdminLoginRequest(
            email="se@kingcorp.example.com", password="pw",
            client_short_name="kingcorp",
        ),
        db=db,
    )
    payload = decode_token(out.access_token)
    assert payload is not None
    assert payload["client_id"] == client.id
    assert payload["client_short_name"] == "kingcorp"


@requires_docker
@pytest.mark.asyncio
async def test_admin_login_without_client_short_name_omits_client_id(db):
    """SA / CM / PWA logins don't pass client_short_name. The token
    must NOT have a client_id claim — those identities reach
    `/client/{cid}/...` via per-endpoint role guards, not via the
    blanket tenant claim."""
    user = await make_user(db, name="CM")
    user.email = "cm@platform.example.com"
    from app.modules.auth.service import hash_password
    user.password_hash = hash_password("pw")
    await db.commit()

    out = await admin_login(
        request=AdminLoginRequest(email="cm@platform.example.com", password="pw"),
        db=db,
    )
    payload = decode_token(out.access_token)
    assert payload is not None
    assert "client_id" not in payload
    assert "client_short_name" not in payload


# ── get_current_user path-scope gate ────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_path_client_id_matching_token_is_permitted(db):
    user = await make_user(db, name="SE")
    client = await make_client(db)
    await _activate(db, client, "kingcorp")
    await make_client_user(db, user=user, client=client, role=ClientUserRole.SUBJECT_EXPERT)
    await db.commit()
    user = await _reload(db, user)
    token = _build_token(user, client_id=client.id, client_short_name="kingcorp")

    request = _fake_request(path_client_id=client.id)
    out = await get_current_user(
        request=request, credentials=_bearer(token), db=db,
    )
    assert out.id == user.id
    # Side effect — stashed on request.state for /auth/me consumption.
    assert request.state.token_client_id == client.id
    assert request.state.token_client_short_name == "kingcorp"


@requires_docker
@pytest.mark.asyncio
async def test_path_client_id_mismatching_token_is_refused_403(db):
    """The headline guarantee: a session bound to KingCorp can NOT
    reach AcmeCorp's tenant data, regardless of any per-endpoint gate
    sloppiness elsewhere."""
    se = await make_user(db, name="SE")
    kingcorp = await make_client(db)
    await _activate(db, kingcorp, "kingcorp")
    acme = await make_client(db)
    await _activate(db, acme, "acme")
    await make_client_user(db, user=se, client=kingcorp, role=ClientUserRole.SUBJECT_EXPERT)
    # Note: NOT a member of acme. Even if they were, the gate fires
    # — token is bound to kingcorp, period.
    await db.commit()
    se = await _reload(db, se)
    token = _build_token(se, client_id=kingcorp.id, client_short_name="kingcorp")

    request = _fake_request(path_client_id=acme.id)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(
            request=request, credentials=_bearer(token), db=db,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "cross_client_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_dual_membership_still_blocked_by_token_binding(db):
    """An SE who is an ACTIVE member of two clients still can't
    cross-access at runtime — the token only binds to one at a time.
    Switching tenants requires logout + re-login to the other portal."""
    se = await make_user(db, name="SE")
    kingcorp = await make_client(db)
    await _activate(db, kingcorp, "kingcorp")
    acme = await make_client(db)
    await _activate(db, acme, "acme")
    await make_client_user(db, user=se, client=kingcorp, role=ClientUserRole.SUBJECT_EXPERT)
    await make_client_user(db, user=se, client=acme, role=ClientUserRole.SUBJECT_EXPERT)
    await db.commit()

    # Token bound to KingCorp.
    se = await _reload(db, se)
    token = _build_token(se, client_id=kingcorp.id, client_short_name="kingcorp")
    request = _fake_request(path_client_id=acme.id)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(
            request=request, credentials=_bearer(token), db=db,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "cross_client_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_token_without_client_claim_is_permitted_on_any_client_path(db):
    """SA / CM tokens have no client_id claim — the path-scope gate
    is silent for them. Per-endpoint role guards still apply
    downstream (e.g. _assert_cm_can_edit_client on import flows)."""
    cm = await make_user(db, name="CM")
    kingcorp = await make_client(db, short_name="kingcorp")
    await db.commit()
    cm = await _reload(db, cm)
    token = _build_token(cm)  # no client_id

    request = _fake_request(path_client_id=kingcorp.id)
    out = await get_current_user(
        request=request, credentials=_bearer(token), db=db,
    )
    assert out.id == cm.id
    assert request.state.token_client_id is None


@requires_docker
@pytest.mark.asyncio
async def test_token_with_client_claim_on_path_without_client_param_is_permitted(db):
    """A portal-bound token reaching a non-client-scoped endpoint
    (e.g. /auth/me) is not refused — the gate only fires when BOTH
    sides have a client_id."""
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await _activate(db, client, "kingcorp")
    await make_client_user(db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT)
    await db.commit()
    se = await _reload(db, se)
    token = _build_token(se, client_id=client.id, client_short_name="kingcorp")

    request = _fake_request(path_client_id=None)
    out = await get_current_user(
        request=request, credentials=_bearer(token), db=db,
    )
    assert out.id == se.id
