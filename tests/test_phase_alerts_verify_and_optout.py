"""POST /alert-preferences — server-side verify + opt-out (2026-05-29).

Covers Alerts A+B+C end-to-end against a real DB:
  A: extra_alert_user_id persisted from a verified phone, denormalised
     phone/name match the resolved User row.
  B: refuse 422 user_not_found when the phone doesn't belong to any
     User; refuse 422 not_a_dealer_or_facilitator when it belongs to
     a User without the right role.
  C: disabled=true clears every override and sets the opt-out flag;
     GET surfaces source='disabled'.

Also confirms the end-to-end pipe — after a save, the value the
alerts task would read (`Subscription.extra_alert_user_id`,
`alerts_extra_disabled`) matches what `GET` reports back.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.platform.models import RoleType, StatusEnum, User, UserRole
from app.modules.subscriptions.models import Subscription
from app.modules.subscriptions.router import (
    get_alert_preferences, set_alert_preferences,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)


async def _sub_owned_by(db, farmer, *, promoter_id: str | None = None):
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=package,
    )
    if promoter_id is not None:
        sub.promoter_user_id = promoter_id
        # ASSIGNED type so GET / resolver use the auto_promoter path
        from app.modules.subscriptions.models import SubscriptionType
        sub.subscription_type = SubscriptionType.ASSIGNED
    await db.flush()
    return sub


async def _make_dealer(db, *, phone: str) -> User:
    u = await make_user(db, name="Dealer Rao")
    u.phone = phone
    db.add(UserRole(
        user_id=u.id, role_type=RoleType.DEALER, status=StatusEnum.ACTIVE,
    ))
    await db.flush()
    return u


# ── A: verified phone resolves to a Dealer ─────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_save_verified_dealer_phone_persists_user_id(db):
    farmer = await make_user(db, name="Asha")
    farmer.phone = "+919800000040"
    sub = await _sub_owned_by(db, farmer)
    dealer = await _make_dealer(db, phone="+919800000041")
    await db.commit()

    await set_alert_preferences(
        subscription_id=sub.id,
        data={"extra_phone": dealer.phone},
        db=db, current_user=farmer,
    )

    refreshed = (await db.execute(
        select(Subscription).where(Subscription.id == sub.id)
    )).scalar_one()
    assert refreshed.extra_alert_user_id == dealer.id
    assert refreshed.extra_alert_phone == dealer.phone
    assert refreshed.extra_alert_name == dealer.name
    assert refreshed.alerts_extra_disabled is False


@requires_docker
@pytest.mark.asyncio
async def test_get_returns_override_source_with_resolved_name(db):
    farmer = await make_user(db, name="Asha")
    farmer.phone = "+919800000042"
    sub = await _sub_owned_by(db, farmer)
    dealer = await _make_dealer(db, phone="+919800000043")
    await db.commit()

    await set_alert_preferences(
        subscription_id=sub.id,
        data={"extra_phone": dealer.phone},
        db=db, current_user=farmer,
    )
    res = await get_alert_preferences(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    assert res["source"] == "override"
    assert res["extra_phone"] == dealer.phone
    assert res["extra_name"] == dealer.name
    assert res["disabled"] is False


# ── B: 422 paths ───────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_save_rejects_phone_not_registered_with_user(db):
    farmer = await make_user(db, name="Bhargav")
    farmer.phone = "+919800000044"
    sub = await _sub_owned_by(db, farmer)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await set_alert_preferences(
            subscription_id=sub.id,
            data={"extra_phone": "+919800000099"},  # nobody has this
            db=db, current_user=farmer,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "user_not_found"


@requires_docker
@pytest.mark.asyncio
async def test_save_rejects_non_dealer_non_facilitator(db):
    farmer = await make_user(db, name="Chitra")
    farmer.phone = "+919800000045"
    sub = await _sub_owned_by(db, farmer)
    other = await make_user(db, name="Random Farmer")
    other.phone = "+919800000046"   # exists but no DEALER/FACILITATOR role
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await set_alert_preferences(
            subscription_id=sub.id,
            data={"extra_phone": other.phone},
            db=db, current_user=farmer,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "not_a_dealer_or_facilitator"


# ── C: explicit opt-out ────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_disabled_flag_clears_overrides_and_blocks_default(db):
    """Even on an ASSIGNED sub with a promoter, `disabled=true` returns
    source='disabled' and persists `alerts_extra_disabled=True`."""
    farmer = await make_user(db, name="Devi")
    farmer.phone = "+919800000047"
    promoter = await make_user(db, name="Promoter")
    promoter.phone = "+919800000048"
    sub = await _sub_owned_by(db, farmer, promoter_id=promoter.id)
    dealer = await _make_dealer(db, phone="+919800000049")
    await db.commit()

    # Set an override first, then opt out — opt-out must clear both.
    await set_alert_preferences(
        subscription_id=sub.id,
        data={"extra_phone": dealer.phone},
        db=db, current_user=farmer,
    )
    await set_alert_preferences(
        subscription_id=sub.id,
        data={"disabled": True},
        db=db, current_user=farmer,
    )

    refreshed = (await db.execute(
        select(Subscription).where(Subscription.id == sub.id)
    )).scalar_one()
    assert refreshed.alerts_extra_disabled is True
    assert refreshed.extra_alert_user_id is None
    assert refreshed.extra_alert_phone is None

    res = await get_alert_preferences(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    assert res["source"] == "disabled"
    assert res["disabled"] is True
    assert res["extra_phone"] is None


@requires_docker
@pytest.mark.asyncio
async def test_clear_phone_restores_default_auto_promoter(db):
    """Saving with an empty phone (no disabled flag) clears the override
    and lets the auto-promoter default take over again."""
    farmer = await make_user(db, name="Ekambar")
    farmer.phone = "+919800000050"
    promoter = await make_user(db, name="Promoter")
    promoter.phone = "+919800000051"
    sub = await _sub_owned_by(db, farmer, promoter_id=promoter.id)
    dealer = await _make_dealer(db, phone="+919800000052")
    await db.commit()

    await set_alert_preferences(
        subscription_id=sub.id,
        data={"extra_phone": dealer.phone},
        db=db, current_user=farmer,
    )
    # Clear without opting out.
    await set_alert_preferences(
        subscription_id=sub.id,
        data={"extra_phone": ""},
        db=db, current_user=farmer,
    )

    refreshed = (await db.execute(
        select(Subscription).where(Subscription.id == sub.id)
    )).scalar_one()
    assert refreshed.extra_alert_user_id is None
    assert refreshed.alerts_extra_disabled is False

    res = await get_alert_preferences(
        subscription_id=sub.id, db=db, current_user=farmer,
    )
    assert res["source"] == "auto_promoter"
    assert res["extra_phone"] == promoter.phone
