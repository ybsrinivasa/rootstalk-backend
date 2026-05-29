"""Alerts E — F-P "subscriptions where I receive alerts" dashboard.

GET /promoter/me/alert-subscriptions returns the union of:
  - Explicit overrides (Subscription.extra_alert_user_id == me)
  - Auto-promoter defaults (ASSIGNED + promoter_user_id == me +
    no override + not opted out)

Opt-out and override-by-someone-else both correctly exclude.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.subscriptions.models import (
    Subscription, SubscriptionStatus, SubscriptionType,
)
from app.modules.subscriptions.router import my_alert_subscriptions
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)


async def _setup_assigned_sub(db, *, farmer_name="Farmer", promoter=None):
    farmer = await make_user(db, name=farmer_name)
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=package,
    )
    sub.subscription_type = SubscriptionType.ASSIGNED
    sub.status = SubscriptionStatus.ACTIVE
    if promoter is not None:
        sub.promoter_user_id = promoter.id
    await db.flush()
    return farmer, client, package, sub


# ── Auto-promoter ──────────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_lists_assigned_subs_where_i_am_the_promoter(db):
    fp = await make_user(db, name="F-P")
    farmer, _, _, sub = await _setup_assigned_sub(
        db, farmer_name="Asha", promoter=fp,
    )
    await db.commit()

    out = await my_alert_subscriptions(db=db, current_user=fp)
    assert len(out) == 1
    row = out[0]
    assert row["subscription_id"] == sub.id
    assert row["farmer_name"] == "Asha"
    assert row["source"] == "auto_promoter"


@requires_docker
@pytest.mark.asyncio
async def test_excludes_self_subs(db):
    """SELF subs have no promoter — they never appear in the F-P
    dashboard regardless of any other state."""
    fp = await make_user(db, name="F-P")
    farmer = await make_user(db, name="Self-subscriber")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.subscription_type = SubscriptionType.SELF
    sub.status = SubscriptionStatus.ACTIVE
    await db.commit()

    out = await my_alert_subscriptions(db=db, current_user=fp)
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_auto_promoter_excluded_when_override_set(db):
    """If the farmer typed someone else's number, the auto-promoter
    no longer receives alerts and the row should drop out."""
    fp = await make_user(db, name="F-P")
    other_dealer = await make_user(db, name="Other Dealer")
    farmer, _, _, sub = await _setup_assigned_sub(db, promoter=fp)
    sub.extra_alert_user_id = other_dealer.id
    await db.commit()

    out_for_fp = await my_alert_subscriptions(db=db, current_user=fp)
    assert out_for_fp == []

    out_for_other = await my_alert_subscriptions(db=db, current_user=other_dealer)
    assert len(out_for_other) == 1
    assert out_for_other[0]["source"] == "override"


@requires_docker
@pytest.mark.asyncio
async def test_excluded_when_farmer_opted_out(db):
    fp = await make_user(db, name="F-P")
    farmer, _, _, sub = await _setup_assigned_sub(db, promoter=fp)
    sub.alerts_extra_disabled = True
    await db.commit()

    out = await my_alert_subscriptions(db=db, current_user=fp)
    assert out == []


# ── Explicit override ──────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_lists_self_sub_where_farmer_added_me_explicitly(db):
    """The PWA flow most farmers actually walk: SELF subscription,
    farmer types a Dealer/Facilitator's number into the alerts sheet,
    that recipient sees the sub in their F-P dashboard."""
    dealer = await make_user(db, name="Dealer")
    farmer = await make_user(db, name="Self Farmer")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.subscription_type = SubscriptionType.SELF
    sub.status = SubscriptionStatus.ACTIVE
    sub.extra_alert_user_id = dealer.id
    await db.commit()

    out = await my_alert_subscriptions(db=db, current_user=dealer)
    assert len(out) == 1
    assert out[0]["source"] == "override"


# ── Ordering + status gate ─────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_only_active_subs_listed(db):
    """LAPSED / CANCELLED / WAITLISTED subs don't appear — alerts
    don't fire on them, so showing them in the dashboard would be
    misleading."""
    fp = await make_user(db, name="F-P")
    farmer, _, _, sub = await _setup_assigned_sub(db, promoter=fp)
    sub.status = SubscriptionStatus.CANCELLED
    await db.commit()

    out = await my_alert_subscriptions(db=db, current_user=fp)
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_ordering_overrides_first_then_alpha_by_farmer_name(db):
    """Override rows surface first (more intentional pairing); within
    each group, alphabetical by farmer name."""
    fp = await make_user(db, name="F-P")

    # Auto-promoter sub, farmer name = "Zacharias"
    _, _, _, sub_z = await _setup_assigned_sub(
        db, farmer_name="Zacharias", promoter=fp,
    )
    # Override sub, farmer name = "Asha"
    farmer_a = await make_user(db, name="Asha")
    client = await make_client(db)
    package = await make_package(db, client)
    sub_a = await make_subscription(
        db, farmer=farmer_a, client=client, package=package,
    )
    sub_a.subscription_type = SubscriptionType.SELF
    sub_a.status = SubscriptionStatus.ACTIVE
    sub_a.extra_alert_user_id = fp.id
    await db.commit()

    out = await my_alert_subscriptions(db=db, current_user=fp)
    assert [r["farmer_name"] for r in out] == ["Asha", "Zacharias"]
    assert [r["source"] for r in out] == ["override", "auto_promoter"]
