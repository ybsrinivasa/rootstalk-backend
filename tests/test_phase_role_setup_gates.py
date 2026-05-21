"""Role-setup gates — dealer profile completeness + facilitator declaration.

The PWA refuses to load /dealer/home until GET /dealer/profile
reports `is_profile_complete: true`, and refuses to load
/facilitator/home until `users.facilitator_declared_at` is set.
These tests pin the backend half of that contract.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.auth.router import confirm_facilitator_declaration
from app.modules.orders.router import _dealer_profile_complete, get_dealer_profile
from app.modules.orders.models import DealerProfile
from app.modules.platform.models import RoleType, StatusEnum, UserRole
from tests.conftest import requires_docker
from tests.factories import make_user


@requires_docker
@pytest.mark.asyncio
async def test_dealer_profile_complete_requires_all_seven_fields(db):
    user = await make_user(db, name="Dealer C")
    profile = DealerProfile(user_id=user.id)
    db.add(profile)
    await db.commit()

    out = await get_dealer_profile(db=db, current_user=user)
    assert out["is_profile_complete"] is False

    # Fill all required fields except shop_photo_url first — should
    # still be incomplete.
    profile.shop_name = "Test Shop"
    profile.shop_address = "1 Test Lane"
    profile.sell_categories = ["SEEDS"]
    profile.shop_gps_lat = 12.97
    profile.shop_gps_lng = 77.59
    profile.shop_registration_url = "https://example.com/cert.pdf"
    await db.commit()
    assert _dealer_profile_complete(profile) is False

    profile.shop_photo_url = "https://example.com/shop.jpg"
    await db.commit()
    assert _dealer_profile_complete(profile) is True

    out = await get_dealer_profile(db=db, current_user=user)
    assert out["is_profile_complete"] is True


@requires_docker
@pytest.mark.asyncio
async def test_dealer_profile_incomplete_when_no_row(db):
    """A user who never opened /dealer/profile has no DealerProfile
    row at all — GET still returns 200 with is_profile_complete=false
    so the PWA can route them to setup without an error path."""
    user = await make_user(db, name="Dealer Z")
    await db.commit()

    out = await get_dealer_profile(db=db, current_user=user)
    assert out["is_profile_complete"] is False
    assert out["shop_name"] is None


@requires_docker
@pytest.mark.asyncio
async def test_facilitator_declaration_stamps_timestamp(db):
    user = await make_user(db, name="Facilitator A")
    db.add(UserRole(user_id=user.id, role_type=RoleType.FACILITATOR,
                    status=StatusEnum.ACTIVE))
    await db.commit()
    assert user.facilitator_declared_at is None

    out = await confirm_facilitator_declaration(db=db, current_user=user)
    assert out["facilitator_declared_at"] is not None
    await db.refresh(user)
    assert user.facilitator_declared_at is not None


@requires_docker
@pytest.mark.asyncio
async def test_facilitator_declaration_idempotent(db):
    """Second confirmation preserves the original timestamp — a
    noisy onClick must not reset the moment of declaration."""
    user = await make_user(db, name="Facilitator B")
    db.add(UserRole(user_id=user.id, role_type=RoleType.FACILITATOR,
                    status=StatusEnum.ACTIVE))
    await db.commit()

    first = await confirm_facilitator_declaration(db=db, current_user=user)
    second = await confirm_facilitator_declaration(db=db, current_user=user)
    assert first["facilitator_declared_at"] == second["facilitator_declared_at"]


@requires_docker
@pytest.mark.asyncio
async def test_facilitator_declaration_refused_without_role(db):
    """Caller must already hold the FACILITATOR UserRole. PWA should
    route them through /become-facilitator first if they don't."""
    user = await make_user(db, name="Stranger")
    await db.commit()  # NO FACILITATOR role granted

    with pytest.raises(HTTPException) as exc:
        await confirm_facilitator_declaration(db=db, current_user=user)
    assert exc.value.status_code == 409
    assert exc.value.detail.get("code") == "facilitator_role_not_claimed"
