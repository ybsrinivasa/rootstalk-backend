"""Spec §11.2 — Facilitator-Promoter is exclusive per company (M9).

Spec table for §11.2 Promoters:
  Facilitator-Promoter   → "One company at a time"
  Dealer-Promoter        → "Multiple companies simultaneously"
  Company-designated     → "As configured"

Pre-fix, register_promoter enforced none of this — a person could
silently end up as an ACTIVE FACILITATOR at multiple clients. Fixed
2026-05-08 by adding a cross-client uniqueness check on FACILITATOR
registrations only. Dealers remain unchanged (multi-company by spec).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.clients.models import ClientPromoter
from app.modules.clients.router import register_promoter
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_self_registered_user, make_user,
)


def _payload(*, phone, promoter_type="FACILITATOR"):
    """V1.1 Item 3 (2026-05-09): name no longer accepted — comes
    from the pre-seeded User. The FM endpoint takes phone +
    promoter_type + optional territory_notes only."""
    return {
        "phone": phone,
        "promoter_type": promoter_type, "territory_notes": None,
    }


# ── M9: cross-client Facilitator uniqueness ─────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_facilitator_register_at_first_client_succeeds(db):
    """Sanity check — first-time Facilitator registration is allowed."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await make_self_registered_user(db, phone="+919900000001", role="FACILITATOR")
    await db.commit()

    out = await register_promoter(
        client_id=client.id,
        request=_payload(phone="+919900000001"),
        db=db, current_user=sa,
    )
    assert out["promoter_type"] == "FACILITATOR"


@requires_docker
@pytest.mark.asyncio
async def test_facilitator_marked_promoter_blocks_marking_at_second_client(db):
    """V1.1 Item 4 (2026-05-09): the §11.2 Facilitator-Promoter
    exclusivity gate moved to the toggle endpoint. Onboarding a
    Facilitator at multiple clients is fine; what's exclusive is
    being MARKED as a Promoter at more than one. Structured 409
    detail so the frontend can pin the message + display
    'unmark at the other company first'."""
    from app.modules.clients.router import toggle_promoter_flag

    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    await make_self_registered_user(db, phone="+919900000002", role="FACILITATOR")
    await db.commit()

    # Both onboardings succeed (plain Facilitator multi-company OK).
    out_a = await register_promoter(
        client_id=client_a.id, request=_payload(phone="+919900000002"),
        db=db, current_user=sa,
    )
    out_b = await register_promoter(
        client_id=client_b.id, request=_payload(phone="+919900000002"),
        db=db, current_user=sa,
    )

    # A marks them as Promoter — fine.
    await toggle_promoter_flag(
        client_id=client_a.id, promoter_id=out_a["id"],
        request={"is_promoter": True},
        db=db, current_user=sa,
    )

    # B tries to mark them as Promoter — refused.
    with pytest.raises(HTTPException) as ei:
        await toggle_promoter_flag(
            client_id=client_b.id, promoter_id=out_b["id"],
            request={"is_promoter": True},
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 409
    assert isinstance(ei.value.detail, dict)
    assert ei.value.detail["code"] == "facilitator_already_active_elsewhere"
    # Privacy guard: the message must NOT name the other client.
    assert client_a.id not in ei.value.detail["message"]
    assert client_a.short_name not in ei.value.detail["message"]


@requires_docker
@pytest.mark.asyncio
async def test_dealer_register_at_multiple_clients_allowed(db):
    """Dealers are exempt from the uniqueness rule — spec §11.2 calls
    them out as 'multiple companies simultaneously'. The check must
    only fire on FACILITATOR registrations."""
    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    await make_self_registered_user(db, phone="+919900000003", role="DEALER")
    await db.commit()

    await register_promoter(
        client_id=client_a.id,
        request=_payload(phone="+919900000003", promoter_type="DEALER"),
        db=db, current_user=sa,
    )

    # Same person at a second client — must succeed for dealers.
    out = await register_promoter(
        client_id=client_b.id,
        request=_payload(phone="+919900000003", promoter_type="DEALER"),
        db=db, current_user=sa,
    )
    assert out["promoter_type"] == "DEALER"


@requires_docker
@pytest.mark.asyncio
async def test_facilitator_promoter_can_move_after_deactivation(db):
    """The exclusivity check looks at status=ACTIVE only — a
    Facilitator-Promoter at a deactivated A row no longer blocks
    marking the same user as Promoter at B. Supports the
    move-between-companies flow."""
    from app.modules.clients.router import toggle_promoter_flag

    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    await make_self_registered_user(db, phone="+919900000004", role="FACILITATOR")
    await db.commit()

    # Onboard at both, mark Promoter at A.
    out_a = await register_promoter(
        client_id=client_a.id, request=_payload(phone="+919900000004"),
        db=db, current_user=sa,
    )
    out_b = await register_promoter(
        client_id=client_b.id, request=_payload(phone="+919900000004"),
        db=db, current_user=sa,
    )
    await toggle_promoter_flag(
        client_id=client_a.id, promoter_id=out_a["id"],
        request={"is_promoter": True}, db=db, current_user=sa,
    )

    # Deactivate the A row → exclusivity lock releases.
    cp_a = (await db.execute(
        select(ClientPromoter).where(ClientPromoter.id == out_a["id"])
    )).scalar_one()
    cp_a.status = "INACTIVE"
    await db.commit()

    # B can now mark Promoter.
    out = await toggle_promoter_flag(
        client_id=client_b.id, promoter_id=out_b["id"],
        request={"is_promoter": True}, db=db, current_user=sa,
    )
    assert out["is_promoter"] is True


@requires_docker
@pytest.mark.asyncio
async def test_facilitator_dealer_at_different_clients_allowed(db):
    """Same person can be a Facilitator at A and a Dealer at B —
    the uniqueness rule is per-promoter-type. A Dealer row at B
    doesn't block a Facilitator row at A or vice versa."""
    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    # Same User has both roles claimed (Dealer + Facilitator).
    user = await make_self_registered_user(db, phone="+919900000005", role="DEALER")
    from app.modules.platform.models import RoleType, UserRole
    db.add(UserRole(user_id=user.id, role_type=RoleType.FACILITATOR))
    await db.commit()

    # Dealer at A first.
    await register_promoter(
        client_id=client_a.id,
        request=_payload(phone="+919900000005", promoter_type="DEALER"),
        db=db, current_user=sa,
    )

    # Same person as Facilitator at B — allowed.
    out = await register_promoter(
        client_id=client_b.id,
        request=_payload(phone="+919900000005", promoter_type="FACILITATOR"),
        db=db, current_user=sa,
    )
    assert out["promoter_type"] == "FACILITATOR"


@requires_docker
@pytest.mark.asyncio
async def test_existing_same_client_facilitator_still_blocked(db):
    """Regression check: the cross-client gate doesn't accidentally
    short-circuit the existing same-client duplicate check (the
    "already registered at this client" error path)."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await make_self_registered_user(db, phone="+919900000006", role="FACILITATOR")
    await db.commit()

    await register_promoter(
        client_id=client.id,
        request=_payload(phone="+919900000006"),
        db=db, current_user=sa,
    )

    with pytest.raises(HTTPException) as ei:
        await register_promoter(
            client_id=client.id,
            request=_payload(phone="+919900000006"),
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 409
    # Same-client message uses string detail, not the structured dict.
    assert isinstance(ei.value.detail, str)
    assert "Facilitator" in ei.value.detail
