"""ClientPromoter `is_promoter` flag — Option C separation (2026-05-08).

Pre-Option-C, every row in `client_promoters` was treated as both a
company-onboarding link AND a Promoter designation. The new flag
splits those two concepts:

  is_promoter=False  → Dealer/Facilitator onboarded at company
                       but not yet a Promoter (can't assign packages)
  is_promoter=True   → Dealer/Facilitator + Promoter (can assign)

Two gates moved to read the new flag:
- M9 (Facilitator-Promoter exclusivity per spec §11.2): now checks
  `is_promoter=True AND promoter_type=FACILITATOR` at OTHER clients,
  not just any Facilitator row.
- M5 (Promoter-Pundit eligibility per spec §14.2): toggle now
  requires the underlying ClientPromoter row to have is_promoter=True
  (in addition to type=FACILITATOR + status=ACTIVE as before).

The current CA-portal flow still creates rows with is_promoter=True
by default, so the existing UX is unchanged for V1. The schema is
ready for the V1.1 redesign that introduces a separate Mark-as-
Promoter step.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.clients.models import ClientPromoter
from app.modules.clients.router import register_promoter
from app.modules.farmpundit.models import (
    ClientFarmPundit, FarmPunditProfile, PunditRole,
)
from app.modules.farmpundit.router import toggle_promoter_pundit
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_self_registered_user, make_user,
)


# ── Schema sanity ───────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_register_promoter_defaults_is_promoter_false(db):
    """V1.1 Item 4 (2026-05-09): onboarding ≠ Promoter designation
    per spec §11.2. New rows default to is_promoter=False; the FM
    flips the flag explicitly via the toggle endpoint."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await make_self_registered_user(db, phone="+919900111100", role="FACILITATOR")
    await db.commit()

    out = await register_promoter(
        client_id=client.id,
        request={
            "phone": "+919900111100",
            "promoter_type": "FACILITATOR", "territory_notes": None,
        },
        db=db, current_user=sa,
    )
    assert out["is_promoter"] is False


# ── M9: gate moves to is_promoter flag ──────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_facilitator_non_promoter_at_other_client_does_not_block(db):
    """A Facilitator who is onboarded at client A but NOT marked as
    a Promoter (is_promoter=False) doesn't block onboarding OR
    promoter-marking at client B. Only the Facilitator-PROMOTER
    combination is exclusive per spec §11.2."""
    from app.modules.clients.router import register_promoter, request_promoter
    from app.modules.orders.router import facilitator_accept_promoter_invitation
    from app.modules.platform.models import User as PlatformUser

    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    await make_self_registered_user(db, phone="+919900111101", role="FACILITATOR", name="Mover")
    await db.commit()

    # Onboarded at A — is_promoter defaults to False (Item 4 default).
    await register_promoter(
        client_id=client_a.id,
        request={"phone": "+919900111101", "promoter_type": "FACILITATOR"},
        db=db, current_user=sa,
    )

    # Onboard at B — also fine (plain Facilitator multi-company OK).
    out_b = await register_promoter(
        client_id=client_b.id,
        request={"phone": "+919900111101", "promoter_type": "FACILITATOR"},
        db=db, current_user=sa,
    )
    assert out_b["is_promoter"] is False

    # Mark Promoter at B — A has is_promoter=False so the §11.2 gate
    # doesn't fire. B invites + Facilitator accepts → sole Promoter.
    facilitator = (await db.execute(
        select(PlatformUser).where(PlatformUser.phone == "+919900111101")
    )).scalar_one()
    await request_promoter(
        client_id=client_b.id, promoter_id=out_b["id"], db=db, current_user=sa,
    )
    accepted = await facilitator_accept_promoter_invitation(
        client_promoter_id=out_b["id"], db=db, current_user=facilitator,
    )
    assert accepted["is_promoter"] is True


@requires_docker
@pytest.mark.asyncio
async def test_facilitator_promoter_at_other_client_blocks_toggle(db):
    """V1.1 Item 4: when the Facilitator at A is marked as Promoter,
    marking the same user as Promoter at B is refused — the spec
    §11.2 exclusivity gate now lives on the toggle endpoint."""
    from app.modules.clients.router import (
        register_promoter, request_promoter, revoke_promoter,
    )
    from app.modules.orders.router import facilitator_accept_promoter_invitation
    from app.modules.platform.models import User as PlatformUser

    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    await make_self_registered_user(db, phone="+919900111102", role="FACILITATOR", name="Locked")
    await db.commit()

    out_a = await register_promoter(
        client_id=client_a.id,
        request={"phone": "+919900111102", "promoter_type": "FACILITATOR"},
        db=db, current_user=sa,
    )
    out_b = await register_promoter(
        client_id=client_b.id,
        request={"phone": "+919900111102", "promoter_type": "FACILITATOR"},
        db=db, current_user=sa,
    )
    facilitator = (await db.execute(
        select(PlatformUser).where(PlatformUser.phone == "+919900111102")
    )).scalar_one()

    # FM at A invites + Facilitator accepts.
    await request_promoter(
        client_id=client_a.id, promoter_id=out_a["id"], db=db, current_user=sa,
    )
    await facilitator_accept_promoter_invitation(
        client_promoter_id=out_a["id"], db=db, current_user=facilitator,
    )

    # FM at B tries to invite — refused at request-time (§11.2).
    with pytest.raises(HTTPException) as ei:
        await request_promoter(
            client_id=client_b.id, promoter_id=out_b["id"],
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "facilitator_already_active_elsewhere"

    # FM at A revokes → exclusivity releases. B can now invite + accept.
    await revoke_promoter(
        client_id=client_a.id, promoter_id=out_a["id"], db=db, current_user=sa,
    )
    await request_promoter(
        client_id=client_b.id, promoter_id=out_b["id"], db=db, current_user=sa,
    )
    out = await facilitator_accept_promoter_invitation(
        client_promoter_id=out_b["id"], db=db, current_user=facilitator,
    )
    assert out["is_promoter"] is True


# ── M5: PP eligibility now requires is_promoter=True ────────────────────────

async def _seed_pundit(db):
    user = await make_user(db, name="Pundit")
    profile = FarmPunditProfile(user_id=user.id, declaration_accepted=True)
    db.add(profile)
    await db.flush()
    return user, profile


async def _enrol_pundit(db, *, client, profile):
    cp = ClientFarmPundit(
        client_id=client.id, pundit_id=profile.id,
        role=PunditRole.PRIMARY, status="ACTIVE", round_robin_sequence=1,
    )
    db.add(cp)
    await db.flush()
    return cp


@requires_docker
@pytest.mark.asyncio
async def test_pp_toggle_blocked_when_facilitator_not_yet_promoter(db):
    """A Facilitator onboarded at this company but is_promoter=False
    cannot be marked as a Promoter-Pundit. Pre-Option-C, the gate
    only checked row-existence + status; the flag tightens it to
    require active Promoter status too."""
    client = await make_client(db)
    member = await make_user(db, name="FM")
    await make_client_user(db, user=member, client=client)
    user, profile = await _seed_pundit(db)
    cp = await _enrol_pundit(db, client=client, profile=profile)
    # Onboarded as Facilitator but NOT a Promoter — V1.1-shaped state.
    db.add(ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="FACILITATOR", status="ACTIVE",
        is_promoter=False, registered_by=member.id,
    ))
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await toggle_promoter_pundit(
            client_id=client.id, cp_id=cp.id,
            data={"is_promoter_pundit": True},
            db=db, current_user=member,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "promoter_pundit_requires_facilitator_promoter"


@requires_docker
@pytest.mark.asyncio
async def test_pp_toggle_succeeds_when_facilitator_is_promoter(db):
    """Same setup but is_promoter=True — succeeds. Confirms the
    backward-compatible path: existing data (which all has
    is_promoter=True after backfill) still works."""
    client = await make_client(db)
    member = await make_user(db, name="FM")
    await make_client_user(db, user=member, client=client)
    user, profile = await _seed_pundit(db)
    cp = await _enrol_pundit(db, client=client, profile=profile)
    db.add(ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="FACILITATOR", status="ACTIVE",
        is_promoter=True, registered_by=member.id,
    ))
    await db.commit()

    out = await toggle_promoter_pundit(
        client_id=client.id, cp_id=cp.id,
        data={"is_promoter_pundit": True},
        db=db, current_user=member,
    )
    assert out["is_promoter_pundit"] is True
