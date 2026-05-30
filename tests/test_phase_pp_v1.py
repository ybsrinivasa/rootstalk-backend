"""Promoter-Pundit V1 — phantom-pundit Option A (2026-05-30).

Three things this batch is meant to make true end-to-end:

  A. CA designates a Facilitator-Promoter as PP without forcing
     them through /pundit/register. The toggle endpoint auto-
     provisions a FarmPunditProfile + ClientFarmPundit with
     `searchable=False` so the farmer never sees them in any
     pundit picker.

  B. Farmer's only path to choose a PP is by typing their phone.
     POST /pundit-preference accepts `{phone}` and verifies the
     User is an ACTIVE Promoter-Pundit at this sub's Client.
     Refuses with 422 user_not_found / 422 not_a_promoter_pundit
     when the typed number is wrong.

  C. When the CA revokes PP status or the F-P is dropped from the
     facilitator onboarding list, the saved override silently
     evaporates from GET /expert-setting — the farmer just sees an
     empty slot, queries fall through to the pool. Re-typing the
     stale number returns 422 not_a_promoter_pundit.

Plus the routing change: PPs are excluded from the round-robin
pool (pure-function test on `route_query`).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.clients.models import ClientPromoter, ClientStatus
from app.modules.farmpundit.models import (
    ClientFarmPundit, FarmPunditPreference, FarmPunditProfile, PunditRole,
)
from app.modules.farmpundit.router import (
    add_promoter_pundit,
    PromoterPunditAddRequest,
    set_pundit_preference,
)
from app.modules.subscriptions.models import (
    Subscription, SubscriptionStatus, SubscriptionType,
)
from app.modules.subscriptions.router import get_expert_setting
from app.services.bl12_query_routing import ExpertSlot, route_query
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_onboarded_facilitator,
    make_package, make_subscription, make_user,
)
from app.modules.clients.models import ClientUserRole


async def _fp_at_client(db, *, client, name="Raghu"):
    """Seed a Facilitator-Promoter (ACTIVE FACILITATOR + is_promoter=True
    + ACTIVE) at the given client. Returns the User."""
    user = await make_onboarded_facilitator(db, client=client, name=name)
    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == user.id,
            ClientPromoter.client_id == client.id,
        )
    )).scalar_one()
    cp.is_promoter = True
    await db.flush()
    return user


async def _ca_user_for(db, client):
    ca = await make_user(db, name="CA")
    await make_client_user(db, user=ca, client=client, role=ClientUserRole.CA)
    return ca


# ── A. CA designates F-P as PP without /pundit/register ────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_add_promoter_pundit_provisions_phantom_profile(db):
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    ca = await _ca_user_for(db, client)
    fp = await _fp_at_client(db, client=client)
    fp.phone = "+919800000060"
    await db.commit()

    out = await add_promoter_pundit(
        client_id=client.id,
        request=PromoterPunditAddRequest(phone=fp.phone),
        db=db, current_user=ca,
    )

    # FarmPunditProfile auto-created
    profile = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.user_id == fp.id)
    )).scalar_one()
    # ClientFarmPundit with searchable=False
    cfp = (await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.client_id == client.id,
            ClientFarmPundit.pundit_id == profile.id,
        )
    )).scalar_one()
    assert cfp.is_promoter_pundit is True
    assert cfp.searchable is False
    assert cfp.status == "ACTIVE"
    assert cfp.role == PunditRole.PANEL
    assert out["is_promoter_pundit"] is True
    assert out["searchable"] is False


@requires_docker
@pytest.mark.asyncio
async def test_add_promoter_pundit_refuses_non_fp(db):
    """§14.2: must already be an ACTIVE Facilitator-Promoter at this
    client. Plain users / dealers / non-promoted facilitators rejected."""
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    ca = await _ca_user_for(db, client)
    random_user = await make_user(db, name="Random")
    random_user.phone = "+919800000061"
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await add_promoter_pundit(
            client_id=client.id,
            request=PromoterPunditAddRequest(phone=random_user.phone),
            db=db, current_user=ca,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "promoter_pundit_requires_facilitator_promoter"


@requires_docker
@pytest.mark.asyncio
async def test_add_promoter_pundit_idempotent(db):
    """Re-adding an already-designated PP is a no-op (200/201 with the
    same row), not a duplicate-key crash."""
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    ca = await _ca_user_for(db, client)
    fp = await _fp_at_client(db, client=client)
    fp.phone = "+919800000062"
    await db.commit()

    a = await add_promoter_pundit(
        client_id=client.id,
        request=PromoterPunditAddRequest(phone=fp.phone),
        db=db, current_user=ca,
    )
    b = await add_promoter_pundit(
        client_id=client.id,
        request=PromoterPunditAddRequest(phone=fp.phone),
        db=db, current_user=ca,
    )
    assert a["id"] == b["id"]
    assert b["is_promoter_pundit"] is True


# ── B. Farmer phone-based set_pundit_preference ────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_set_preference_by_phone_resolves_to_pp_pundit_id(db):
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    ca = await _ca_user_for(db, client)
    fp = await _fp_at_client(db, client=client)
    fp.phone = "+919800000063"

    farmer = await make_user(db, name="Asha")
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.status = SubscriptionStatus.ACTIVE
    await db.commit()

    # Designate FP as PP first.
    await add_promoter_pundit(
        client_id=client.id,
        request=PromoterPunditAddRequest(phone=fp.phone),
        db=db, current_user=ca,
    )

    # Farmer types FP's phone — saved as preference, resolved to the
    # auto-provisioned FarmPunditProfile id.
    res = await set_pundit_preference(
        subscription_id=sub.id,
        data={"phone": fp.phone},
        db=db, current_user=farmer,
    )
    profile = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.user_id == fp.id)
    )).scalar_one()
    assert res["pundit_id"] == profile.id


@requires_docker
@pytest.mark.asyncio
async def test_set_preference_refuses_non_pp_phone(db):
    """Typing a number that resolves to a User who isn't an ACTIVE PP
    at this Client returns the actionable not_a_promoter_pundit code."""
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    farmer = await make_user(db, name="Bhavana")
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.status = SubscriptionStatus.ACTIVE
    other = await make_user(db, name="Some Other User")
    other.phone = "+919800000064"
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await set_pundit_preference(
            subscription_id=sub.id,
            data={"phone": other.phone},
            db=db, current_user=farmer,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "not_a_promoter_pundit"


@requires_docker
@pytest.mark.asyncio
async def test_set_preference_refuses_unregistered_phone(db):
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    farmer = await make_user(db, name="Chand")
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.status = SubscriptionStatus.ACTIVE
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await set_pundit_preference(
            subscription_id=sub.id,
            data={"phone": "+919999999999"},   # nobody has this
            db=db, current_user=farmer,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "user_not_found"


# ── C. Stale-PP override evaporates on read ────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_expert_setting_hides_stale_override_after_pp_revoked(db):
    """CA toggles PP off after the farmer saved them as preference. The
    farmer's GET /expert-setting just shows an empty slot — no
    proactive notification, the override silently drops."""
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    ca = await _ca_user_for(db, client)
    fp = await _fp_at_client(db, client=client)
    fp.phone = "+919800000065"

    farmer = await make_user(db, name="Devi")
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.status = SubscriptionStatus.ACTIVE
    await db.commit()

    await add_promoter_pundit(
        client_id=client.id,
        request=PromoterPunditAddRequest(phone=fp.phone),
        db=db, current_user=ca,
    )
    await set_pundit_preference(
        subscription_id=sub.id, data={"phone": fp.phone},
        db=db, current_user=farmer,
    )

    # Sanity: preference visible before revoke.
    pre = await get_expert_setting(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    assert pre["preferred_pundit"] is not None

    # Revoke PP status on the ClientFarmPundit row.
    cfp = (await db.execute(
        select(ClientFarmPundit).join(
            FarmPunditProfile, FarmPunditProfile.id == ClientFarmPundit.pundit_id,
        ).where(
            ClientFarmPundit.client_id == client.id,
            FarmPunditProfile.user_id == fp.id,
        )
    )).scalar_one()
    cfp.is_promoter_pundit = False
    await db.commit()

    # GET evaporates the override silently.
    post = await get_expert_setting(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    assert post["preferred_pundit"] is None


@requires_docker
@pytest.mark.asyncio
async def test_expert_setting_hides_stale_override_after_fp_stepdown(db):
    """ClientPromoter.is_promoter flipped off (F-P stepped down) — the
    PP row may still exist but the read-time eligibility check fails,
    so the override drops."""
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    ca = await _ca_user_for(db, client)
    fp = await _fp_at_client(db, client=client)
    fp.phone = "+919800000066"
    farmer = await make_user(db, name="Esha")
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.status = SubscriptionStatus.ACTIVE
    await db.commit()

    await add_promoter_pundit(
        client_id=client.id,
        request=PromoterPunditAddRequest(phone=fp.phone),
        db=db, current_user=ca,
    )
    await set_pundit_preference(
        subscription_id=sub.id, data={"phone": fp.phone},
        db=db, current_user=farmer,
    )

    # F-P steps down — is_promoter -> False
    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == fp.id,
            ClientPromoter.client_id == client.id,
        )
    )).scalar_one()
    cp.is_promoter = False
    await db.commit()

    res = await get_expert_setting(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    assert res["preferred_pundit"] is None


# ── Routing pure-function: PP excluded from round-robin ────────────────────

def test_route_query_excludes_pp_from_round_robin():
    """A PP who is also marked PRIMARY ACTIVE must NOT enter the
    round-robin pool. They receive queries only via the P1 (preference)
    path."""
    experts = [
        ExpertSlot(
            pundit_id="reg1", role="PRIMARY", status="ACTIVE",
            round_robin_sequence=1, is_promoter_pundit=False,
            onboarded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        ExpertSlot(
            pundit_id="pp1", role="PRIMARY", status="ACTIVE",
            round_robin_sequence=2, is_promoter_pundit=True,
            onboarded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        ),
        ExpertSlot(
            pundit_id="reg2", role="PRIMARY", status="ACTIVE",
            round_robin_sequence=3, is_promoter_pundit=False,
            onboarded_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        ),
    ]
    # No preference, no auto-PP → round-robin from the first PRIMARY.
    res = route_query(experts, None, None, None)
    assert res.reason == "ROUND_ROBIN"
    assert res.pundit_id == "reg1"

    # Walk the rotation — must skip pp1.
    res = route_query(experts, None, None, "reg1")
    assert res.pundit_id == "reg2"
    res = route_query(experts, None, None, "reg2")
    assert res.pundit_id == "reg1"   # wraps past pp1
