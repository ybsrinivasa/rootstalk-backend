"""R9 + R10 — Facilitator-Promoter invitation handshake (2026-05-29).

Pre-R9 the FM unilaterally flipped `is_promoter` via toggle_promoter_
flag. After R9 the Facilitator must accept; either side can step
down. Endpoint surface:

  Client side (FM):
    PUT  .../request-promoter  — NONE | DECLINED → PENDING (FACILITATOR)
                                 NONE → ACCEPTED (DEALER, auto-accept)
    PUT  .../revoke-promoter   — any state → NONE

  Facilitator side:
    GET  /facilitator/promoter-invitations
    PUT  /facilitator/promoter-invitations/{id}/accept   PENDING → ACCEPTED
    PUT  /facilitator/promoter-invitations/{id}/decline  PENDING → DECLINED
    PUT  /facilitator/promoter-status/{id}/step-down     ACCEPTED → NONE
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.clients.models import ClientPromoter
from app.modules.clients.router import (
    register_promoter, request_promoter, revoke_promoter,
)
from app.modules.orders.router import (
    facilitator_accept_promoter_invitation,
    facilitator_decline_promoter_invitation,
    facilitator_promoter_invitations,
    facilitator_step_down_promoter,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_self_registered_user, make_user,
)


def _payload(phone, promoter_type="FACILITATOR"):
    return {"phone": phone, "promoter_type": promoter_type, "territory_notes": None}


async def _onboard_facilitator(db, *, sa, client, phone):
    """Seed: client onboards the user as a Facilitator. is_promoter
    defaults to False; no invitation outstanding."""
    return await register_promoter(
        client_id=client.id, request=_payload(phone), db=db, current_user=sa,
    )


# ── Happy path: FM invites, Facilitator accepts ─────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_facilitator_request_then_accept_flips_to_promoter(db):
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    facilitator = await make_self_registered_user(
        db, phone="+919900300001", role="FACILITATOR",
    )
    await db.commit()

    cp = await _onboard_facilitator(db, sa=sa, client=client, phone="+919900300001")

    requested = await request_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )
    assert requested["promoter_request_status"] == "PENDING"
    assert requested["is_promoter"] is False
    assert requested["promoter_request_sent_at"] is not None

    # Facilitator sees the invitation in their pending list.
    invitations = await facilitator_promoter_invitations(
        db=db, current_user=facilitator,
    )
    assert len(invitations) == 1
    assert invitations[0]["client_promoter_id"] == cp["id"]
    assert invitations[0]["client_id"] == client.id

    # Facilitator accepts.
    accepted = await facilitator_accept_promoter_invitation(
        client_promoter_id=cp["id"], db=db, current_user=facilitator,
    )
    assert accepted["is_promoter"] is True
    assert accepted["promoter_request_status"] == "ACCEPTED"
    assert accepted["promoter_request_responded_at"] is not None


# ── Facilitator declines a pending invitation ───────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_facilitator_declines_invitation(db):
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    facilitator = await make_self_registered_user(
        db, phone="+919900300002", role="FACILITATOR",
    )
    await db.commit()

    cp = await _onboard_facilitator(db, sa=sa, client=client, phone="+919900300002")
    await request_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )

    declined = await facilitator_decline_promoter_invitation(
        client_promoter_id=cp["id"], db=db, current_user=facilitator,
    )
    assert declined["promoter_request_status"] == "DECLINED"

    # Pending list is now empty.
    invitations = await facilitator_promoter_invitations(
        db=db, current_user=facilitator,
    )
    assert invitations == []

    # is_promoter still False.
    row = (await db.execute(
        select(ClientPromoter).where(ClientPromoter.id == cp["id"])
    )).scalar_one()
    assert row.is_promoter is False


# ── FM revokes a pending invitation ─────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_fm_revoke_pending_invitation(db):
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    facilitator = await make_self_registered_user(
        db, phone="+919900300003", role="FACILITATOR",
    )
    await db.commit()

    cp = await _onboard_facilitator(db, sa=sa, client=client, phone="+919900300003")
    await request_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )

    revoked = await revoke_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )
    assert revoked["promoter_request_status"] == "NONE"

    invitations = await facilitator_promoter_invitations(
        db=db, current_user=facilitator,
    )
    assert invitations == []


# ── R10: Facilitator steps down after accepting ─────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_facilitator_step_down_after_acceptance(db):
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    facilitator = await make_self_registered_user(
        db, phone="+919900300004", role="FACILITATOR",
    )
    await db.commit()

    cp = await _onboard_facilitator(db, sa=sa, client=client, phone="+919900300004")
    await request_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )
    await facilitator_accept_promoter_invitation(
        client_promoter_id=cp["id"], db=db, current_user=facilitator,
    )

    out = await facilitator_step_down_promoter(
        client_promoter_id=cp["id"], db=db, current_user=facilitator,
    )
    assert out["is_promoter"] is False
    assert out["promoter_request_status"] == "NONE"


# ── R10: FM revokes after acceptance ────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_fm_revoke_after_acceptance(db):
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    facilitator = await make_self_registered_user(
        db, phone="+919900300005", role="FACILITATOR",
    )
    await db.commit()

    cp = await _onboard_facilitator(db, sa=sa, client=client, phone="+919900300005")
    await request_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )
    await facilitator_accept_promoter_invitation(
        client_promoter_id=cp["id"], db=db, current_user=facilitator,
    )

    revoked = await revoke_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )
    assert revoked["is_promoter"] is False
    assert revoked["promoter_request_status"] == "NONE"


# ── Multiple pending invitations stay; accepted blocks new requests ──────────

@requires_docker
@pytest.mark.asyncio
async def test_multiple_pending_allowed_only_one_accepted(db):
    """A Facilitator can have multiple PENDING invitations from
    different Clients. Accepting one doesn't auto-decline the others
    — the Facilitator might want to keep them as future options. But
    while one is ACCEPTED, a third Client trying to send a new
    request hits the §11.2 gate."""
    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    client_c = await make_client(db)
    facilitator = await make_self_registered_user(
        db, phone="+919900300006", role="FACILITATOR",
    )
    await db.commit()

    cp_a = await _onboard_facilitator(db, sa=sa, client=client_a, phone="+919900300006")
    cp_b = await _onboard_facilitator(db, sa=sa, client=client_b, phone="+919900300006")

    # Both A and B invite — both PENDING.
    await request_promoter(
        client_id=client_a.id, promoter_id=cp_a["id"], db=db, current_user=sa,
    )
    await request_promoter(
        client_id=client_b.id, promoter_id=cp_b["id"], db=db, current_user=sa,
    )
    invitations = await facilitator_promoter_invitations(
        db=db, current_user=facilitator,
    )
    assert len(invitations) == 2

    # Facilitator accepts A. B's PENDING invitation survives.
    await facilitator_accept_promoter_invitation(
        client_promoter_id=cp_a["id"], db=db, current_user=facilitator,
    )
    invitations = await facilitator_promoter_invitations(
        db=db, current_user=facilitator,
    )
    assert len(invitations) == 1
    assert invitations[0]["client_id"] == client_b.id

    # A new Client C now tries to invite — refused by §11.2 request-
    # time gate (Facilitator is already ACCEPTED at A).
    cp_c = await _onboard_facilitator(db, sa=sa, client=client_c, phone="+919900300006")
    with pytest.raises(HTTPException) as ei:
        await request_promoter(
            client_id=client_c.id, promoter_id=cp_c["id"],
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "facilitator_already_active_elsewhere"


# ── Facilitator can't accept someone else's invitation ──────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_facilitator_cannot_accept_other_users_invitation(db):
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    other = await make_self_registered_user(
        db, phone="+919900300007", role="FACILITATOR",
    )
    impostor = await make_self_registered_user(
        db, phone="+919900300099", role="FACILITATOR",
    )
    await db.commit()

    cp = await _onboard_facilitator(db, sa=sa, client=client, phone="+919900300007")
    await request_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )

    with pytest.raises(HTTPException) as ei:
        await facilitator_accept_promoter_invitation(
            client_promoter_id=cp["id"], db=db, current_user=impostor,
        )
    assert ei.value.status_code == 404


# ── Decline only works on PENDING; step-down only on ACCEPTED ───────────────

@requires_docker
@pytest.mark.asyncio
async def test_decline_rejects_non_pending(db):
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    facilitator = await make_self_registered_user(
        db, phone="+919900300008", role="FACILITATOR",
    )
    await db.commit()

    cp = await _onboard_facilitator(db, sa=sa, client=client, phone="+919900300008")
    await request_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )
    await facilitator_accept_promoter_invitation(
        client_promoter_id=cp["id"], db=db, current_user=facilitator,
    )

    with pytest.raises(HTTPException) as ei:
        await facilitator_decline_promoter_invitation(
            client_promoter_id=cp["id"], db=db, current_user=facilitator,
        )
    assert ei.value.status_code == 409


@requires_docker
@pytest.mark.asyncio
async def test_step_down_rejects_when_not_promoter(db):
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    facilitator = await make_self_registered_user(
        db, phone="+919900300009", role="FACILITATOR",
    )
    await db.commit()

    cp = await _onboard_facilitator(db, sa=sa, client=client, phone="+919900300009")
    # Onboarded but never invited.
    with pytest.raises(HTTPException) as ei:
        await facilitator_step_down_promoter(
            client_promoter_id=cp["id"], db=db, current_user=facilitator,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "not_currently_promoter"


# ── Re-invite after decline ─────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_fm_can_re_invite_after_decline(db):
    """A DECLINED row can be invited again (DECLINED → PENDING).
    Lets the FM re-pitch after the Facilitator's circumstances
    change."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    facilitator = await make_self_registered_user(
        db, phone="+919900300010", role="FACILITATOR",
    )
    await db.commit()

    cp = await _onboard_facilitator(db, sa=sa, client=client, phone="+919900300010")
    await request_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )
    await facilitator_decline_promoter_invitation(
        client_promoter_id=cp["id"], db=db, current_user=facilitator,
    )

    re_invited = await request_promoter(
        client_id=client.id, promoter_id=cp["id"], db=db, current_user=sa,
    )
    assert re_invited["promoter_request_status"] == "PENDING"
