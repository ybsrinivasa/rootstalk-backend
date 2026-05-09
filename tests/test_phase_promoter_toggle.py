"""Mark-as-Promoter toggle — V1.1 Item 4 (2026-05-09).

Onboarding (register_promoter) creates a ClientPromoter row with
is_promoter=False; the FM flips the flag explicitly via this
endpoint. Spec §11.2 Facilitator-Promoter exclusivity now lives
here too.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.clients.models import ClientPromoter
from app.modules.clients.router import (
    register_promoter, toggle_promoter_flag,
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


# ── Basic toggle behaviour ──────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_mark_as_promoter_flips_flag(db):
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await make_self_registered_user(db, phone="+919900200001", role="DEALER")
    await db.commit()

    cp = await _onboard(db, sa=sa, client=client, phone="+919900200001", promoter_type="DEALER")
    assert cp["is_promoter"] is False  # default after Item 4

    out = await toggle_promoter_flag(
        client_id=client.id, promoter_id=cp["id"],
        request={"is_promoter": True},
        db=db, current_user=sa,
    )
    assert out["is_promoter"] is True


@requires_docker
@pytest.mark.asyncio
async def test_unmark_promoter_flips_flag_back(db):
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await make_self_registered_user(db, phone="+919900200002", role="DEALER")
    await db.commit()

    cp = await _onboard(db, sa=sa, client=client, phone="+919900200002", promoter_type="DEALER")
    await toggle_promoter_flag(
        client_id=client.id, promoter_id=cp["id"],
        request={"is_promoter": True}, db=db, current_user=sa,
    )

    out = await toggle_promoter_flag(
        client_id=client.id, promoter_id=cp["id"],
        request={"is_promoter": False},
        db=db, current_user=sa,
    )
    assert out["is_promoter"] is False


@requires_docker
@pytest.mark.asyncio
async def test_dealer_promoter_at_multiple_clients_allowed(db):
    """Dealers are exempt from the §11.2 Facilitator exclusivity
    rule — they can be Promoters at multiple companies. This is an
    explicit spec carve-out."""
    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    await make_self_registered_user(db, phone="+919900200003", role="DEALER")
    await db.commit()

    cp_a = await _onboard(db, sa=sa, client=client_a, phone="+919900200003", promoter_type="DEALER")
    cp_b = await _onboard(db, sa=sa, client=client_b, phone="+919900200003", promoter_type="DEALER")
    await toggle_promoter_flag(
        client_id=client_a.id, promoter_id=cp_a["id"],
        request={"is_promoter": True}, db=db, current_user=sa,
    )
    out = await toggle_promoter_flag(
        client_id=client_b.id, promoter_id=cp_b["id"],
        request={"is_promoter": True}, db=db, current_user=sa,
    )
    assert out["is_promoter"] is True


# ── Status + cross-client guards ────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_toggle_blocked_when_row_inactive(db):
    """Can't toggle the flag on an INACTIVE row — reactivate first.
    Otherwise we'd silently flip a defunct attachment."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await make_self_registered_user(db, phone="+919900200004", role="FACILITATOR")
    await db.commit()

    cp_resp = await _onboard(db, sa=sa, client=client, phone="+919900200004")
    cp = (await db.execute(
        ClientPromoter.__table__.select().where(ClientPromoter.id == cp_resp["id"])
    )).first()
    # Direct flip to INACTIVE for the test setup.
    from sqlalchemy import update
    await db.execute(
        update(ClientPromoter)
        .where(ClientPromoter.id == cp_resp["id"])
        .values(status="INACTIVE")
    )
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await toggle_promoter_flag(
            client_id=client.id, promoter_id=cp_resp["id"],
            request={"is_promoter": True},
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 409


@requires_docker
@pytest.mark.asyncio
async def test_toggle_404_when_row_belongs_to_other_client(db):
    """Cross-client URL tampering returns 404 — same shape as
    'not found' so the existence of other clients' rows isn't leaked."""
    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    await make_self_registered_user(db, phone="+919900200005", role="DEALER")
    await db.commit()

    cp_a = await _onboard(db, sa=sa, client=client_a, phone="+919900200005", promoter_type="DEALER")

    with pytest.raises(HTTPException) as ei:
        await toggle_promoter_flag(
            client_id=client_b.id, promoter_id=cp_a["id"],
            request={"is_promoter": True},
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 404


# ── Already-promoter idempotence ────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_marking_already_promoter_is_idempotent(db):
    """Re-marking a row that's already is_promoter=True is a 200
    no-op — pages can fire-and-forget on a button click."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await make_self_registered_user(db, phone="+919900200006", role="DEALER")
    await db.commit()

    cp = await _onboard(db, sa=sa, client=client, phone="+919900200006", promoter_type="DEALER")
    await toggle_promoter_flag(
        client_id=client.id, promoter_id=cp["id"],
        request={"is_promoter": True}, db=db, current_user=sa,
    )

    # Second call — no exclusivity check fires for the same row,
    # since the existence-elsewhere predicate excludes self-id.
    out = await toggle_promoter_flag(
        client_id=client.id, promoter_id=cp["id"],
        request={"is_promoter": True}, db=db, current_user=sa,
    )
    assert out["is_promoter"] is True
