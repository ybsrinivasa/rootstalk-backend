"""Promoter designation via request_promoter / revoke_promoter.

Rewritten 2026-05-29 for R9. Pre-R9 this file exercised
`toggle_promoter_flag` (FM unilaterally flipped `is_promoter`).
R9 split the Promoter sub-role into a two-sided handshake for
Facilitators (request → Facilitator accept) while keeping Dealer-
Promoters one-sided (auto-accept on request). Endpoint surface:

  Client side:
    PUT .../request-promoter  — DEALER: NONE → ACCEPTED (auto)
                                FACILITATOR: NONE → PENDING
    PUT .../revoke-promoter   — any state → NONE (FM teardown)

The Facilitator-side accept/decline/step-down lives in
`tests/test_phase_facilitator_promoter_invitation.py`.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.clients.models import ClientPromoter
from app.modules.clients.router import (
    register_promoter, request_promoter, revoke_promoter,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_self_registered_user, make_user,
)


async def _onboard(db, *, sa, client, phone, promoter_type="FACILITATOR"):
    return await register_promoter(
        client_id=client.id,
        request={"phone": phone, "promoter_type": promoter_type},
        db=db, current_user=sa,
    )


# ── Dealer: request auto-accepts (no handshake needed) ───────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_dealer_request_auto_accepts(db):
    """Per spec §11.2 Dealers are multi-company and don't need
    farmer-side consent. request_promoter on a Dealer flips
    `is_promoter=True` immediately."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await make_self_registered_user(db, phone="+919900200001", role="DEALER")
    await db.commit()

    cp = await _onboard(db, sa=sa, client=client, phone="+919900200001", promoter_type="DEALER")
    assert cp["is_promoter"] is False   # default after V1.1 Item 4

    out = await request_promoter(
        client_id=client.id, promoter_id=cp["id"],
        db=db, current_user=sa,
    )
    assert out["is_promoter"] is True
    assert out["promoter_request_status"] == "ACCEPTED"


# ── Dealer: revoke returns the row to NONE ───────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_dealer_revoke_flips_flag_back(db):
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await make_self_registered_user(db, phone="+919900200002", role="DEALER")
    await db.commit()

    cp = await _onboard(db, sa=sa, client=client, phone="+919900200002", promoter_type="DEALER")
    await request_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )

    out = await revoke_promoter(
        client_id=client.id, promoter_id=cp["id"],
        db=db, current_user=sa,
    )
    assert out["is_promoter"] is False
    assert out["promoter_request_status"] == "NONE"


# ── Dealer-Promoter at multiple clients is allowed ───────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_dealer_promoter_at_multiple_clients_allowed(db):
    """§11.2 Dealer-Promoter carve-out — multi-company by spec."""
    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    await make_self_registered_user(db, phone="+919900200003", role="DEALER")
    await db.commit()

    cp_a = await _onboard(db, sa=sa, client=client_a, phone="+919900200003", promoter_type="DEALER")
    cp_b = await _onboard(db, sa=sa, client=client_b, phone="+919900200003", promoter_type="DEALER")
    await request_promoter(
        client_id=client_a.id, promoter_id=cp_a["id"], db=db, current_user=sa,
    )
    out = await request_promoter(
        client_id=client_b.id, promoter_id=cp_b["id"], db=db, current_user=sa,
    )
    assert out["is_promoter"] is True
    assert out["promoter_request_status"] == "ACCEPTED"


# ── Status + cross-client guards (shared between dealer + facilitator) ──────

@requires_docker
@pytest.mark.asyncio
async def test_request_blocked_when_row_inactive(db):
    """Can't request Promoter on an INACTIVE row — reactivate first."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await make_self_registered_user(db, phone="+919900200004", role="FACILITATOR")
    await db.commit()

    cp = await _onboard(db, sa=sa, client=client, phone="+919900200004")
    from sqlalchemy import update
    await db.execute(
        update(ClientPromoter)
        .where(ClientPromoter.id == cp["id"])
        .values(status="INACTIVE")
    )
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await request_promoter(
            client_id=client.id, promoter_id=cp["id"],
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 409


@requires_docker
@pytest.mark.asyncio
async def test_request_404_when_row_belongs_to_other_client(db):
    """Cross-client URL tampering returns 404 — same shape as
    'not found' so the existence of other clients' rows isn't leaked."""
    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    await make_self_registered_user(db, phone="+919900200005", role="DEALER")
    await db.commit()

    cp_a = await _onboard(db, sa=sa, client=client_a, phone="+919900200005", promoter_type="DEALER")

    with pytest.raises(HTTPException) as ei:
        await request_promoter(
            client_id=client_b.id, promoter_id=cp_a["id"],
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 404


# ── Double-request guard ────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_double_request_is_rejected(db):
    """An outstanding PENDING or ACCEPTED request blocks a second
    request from the same Client — revoke first to send anew."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await make_self_registered_user(db, phone="+919900200006", role="DEALER")
    await db.commit()

    cp = await _onboard(db, sa=sa, client=client, phone="+919900200006", promoter_type="DEALER")
    await request_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )
    with pytest.raises(HTTPException) as ei:
        await request_promoter(
            client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "promoter_already_outstanding"
