"""R14 — Facilitator can see the list of Clients who have onboarded them.

GET /facilitator/onboarding-clients returns the caller's active
FACILITATOR ClientPromoter rows joined to the Client display fields,
ordered newest-first. Empty for non-functional Facilitators (NOT 403)
so the PWA can render the "No companies have onboarded you yet" state.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.clients.models import ClientPromoter
from app.modules.clients.router import register_promoter
from app.modules.orders.router import facilitator_onboarding_clients
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_self_registered_user, make_user,
)


def _payload(*, phone, promoter_type="FACILITATOR"):
    return {
        "phone": phone,
        "promoter_type": promoter_type,
        "territory_notes": None,
    }


@requires_docker
@pytest.mark.asyncio
async def test_facilitator_with_no_onboardings_gets_empty_list(db):
    """Newly-registered Facilitator with no Field-Manager onboarding
    yet: empty list, NOT a 403. Lets the PWA distinguish "you're not
    a Facilitator" (frontend gate) from "you're a Facilitator pending
    onboarding"."""
    facilitator = await make_self_registered_user(
        db, phone="+919900000010", role="FACILITATOR",
    )
    await db.commit()

    out = await facilitator_onboarding_clients(db=db, current_user=facilitator)
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_lists_all_active_facilitator_onboardings(db):
    """Two clients onboard the same Facilitator → both appear,
    newest registered_at first."""
    sa = await make_user(db, name="SA")
    client_a = await make_client(db, full_name="Alpha Agri", short_name="alpha")
    client_b = await make_client(db, full_name="Bravo Biotech", short_name="bravo")
    facilitator = await make_self_registered_user(
        db, phone="+919900000011", role="FACILITATOR",
    )
    await db.commit()

    await register_promoter(
        client_id=client_a.id, request=_payload(phone="+919900000011"),
        db=db, current_user=sa,
    )
    await register_promoter(
        client_id=client_b.id, request=_payload(phone="+919900000011"),
        db=db, current_user=sa,
    )

    out = await facilitator_onboarding_clients(db=db, current_user=facilitator)
    assert len(out) == 2
    client_ids = {row["client_id"] for row in out}
    assert client_ids == {client_a.id, client_b.id}
    # Newest first.
    assert out[0]["client_id"] == client_b.id
    assert out[1]["client_id"] == client_a.id
    # Each row carries enough to brand the card in the PWA.
    for row in out:
        assert "client_name" in row
        assert "short_name" in row
        assert "logo_url" in row
        assert "primary_colour" in row
        assert "is_promoter" in row
        assert "onboarded_at" in row


@requires_docker
@pytest.mark.asyncio
async def test_inactive_onboardings_excluded(db):
    """A Facilitator who was deactivated at one of two clients sees
    only the still-active one."""
    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    facilitator = await make_self_registered_user(
        db, phone="+919900000012", role="FACILITATOR",
    )
    await db.commit()

    await register_promoter(
        client_id=client_a.id, request=_payload(phone="+919900000012"),
        db=db, current_user=sa,
    )
    await register_promoter(
        client_id=client_b.id, request=_payload(phone="+919900000012"),
        db=db, current_user=sa,
    )
    # Flip client_a's row to INACTIVE directly (mirrors what the
    # deactivate_promoter endpoint does).
    cp_a = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.client_id == client_a.id,
            ClientPromoter.user_id == facilitator.id,
        )
    )).scalar_one()
    cp_a.status = "INACTIVE"
    await db.commit()

    out = await facilitator_onboarding_clients(db=db, current_user=facilitator)
    assert len(out) == 1
    assert out[0]["client_id"] == client_b.id


@requires_docker
@pytest.mark.asyncio
async def test_dealer_rows_excluded(db):
    """The same user could be a DEALER at one client AND a FACILITATOR
    at another. /facilitator/onboarding-clients lists FACILITATOR
    rows only."""
    from app.modules.platform.models import RoleType, UserRole

    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    user = await make_self_registered_user(
        db, phone="+919900000013", role="FACILITATOR",
    )
    # User self-claims DEALER too — the FM endpoint requires the role
    # to be pre-claimed before onboarding the user as a DEALER.
    db.add(UserRole(user_id=user.id, role_type=RoleType.DEALER))
    await db.commit()

    await register_promoter(
        client_id=client_a.id, request=_payload(phone="+919900000013"),
        db=db, current_user=sa,
    )
    await register_promoter(
        client_id=client_b.id,
        request=_payload(phone="+919900000013", promoter_type="DEALER"),
        db=db, current_user=sa,
    )

    out = await facilitator_onboarding_clients(db=db, current_user=user)
    assert len(out) == 1
    assert out[0]["client_id"] == client_a.id


@requires_docker
@pytest.mark.asyncio
async def test_is_promoter_flag_reflected(db):
    """Two FACILITATOR onboardings; one marked Promoter, the other not.
    `is_promoter` on each row mirrors the ClientPromoter flag — lets
    the PWA show a "Promoter" badge on the right card."""
    from app.modules.clients.router import toggle_promoter_flag

    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    facilitator = await make_self_registered_user(
        db, phone="+919900000014", role="FACILITATOR",
    )
    await db.commit()

    out_a = await register_promoter(
        client_id=client_a.id, request=_payload(phone="+919900000014"),
        db=db, current_user=sa,
    )
    await register_promoter(
        client_id=client_b.id, request=_payload(phone="+919900000014"),
        db=db, current_user=sa,
    )
    await toggle_promoter_flag(
        client_id=client_a.id, promoter_id=out_a["id"],
        request={"is_promoter": True},
        db=db, current_user=sa,
    )

    out = await facilitator_onboarding_clients(db=db, current_user=facilitator)
    by_client = {row["client_id"]: row for row in out}
    assert by_client[client_a.id]["is_promoter"] is True
    assert by_client[client_b.id]["is_promoter"] is False


# ── R12: cascade is_promoter=False on deactivation ──────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_deactivate_promoter_clears_is_promoter_flag(db):
    """R12 (2026-05-29): when a Client deactivates a Facilitator who
    was their Promoter, the cascade must clear `is_promoter` on the
    same row. Otherwise the flag stales — every R8/§11.2 gate filters
    on status='ACTIVE' so the live behaviour is correct, but a future
    `reactivate_promoter` would silently re-grant Promoter status
    without an explicit re-assignment."""
    from app.modules.clients.router import (
        deactivate_promoter, toggle_promoter_flag,
    )

    sa = await make_user(db, name="SA")
    client = await make_client(db)
    facilitator = await make_self_registered_user(
        db, phone="+919900000015", role="FACILITATOR",
    )
    await db.commit()

    out = await register_promoter(
        client_id=client.id, request=_payload(phone="+919900000015"),
        db=db, current_user=sa,
    )
    await toggle_promoter_flag(
        client_id=client.id, promoter_id=out["id"],
        request={"is_promoter": True},
        db=db, current_user=sa,
    )

    # Sanity: flag is set before deactivation.
    cp = (await db.execute(
        select(ClientPromoter).where(ClientPromoter.id == out["id"])
    )).scalar_one()
    assert cp.status == "ACTIVE"
    assert cp.is_promoter is True

    # Deactivate → status flips to INACTIVE AND is_promoter clears.
    await deactivate_promoter(
        client_id=client.id, promoter_id=out["id"],
        db=db, current_user=sa,
    )
    await db.refresh(cp)
    assert cp.status == "INACTIVE"
    assert cp.is_promoter is False
