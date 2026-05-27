"""FarmPundit search-result polish (2026-05-27).

Three things added to /client/{cid}/pundit-search response:
1. `address` — state + district resolved from the User's profile
   (User.state_cosh_id / district_cosh_id), NOT from the FP
   register form's support_areas.
2. `invitation_status` ∈ {NONE, PENDING, ONBOARDED} per result,
   replacing the previous `already_onboarded` bool.
3. Dedupe on invite — second invite while one is PENDING gets 409
   `invitation_already_pending`. ACCEPTED ClientFarmPundit gets
   409 `pundit_already_onboarded`. REJECTED does NOT block.

Plus the new GET /client/{cid}/pundits/{cp_id}/profile endpoint
that surfaces a Pundit's full Cosh-resolved profile for the
CA's drill-down modal.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.farmpundit.models import (
    ClientFarmPundit, FarmPunditProfile, PunditInvitation, PunditRole,
)
from app.modules.farmpundit.router import (
    InviteRequest, get_company_pundit_profile, invite_pundit, search_pundits,
)
from app.modules.sync.models import CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import make_client, make_client_user, make_user


async def _ca_user_for(db, *, client):
    user = await make_user(db, name=f"CA-{client.short_name}")
    await make_client_user(db, user=user, client=client)
    return user


async def _seed_pundit(db, *, name, user_phone, state_cosh_id=None,
                       district_cosh_id=None, town=None):
    user = await make_user(db, name=name)
    user.phone = user_phone
    user.state_cosh_id = state_cosh_id
    user.district_cosh_id = district_cosh_id
    user.town = town
    profile = FarmPunditProfile(user_id=user.id, declaration_accepted=True)
    db.add(profile)
    await db.flush()
    return user, profile


async def _empty_search(db, *, client, ca):
    return await search_pundits(
        client_id=client.id,
        state_cosh_ids=[], expertise_domains=[], language_codes=[], crop_groups=[],
        farming_methods=[], cultivation_types=[],
        db=db, current_user=ca,
    )


# ── Address (from User profile) ─────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_search_result_address_resolves_state_and_district(db):
    client = await make_client(db)
    db.add(CoshCoreItem(
        cosh_id="state_ka", core_type="state_list",
        translations={"en": "Karnataka"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="dist_mysuru", core_type="district_list",
        translations={"en": "Mysuru"}, status="active",
    ))
    user, _ = await _seed_pundit(
        db, name="Located Expert", user_phone="+910000000001",
        state_cosh_id="state_ka", district_cosh_id="dist_mysuru",
        town="Mysore",
    )
    await db.commit()

    results = await _empty_search(db, client=client, ca=await _ca_user_for(db, client=client))
    row = next(r for r in results if r["name"] == "Located Expert")
    assert row["address"]["state"] == "Karnataka"
    assert row["address"]["district"] == "Mysuru"
    assert row["address"]["town"] == "Mysore"


@requires_docker
@pytest.mark.asyncio
async def test_search_result_address_handles_unset_user_address(db):
    """A Pundit who hasn't filled their User-side address still
    surfaces in the search — address fields just come back null."""
    client = await make_client(db)
    await _seed_pundit(
        db, name="No Address", user_phone="+910000000002",
    )
    await db.commit()

    results = await _empty_search(db, client=client, ca=await _ca_user_for(db, client=client))
    row = next(r for r in results if r["name"] == "No Address")
    assert row["address"]["state"] is None
    assert row["address"]["district"] is None
    assert row["address"]["town"] is None


# ── invitation_status ───────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_invitation_status_none_pending_onboarded(db):
    """invitation_status surfaces the (client, pundit) relationship
    state without requiring the CA to cross-reference two endpoints."""
    client = await make_client(db)
    _, p_none = await _seed_pundit(db, name="Untouched", user_phone="+911")
    _, p_pending = await _seed_pundit(db, name="Pending One", user_phone="+912")
    _, p_active = await _seed_pundit(db, name="Onboarded", user_phone="+913")

    db.add(PunditInvitation(
        client_id=client.id, pundit_id=p_pending.id,
        role=PunditRole.PRIMARY, status="PENDING",
    ))
    db.add(ClientFarmPundit(
        client_id=client.id, pundit_id=p_active.id,
        role=PunditRole.PRIMARY, status="ACTIVE", round_robin_sequence=1,
    ))
    await db.commit()

    results = await _empty_search(db, client=client, ca=await _ca_user_for(db, client=client))
    by_name = {r["name"]: r for r in results}
    assert by_name["Untouched"]["invitation_status"] == "NONE"
    assert by_name["Pending One"]["invitation_status"] == "PENDING"
    assert by_name["Onboarded"]["invitation_status"] == "ONBOARDED"


@requires_docker
@pytest.mark.asyncio
async def test_invitation_status_rejected_still_reads_as_none(db):
    """A REJECTED invitation does not block re-invite — the search
    card shows NONE so the CA can click Invite again."""
    client = await make_client(db)
    _, profile = await _seed_pundit(db, name="Declined Once", user_phone="+914")
    db.add(PunditInvitation(
        client_id=client.id, pundit_id=profile.id,
        role=PunditRole.PRIMARY, status="REJECTED",
        rejection_reason="busy this quarter",
    ))
    await db.commit()

    results = await _empty_search(db, client=client, ca=await _ca_user_for(db, client=client))
    row = next(r for r in results if r["name"] == "Declined Once")
    assert row["invitation_status"] == "NONE"


# ── Invite dedupe ──────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_invite_refuses_second_pending_for_same_pundit(db):
    client = await make_client(db)
    user, _ = await _seed_pundit(db, name="Duplicate Target", user_phone="+915")
    ca = await _ca_user_for(db, client=client)
    await db.commit()

    await invite_pundit(
        client_id=client.id,
        request=InviteRequest(pundit_user_id=user.id, role=PunditRole.PRIMARY),
        db=db, current_user=ca,
    )
    with pytest.raises(HTTPException) as exc:
        await invite_pundit(
            client_id=client.id,
            request=InviteRequest(pundit_user_id=user.id, role=PunditRole.PRIMARY),
            db=db, current_user=ca,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "invitation_already_pending"


@requires_docker
@pytest.mark.asyncio
async def test_invite_refuses_when_already_onboarded(db):
    client = await make_client(db)
    user, profile = await _seed_pundit(db, name="Already In", user_phone="+916")
    db.add(ClientFarmPundit(
        client_id=client.id, pundit_id=profile.id,
        role=PunditRole.PRIMARY, status="ACTIVE", round_robin_sequence=1,
    ))
    ca = await _ca_user_for(db, client=client)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await invite_pundit(
            client_id=client.id,
            request=InviteRequest(pundit_user_id=user.id, role=PunditRole.PRIMARY),
            db=db, current_user=ca,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "pundit_already_onboarded"


@requires_docker
@pytest.mark.asyncio
async def test_invite_allowed_after_rejection(db):
    """A REJECTED invitation does not block a fresh invite."""
    client = await make_client(db)
    user, profile = await _seed_pundit(db, name="Reject-and-retry", user_phone="+917")
    db.add(PunditInvitation(
        client_id=client.id, pundit_id=profile.id,
        role=PunditRole.PRIMARY, status="REJECTED",
        rejection_reason="not now",
    ))
    ca = await _ca_user_for(db, client=client)
    await db.commit()

    out = await invite_pundit(
        client_id=client.id,
        request=InviteRequest(pundit_user_id=user.id, role=PunditRole.PRIMARY),
        db=db, current_user=ca,
    )
    assert out["status"] == "PENDING"


# ── CA drill-down profile endpoint ──────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_company_pundit_profile_returns_full_resolved_shape(db):
    client = await make_client(db)
    db.add(CoshCoreItem(
        cosh_id="ed_mas", core_type="pundit_education",
        translations={"en": "Masters"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="state_ka", core_type="state_list",
        translations={"en": "Karnataka"}, status="active",
    ))
    user, profile = await _seed_pundit(
        db, name="Onboarded Expert", user_phone="+918",
        state_cosh_id="state_ka",
    )
    profile.education_cosh_id = "ed_mas"
    cp = ClientFarmPundit(
        client_id=client.id, pundit_id=profile.id,
        role=PunditRole.PRIMARY, status="ACTIVE", round_robin_sequence=1,
    )
    db.add(cp)
    await db.commit()
    await db.refresh(cp)

    out = await get_company_pundit_profile(
        client_id=client.id, cp_id=cp.id,
        db=db, current_user=await _ca_user_for(db, client=client),
    )
    assert out["name"] == "Onboarded Expert"
    assert out["education"] == {"cosh_id": "ed_mas", "name": "Masters"}
    assert out["address"]["state"] == "Karnataka"
    assert out["role"] == PunditRole.PRIMARY
    assert out["status"] == "ACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_company_pundit_profile_refuses_cross_client_id(db):
    """CA of client A cannot view a Pundit onboarded only at client B
    by guessing the cp_id."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    _, profile = await _seed_pundit(db, name="In B", user_phone="+919")
    cp_b = ClientFarmPundit(
        client_id=client_b.id, pundit_id=profile.id,
        role=PunditRole.PRIMARY, status="ACTIVE", round_robin_sequence=1,
    )
    db.add(cp_b)
    ca_a = await _ca_user_for(db, client=client_a)
    await db.commit()
    await db.refresh(cp_b)

    with pytest.raises(HTTPException) as exc:
        await get_company_pundit_profile(
            client_id=client_a.id, cp_id=cp_b.id,
            db=db, current_user=ca_a,
        )
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "pundit_not_in_client"
