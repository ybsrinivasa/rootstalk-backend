"""Batch O (2026-05-18) — Seed Varieties org-type + role guard.

Per user 2026-05-18:
  (1) Seed Varieties available only to clients onboarded as Seed
      Companies (org_type_cosh_ids includes 'org_type_seed_companies').
  (2) Within such a client, only CA + SEED_DATA_MANAGER can manage
      the catalogue.

Tests exercise `_assert_can_manage_seed_varieties` directly (the
helper is the audit-grade gate). Each of the six client-scoped seed
endpoints (list / create / update / delete / assign-pop / remove-pop)
calls this helper at the top, so refusing here proves they all
refuse.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.clients.models import (
    Client, ClientOrganisationType, ClientUser, ClientUserRole,
)
from app.modules.platform.models import StatusEnum
from app.modules.seed_mgmt.router import (
    _assert_can_manage_seed_varieties, SEED_COMPANY_COSH_ID,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_client_user, make_user


async def _mark_as_seed_company(db, client: Client) -> None:
    db.add(ClientOrganisationType(
        client_id=client.id, org_type_cosh_id=SEED_COMPANY_COSH_ID,
    ))
    await db.flush()


# ── Org-type gate ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_refuses_non_seed_company(db):
    """Client with no Seed Company org-type → 403 not_a_seed_company,
    regardless of the user's role."""
    client = await make_client(db)  # no org-type added
    user = await make_user(db, name="CA", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.CA,
    )
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await _assert_can_manage_seed_varieties(db, user.id, client.id)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "not_a_seed_company"


# ── Role gate (within a Seed Company) ─────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_permits_ca_on_seed_company(db):
    client = await make_client(db)
    await _mark_as_seed_company(db, client)
    user = await make_user(db, name="CA", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.CA,
    )
    await db.commit()
    await _assert_can_manage_seed_varieties(db, user.id, client.id)


@requires_docker
@pytest.mark.asyncio
async def test_permits_se_with_seed_data_privilege(db):
    """Batch X (2026-05-19): SE who holds the SEED_DATA privilege
    passes (replaces the legacy SDM role test)."""
    from app.modules.clients.models import (
        ClientUserPrivilege, ClientUserPrivilegeModel,
    )
    client = await make_client(db)
    await _mark_as_seed_company(db, client)
    user = await make_user(db, name="SE+SeedData", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    db.add(ClientUserPrivilegeModel(
        client_id=client.id, user_id=user.id,
        privilege=ClientUserPrivilege.SEED_DATA,
    ))
    await db.commit()
    await _assert_can_manage_seed_varieties(db, user.id, client.id)


@requires_docker
@pytest.mark.asyncio
async def test_refuses_subject_expert_without_privilege(db):
    """SE without the SEED_DATA privilege can't manage varieties."""
    client = await make_client(db)
    await _mark_as_seed_company(db, client)
    user = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await _assert_can_manage_seed_varieties(db, user.id, client.id)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "seed_data_or_ca_only"


@requires_docker
@pytest.mark.asyncio
async def test_refuses_field_manager_on_seed_company(db):
    client = await make_client(db)
    await _mark_as_seed_company(db, client)
    user = await make_user(db, name="FM", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.FIELD_MANAGER,
    )
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await _assert_can_manage_seed_varieties(db, user.id, client.id)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "seed_data_or_ca_only"


@requires_docker
@pytest.mark.asyncio
async def test_se_with_other_privilege_only_still_refused(db):
    """An SE without the SEED_DATA privilege is still refused even
    if they have other ClientUser rows on the client."""
    client = await make_client(db)
    await _mark_as_seed_company(db, client)
    user = await make_user(db, name="FM+SE", skip_auto_link=True)
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.FIELD_MANAGER,
    )
    db.add(ClientUser(
        client_id=client.id, user_id=user.id,
        role=ClientUserRole.SUBJECT_EXPERT, status=StatusEnum.ACTIVE,
    ))
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await _assert_can_manage_seed_varieties(db, user.id, client.id)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "seed_data_or_ca_only"
