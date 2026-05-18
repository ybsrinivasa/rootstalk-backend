"""Batch P (2026-05-18) — Content Manager can reach client detail
read endpoints for clients they're assigned to.

Per user feedback 2026-05-18: when a CM clicks "My Clients" on SA
Portal and tries to open a client's Details page, the GET
`/admin/clients/{cid}` was refusing them (403 → frontend rendered
a 404-feel state). Widened to accept SA OR CM-with-ACTIVE-assignment.
GET `/admin/clients/{cid}/cm-assignment` widened the same way so the
CM can see their own assignment in the detail-page panel.

PUT / DELETE remain SA-only — CMs can't reassign themselves or
edit client metadata.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.config import settings
from app.modules.clients.models import (
    CMClientAssignment, CMRights, ClientStatus,
)
from app.modules.clients.router import get_client, get_cm_assignment
from app.modules.platform.models import StatusEnum
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


async def _activate_client(db, client) -> None:
    client.status = ClientStatus.ACTIVE
    await db.flush()


async def _make_sa(db, *, name="SA"):
    sa = await make_user(db, name=name, skip_auto_link=True)
    sa.email = settings.sa_email
    await db.flush()
    return sa


async def _make_cm_with_assignment(db, *, client, name="CM", rights=CMRights.EDIT):
    cm = await make_user(db, name=name, skip_auto_link=True)
    db.add(CMClientAssignment(
        cm_user_id=cm.id, client_id=client.id,
        rights=rights, status=StatusEnum.ACTIVE,
    ))
    await db.flush()
    return cm


# ── get_client ─────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_get_client_permits_sa(db):
    client = await make_client(db)
    await _activate_client(db, client)
    sa = await _make_sa(db)
    await db.commit()
    out = await get_client(client_id=client.id, db=db, current_user=sa)
    assert out.id == client.id


@requires_docker
@pytest.mark.asyncio
async def test_get_client_permits_assigned_cm(db):
    """The headline bug fix: CM with active assignment can fetch
    client details for that client."""
    client = await make_client(db)
    await _activate_client(db, client)
    cm = await _make_cm_with_assignment(db, client=client)
    await db.commit()
    out = await get_client(client_id=client.id, db=db, current_user=cm)
    assert out.id == client.id


@requires_docker
@pytest.mark.asyncio
async def test_get_client_refuses_unassigned_cm(db):
    """A CM who has NO assignment to this client gets 403 —
    privilege doesn't widen across clients."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    await _activate_client(db, client_a)
    await _activate_client(db, client_b)
    cm_a = await _make_cm_with_assignment(db, client=client_a)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await get_client(client_id=client_b.id, db=db, current_user=cm_a)
    assert exc.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_get_client_refuses_inactive_cm_assignment(db):
    """An assignment with status != ACTIVE doesn't grant access."""
    client = await make_client(db)
    await _activate_client(db, client)
    cm = await make_user(db, name="ExCM", skip_auto_link=True)
    db.add(CMClientAssignment(
        cm_user_id=cm.id, client_id=client.id,
        rights=CMRights.EDIT, status=StatusEnum.INACTIVE,
    ))
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await get_client(client_id=client.id, db=db, current_user=cm)
    assert exc.value.status_code == 403


# ── get_cm_assignment ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_get_cm_assignment_permits_assigned_cm(db):
    client = await make_client(db)
    await _activate_client(db, client)
    cm = await _make_cm_with_assignment(db, client=client)
    await db.commit()
    out = await get_cm_assignment(
        client_id=client.id, db=db, current_user=cm,
    )
    assert out["cm_user_id"] == cm.id
    assert out["rights"] == "EDIT"
