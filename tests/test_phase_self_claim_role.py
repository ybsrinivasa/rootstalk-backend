"""Self-claim PWA role — V1.1 Items 1+2.

Per the five-ecosystem architecture (see five_ecosystems memory):
a User self-registers in their ecosystem first, then a company
recognises them. This endpoint adds the UserRole row that flips
the role on for the PWA so the user can fill in their profile and
become available for company onboarding.

Self-claimable roles: DEALER, FACILITATOR.
NOT self-claimable: FARMER (implicit), FARM_PUNDIT (own flow at
/pundit/profile), CONTENT_MANAGER / RELATIONSHIP_MANAGER /
BUSINESS_MANAGER (Neytiri-side admin-assigned).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.auth.router import claim_role
from app.modules.platform.models import RoleType, UserRole
from tests.conftest import requires_docker
from tests.factories import make_user


@requires_docker
@pytest.mark.asyncio
async def test_claim_dealer_creates_user_role(db):
    user = await make_user(db, name="Aspiring Dealer")
    await db.commit()

    out = await claim_role(
        data={"role": "DEALER"}, db=db, current_user=user,
    )
    assert out["role"] == "DEALER"
    assert out["status"] == "ACTIVE"

    row = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_type == RoleType.DEALER,
        )
    )).scalar_one_or_none()
    assert row is not None


@requires_docker
@pytest.mark.asyncio
async def test_claim_facilitator_creates_user_role(db):
    user = await make_user(db, name="Aspiring Facilitator")
    await db.commit()

    out = await claim_role(
        data={"role": "FACILITATOR"}, db=db, current_user=user,
    )
    assert out["role"] == "FACILITATOR"


@requires_docker
@pytest.mark.asyncio
async def test_claim_is_idempotent(db):
    """Calling twice doesn't fail and doesn't create duplicates —
    the unique constraint on (user_id, role_type) would otherwise
    raise IntegrityError. Pages can fire-and-forget on landing."""
    user = await make_user(db, name="Repeat Claimer")
    await db.commit()

    await claim_role(data={"role": "DEALER"}, db=db, current_user=user)
    await claim_role(data={"role": "DEALER"}, db=db, current_user=user)  # second call must not error

    rows = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_type == RoleType.DEALER,
        )
    )).scalars().all()
    assert len(rows) == 1


@requires_docker
@pytest.mark.asyncio
async def test_claim_lowercase_role_is_normalised(db):
    """Pages may send lowercase by accident. The endpoint uppercases
    before lookup so the contract is forgiving."""
    user = await make_user(db, name="Loose Caller")
    await db.commit()

    out = await claim_role(
        data={"role": "dealer"}, db=db, current_user=user,
    )
    assert out["role"] == "DEALER"


@requires_docker
@pytest.mark.asyncio
async def test_claim_farmer_rejected(db):
    """FARMER is implicit on every PWA user — claiming it via this
    endpoint is a category error the system catches with a clear
    structured 422 explaining why."""
    user = await make_user(db, name="Confused User")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await claim_role(
            data={"role": "FARMER"}, db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "role_not_self_claimable"


@requires_docker
@pytest.mark.asyncio
async def test_claim_farm_pundit_rejected(db):
    """FARM_PUNDIT has its own richer registration flow — claiming it
    via this endpoint would skip the qualification fields."""
    user = await make_user(db, name="Aspiring Pundit")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await claim_role(
            data={"role": "FARM_PUNDIT"}, db=db, current_user=user,
        )
    assert ei.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_claim_internal_role_rejected(db):
    """Neytiri-side internal roles (CM / RM / BM) are admin-assigned
    via the SA portal, not user-self-claimable."""
    user = await make_user(db, name="Aspiring CM")
    await db.commit()

    for role in ("CONTENT_MANAGER", "RELATIONSHIP_MANAGER", "BUSINESS_MANAGER"):
        with pytest.raises(HTTPException) as ei:
            await claim_role(
                data={"role": role}, db=db, current_user=user,
            )
        assert ei.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_claim_unknown_role_rejected(db):
    user = await make_user(db, name="Typo Maker")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await claim_role(
            data={"role": "ASTRONAUT"}, db=db, current_user=user,
        )
    assert ei.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_claim_missing_role_rejected(db):
    user = await make_user(db, name="Empty Caller")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await claim_role(data={}, db=db, current_user=user)
    assert ei.value.status_code == 422
