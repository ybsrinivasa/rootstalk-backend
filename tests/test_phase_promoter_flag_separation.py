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

from app.modules.clients.models import ClientPromoter
from app.modules.clients.router import register_promoter
from app.modules.farmpundit.models import (
    ClientFarmPundit, FarmPunditProfile, PunditRole,
)
from app.modules.farmpundit.router import toggle_promoter_pundit
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_user,
)


# ── Schema sanity ───────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_register_promoter_defaults_is_promoter_true(db):
    """Backward-compatible default. Pre-V1.1, the existing CA-portal
    register flow continues to produce rows that ARE Promoters. This
    locks in the migration backfill semantics."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await db.commit()

    out = await register_promoter(
        client_id=client.id,
        request={
            "phone": "+919900111100", "name": "Default Promoter",
            "promoter_type": "FACILITATOR", "territory_notes": None,
        },
        db=db, current_user=sa,
    )
    assert out["is_promoter"] is True


# ── M9: gate moves to is_promoter flag ──────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_facilitator_non_promoter_at_other_client_does_not_block(db):
    """A Facilitator who is onboarded at client A but NOT marked as
    a Promoter (is_promoter=False) doesn't block registration at
    client B. Only the Facilitator-PROMOTER combination is exclusive
    per spec §11.2 — the user's clarification on 2026-05-08."""
    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    user = await make_user(db, name="Mover")
    user.phone = "+919900111101"
    # Manually craft a non-promoter Facilitator row at A. The current
    # register_promoter endpoint defaults is_promoter=True, so we
    # bypass it to set up the V1.1-style state directly.
    db.add(ClientPromoter(
        client_id=client_a.id, user_id=user.id,
        promoter_type="FACILITATOR", status="ACTIVE",
        is_promoter=False, registered_by=sa.id,
    ))
    await db.commit()

    out = await register_promoter(
        client_id=client_b.id,
        request={
            "phone": "+919900111101", "name": "Mover",
            "promoter_type": "FACILITATOR", "territory_notes": None,
        },
        db=db, current_user=sa,
    )
    assert out["promoter_type"] == "FACILITATOR"


@requires_docker
@pytest.mark.asyncio
async def test_facilitator_promoter_at_other_client_still_blocks(db):
    """When the Facilitator at the other client IS a Promoter
    (is_promoter=True), registration here as a Promoter is still
    rejected — same spec §11.2 rule, just expressed via the flag."""
    sa = await make_user(db, name="SA")
    client_a = await make_client(db)
    client_b = await make_client(db)
    await db.commit()

    # First-time registration at A — defaults is_promoter=True.
    await register_promoter(
        client_id=client_a.id,
        request={
            "phone": "+919900111102", "name": "Locked",
            "promoter_type": "FACILITATOR", "territory_notes": None,
        },
        db=db, current_user=sa,
    )

    with pytest.raises(HTTPException) as ei:
        await register_promoter(
            client_id=client_b.id,
            request={
                "phone": "+919900111102", "name": "Locked",
                "promoter_type": "FACILITATOR", "territory_notes": None,
            },
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "facilitator_already_active_elsewhere"


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
