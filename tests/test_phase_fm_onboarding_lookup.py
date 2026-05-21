"""FM onboarding lookup — V1.1 Item 3 (2026-05-09).

`GET /admin/users/lookup-for-onboarding` drives the phone-only
Field Manager onboarding modal. Returns whether the phone matches
an existing User, whether they've self-claimed the right role,
whether they're already onboarded at this client, and (for
Dealers) the DealerProfile preview the FM verifies offline.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.clients.models import ClientPromoter
from app.modules.clients.router import lookup_user_for_onboarding
from app.modules.orders.models import DealerProfile
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_self_registered_user, make_user,
)


# ── States the modal renders against ───────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_lookup_phone_not_found(db):
    """Unknown phone → exists=False. Modal shows 'must self-register'."""
    client = await make_client(db)
    caller = await make_user(db, name="FM")
    await db.commit()

    out = await lookup_user_for_onboarding(
        phone="+919999999999",
        client_id=client.id,
        promoter_type="DEALER",
        db=db, current_user=caller,
    )
    assert out["exists"] is False
    assert out["user"] is None
    assert out["has_role"] is False
    assert out["already_onboarded"] is False
    assert out["dealer_profile"] is None


@requires_docker
@pytest.mark.asyncio
async def test_lookup_user_without_role(db):
    """Phone matches but the user hasn't claimed the role yet.
    has_role=False; modal shows 'has not claimed Dealer/Facilitator'."""
    client = await make_client(db)
    caller = await make_user(db, name="FM")
    plain = await make_user(db, name="Just Farmer")
    plain.phone = "+919900111200"
    await db.commit()

    out = await lookup_user_for_onboarding(
        phone="+919900111200",
        client_id=client.id,
        promoter_type="DEALER",
        db=db, current_user=caller,
    )
    assert out["exists"] is True
    assert out["has_role"] is False
    assert out["already_onboarded"] is False
    # No DealerProfile preview when the user hasn't claimed DEALER.
    assert out["dealer_profile"] is None


@requires_docker
@pytest.mark.asyncio
async def test_lookup_dealer_returns_profile_preview(db):
    """Dealer with a fleshed-out DealerProfile → modal can show
    shop name, address, GPS, sell categories, licence URLs for
    offline verification by the FM."""
    client = await make_client(db)
    caller = await make_user(db, name="FM")
    user = await make_self_registered_user(
        db, phone="+919900111201", role="DEALER", name="Shopkeeper",
    )
    db.add(DealerProfile(
        user_id=user.id,
        shop_name="Krishna Agros",
        shop_address="Bypass Road, Mysuru",
        sell_categories=["PESTICIDES", "SEEDS"],
        pesticide_licence_url="https://cdn/lic.pdf",
        shop_gps_lat=12.2958,
        shop_gps_lng=76.6394,
    ))
    await db.commit()

    out = await lookup_user_for_onboarding(
        phone="+919900111201",
        client_id=client.id,
        promoter_type="DEALER",
        db=db, current_user=caller,
    )
    assert out["exists"] is True
    assert out["has_role"] is True
    assert out["user"]["name"] == "Shopkeeper"
    assert out["dealer_profile"]["shop_name"] == "Krishna Agros"
    assert out["dealer_profile"]["pesticide_licence_url"] == "https://cdn/lic.pdf"
    assert out["dealer_profile"]["sell_categories"] == ["PESTICIDES", "SEEDS"]
    assert out["dealer_profile"]["shop_gps_lat"] == pytest.approx(12.2958)


@requires_docker
@pytest.mark.asyncio
async def test_lookup_facilitator_no_profile_preview(db):
    """Facilitators don't have a profile model — confirmed by user
    2026-05-08 ('plain rural youths, they don't need a special
    profile'). Lookup returns dealer_profile=None even when has_role
    is true."""
    client = await make_client(db)
    caller = await make_user(db, name="FM")
    await make_self_registered_user(
        db, phone="+919900111202", role="FACILITATOR", name="Helper",
    )
    await db.commit()

    out = await lookup_user_for_onboarding(
        phone="+919900111202",
        client_id=client.id,
        promoter_type="FACILITATOR",
        db=db, current_user=caller,
    )
    assert out["exists"] is True
    assert out["has_role"] is True
    assert out["dealer_profile"] is None
    assert out["user"]["name"] == "Helper"


@requires_docker
@pytest.mark.asyncio
async def test_lookup_already_onboarded_at_this_client(db):
    """Already-onboarded users get already_onboarded=True scoped to
    THIS client. Onboarding at a different client doesn't trigger it
    (privacy: never name other clients)."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    caller = await make_user(db, name="FM")
    user = await make_self_registered_user(
        db, phone="+919900111203", role="DEALER",
    )
    # Already onboarded at A.
    db.add(ClientPromoter(
        client_id=client_a.id, user_id=user.id,
        promoter_type="DEALER", status="ACTIVE",
        registered_by=caller.id,
    ))
    await db.commit()

    # Lookup against A: already_onboarded=True.
    out_a = await lookup_user_for_onboarding(
        phone="+919900111203",
        client_id=client_a.id,
        promoter_type="DEALER",
        db=db, current_user=caller,
    )
    assert out_a["already_onboarded"] is True

    # Lookup against B: already_onboarded=False (other client's
    # onboarding doesn't leak).
    out_b = await lookup_user_for_onboarding(
        phone="+919900111203",
        client_id=client_b.id,
        promoter_type="DEALER",
        db=db, current_user=caller,
    )
    assert out_b["already_onboarded"] is False


@requires_docker
@pytest.mark.asyncio
async def test_lookup_inactive_role_treated_as_no_role(db):
    """An INACTIVE UserRole should not satisfy has_role — same gate
    semantic as `register_promoter` enforces."""
    from app.modules.platform.models import RoleType, StatusEnum, UserRole

    client = await make_client(db)
    caller = await make_user(db, name="FM")
    user = await make_user(db, name="Was Dealer Once")
    user.phone = "+919900111204"
    db.add(UserRole(
        user_id=user.id, role_type=RoleType.DEALER, status=StatusEnum.INACTIVE,
    ))
    await db.commit()

    out = await lookup_user_for_onboarding(
        phone="+919900111204",
        client_id=client.id,
        promoter_type="DEALER",
        db=db, current_user=caller,
    )
    assert out["has_role"] is False


@requires_docker
@pytest.mark.asyncio
async def test_lookup_invalid_promoter_type_rejected(db):
    """Defensive 422 — promoter_type must be DEALER or FACILITATOR."""
    client = await make_client(db)
    caller = await make_user(db, name="FM")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await lookup_user_for_onboarding(
            phone="+919900000000",
            client_id=client.id,
            promoter_type="ASTRONAUT",
            db=db, current_user=caller,
        )
    assert ei.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_lookup_normalises_bare_10_digit_input(db):
    """Regression 2026-05-21: frontend sends what the FM typed
    (often bare 10 digits). User.phone is stored +91XXXXXXXXXX.
    Pre-fix the lookup did `User.phone == phone` and returned
    exists=False for every demonstrably-registered user."""
    client = await make_client(db)
    caller = await make_user(db, name="FM")
    target = await make_self_registered_user(
        db, phone="+919901399939", role="DEALER", name="Demo Dealer",
    )
    await db.commit()

    # Bare 10 digits — what the modal usually sends.
    out = await lookup_user_for_onboarding(
        phone="9901399939",
        client_id=client.id,
        promoter_type="DEALER",
        db=db, current_user=caller,
    )
    assert out["exists"] is True
    assert out["user"]["id"] == target.id

    # Messy formats — spaces / dashes / extra +91 — all resolve.
    for messy in ("+91 99013 99939", "99013-99939", "91-9901399939"):
        out2 = await lookup_user_for_onboarding(
            phone=messy, client_id=client.id, promoter_type="DEALER",
            db=db, current_user=caller,
        )
        assert out2["exists"] is True, f"failed on {messy!r}"
        assert out2["user"]["id"] == target.id

    # Too-short input short-circuits to not-found, no DB hit needed.
    short = await lookup_user_for_onboarding(
        phone="999", client_id=client.id, promoter_type="DEALER",
        db=db, current_user=caller,
    )
    assert short["exists"] is False
