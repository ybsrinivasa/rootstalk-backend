"""Enterprise License module (2026-05-30).

Covers the four design pillars in one file:

  A. is_enterprise_licensed correctness — covers exact-day boundaries
     and status filtering.
  B. EL bypass on the four touch points: get_promoter_balance,
     get_company_unallocated_balance, consume_for_assignment,
     refund_to_promoter — all no-op / return-sentinel when EL active.
  C. SA endpoints: grant-subscriptions, grant-EL (incl. 409 on dup +
     422 on date-misorder), revoke, mgmt-view shape.
  D. Daily Celery sweep: closure flips lifecycle + Client.INACTIVE on
     to_date; reminders fire on the 5 days-out triggers.

Plus end-to-end: F-P initiate during EL doesn't touch kitty.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.clients.models import (
    Client, ClientPromoter, ClientStatus, ClientUserRole,
)
from app.modules.clients.router import (
    EnterpriseLicenseRequest, SubscriptionGrantRequest,
    sa_grant_enterprise_license, sa_grant_subscriptions,
    sa_revoke_enterprise_license, sa_subscription_mgmt_view,
)
from app.modules.subscriptions.models import (
    EnterpriseLicense, SubscriptionPool, SubscriptionStatus,
)
from app.modules.subscriptions.promoter_allocation_models import (
    PromoterAllocation,
)
from app.modules.subscriptions.router import (
    PromoterAssignRequest, initiate_assignment, my_kitty,
    my_promoter_allocations,
)
from app.modules.platform.models import User
from app.services.promoter_pool import (
    ENTERPRISE_UNLIMITED_BALANCE,
    consume_for_assignment,
    get_company_unallocated_balance,
    get_promoter_balance,
    is_enterprise_licensed,
    refund_to_promoter,
)
from app.tasks.enterprise_license_lifecycle import (
    REMINDER_DAYS, _sweep_with_session,
)
from app.config import settings
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_onboarded_facilitator, make_package,
    make_user,
)


async def _make_sa_user(db) -> User:
    """Mint a User whose email matches settings.sa_email so _require_sa
    passes. Memory: SA is the single canonical mailbox per instance."""
    u = await make_user(db, name="SA")
    u.email = settings.sa_email
    await db.flush()
    return u


async def _make_active_el_client(
    db, *, days_remaining: int = 30,
) -> tuple[Client, EnterpriseLicense]:
    """Seed a Client with one ACTIVE EnterpriseLicense window."""
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    client.ca_email = "ca@example.com"
    today = date.today()
    lic = EnterpriseLicense(
        client_id=client.id,
        from_date=today - timedelta(days=1),
        to_date=today + timedelta(days=days_remaining),
        status="ACTIVE",
    )
    db.add(lic)
    await db.flush()
    return client, lic


# ── A. is_enterprise_licensed ─────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_is_licensed_true_when_today_is_inside_window(db):
    client, _ = await _make_active_el_client(db, days_remaining=10)
    await db.commit()
    assert await is_enterprise_licensed(db, client.id) is True


@requires_docker
@pytest.mark.asyncio
async def test_is_licensed_false_outside_window(db):
    client = await make_client(db)
    today = date.today()
    db.add(EnterpriseLicense(
        client_id=client.id,
        from_date=today + timedelta(days=10),
        to_date=today + timedelta(days=40),
        status="ACTIVE",
    ))
    await db.commit()
    assert await is_enterprise_licensed(db, client.id) is False


@requires_docker
@pytest.mark.asyncio
async def test_is_licensed_false_when_revoked(db):
    client = await make_client(db)
    today = date.today()
    db.add(EnterpriseLicense(
        client_id=client.id,
        from_date=today - timedelta(days=1),
        to_date=today + timedelta(days=10),
        status="REVOKED",
    ))
    await db.commit()
    assert await is_enterprise_licensed(db, client.id) is False


# ── B. Touch-point bypass ────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_balance_helpers_return_unlimited_when_el_active(db):
    client, _ = await _make_active_el_client(db)
    user = await make_user(db)
    await db.commit()

    assert await get_promoter_balance(db, client.id, user.id) == ENTERPRISE_UNLIMITED_BALANCE
    assert await get_company_unallocated_balance(db, client.id) == ENTERPRISE_UNLIMITED_BALANCE


@requires_docker
@pytest.mark.asyncio
async def test_consume_is_noop_when_el_active(db):
    """Promoter row stays untouched — the assign flow can call this
    safely and the underlying kitty rows aren't churned."""
    client, _ = await _make_active_el_client(db)
    user = await make_user(db)
    db.add(PromoterAllocation(
        client_id=client.id, promoter_user_id=user.id,
        units_balance=5, allocated_total=5,
        reclaimed_total=0, consumed_total=0, refunded_total=0,
    ))
    await db.commit()

    result = await consume_for_assignment(
        db, client_id=client.id, promoter_user_id=user.id,
    )
    assert result is None

    row = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.client_id == client.id,
            PromoterAllocation.promoter_user_id == user.id,
        )
    )).scalar_one()
    assert row.units_balance == 5
    assert row.consumed_total == 0


@requires_docker
@pytest.mark.asyncio
async def test_refund_is_noop_when_el_active(db):
    client, _ = await _make_active_el_client(db)
    user = await make_user(db)
    db.add(PromoterAllocation(
        client_id=client.id, promoter_user_id=user.id,
        units_balance=3, allocated_total=5,
        reclaimed_total=0, consumed_total=2, refunded_total=0,
    ))
    await db.commit()

    result = await refund_to_promoter(
        db, client_id=client.id, promoter_user_id=user.id,
    )
    assert result is None
    row = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.client_id == client.id,
            PromoterAllocation.promoter_user_id == user.id,
        )
    )).scalar_one()
    assert row.refunded_total == 0


# ── End-to-end: F-P initiate during EL ────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_fp_initiate_succeeds_with_no_allocation_when_el(db):
    """The whole point of EL: F-P can assign without ever having had
    an allocation row. Subscription + Assignment still get created
    correctly; just no kitty side-effect."""
    client, lic = await _make_active_el_client(db, days_remaining=60)
    fac = await make_onboarded_facilitator(db, client=client)
    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == fac.id,
            ClientPromoter.client_id == client.id,
        )
    )).scalar_one()
    cp.is_promoter = True

    farmer = await make_user(db, name="Farmer Eswari")
    farmer.phone = "+919800000700"
    pkg = await make_package(db, client, crop_cosh_id="crop:onion")
    await db.commit()

    out = await initiate_assignment(
        request=PromoterAssignRequest(
            farmer_phone=farmer.phone,
            package_id=pkg.id,
            promoter_type="FACILITATOR",
            farm_area_acres=1.0,
        ),
        db=db,
        current_user=fac,
    )
    assert "subscription_id" in out
    # No PromoterAllocation row was created or touched.
    alloc = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.client_id == client.id,
            PromoterAllocation.promoter_user_id == fac.id,
        )
    )).scalar_one_or_none()
    assert alloc is None


@requires_docker
@pytest.mark.asyncio
async def test_my_kitty_surfaces_unlimited_flag_when_el(db):
    client, lic = await _make_active_el_client(db, days_remaining=45)
    fac = await make_onboarded_facilitator(db, client=client)
    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == fac.id,
            ClientPromoter.client_id == client.id,
        )
    )).scalar_one()
    cp.is_promoter = True
    await db.commit()

    out = await my_kitty(db=db, current_user=fac)
    assert out["units_balance"] == ENTERPRISE_UNLIMITED_BALANCE
    assert out["unlimited"] is True
    assert out["enterprise_to_date"] == lic.to_date


# ── C. SA endpoints ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_sa_grant_subscriptions_writes_invoice_note(db):
    sa = await _make_sa_user(db)
    client = await make_client(db)
    await db.commit()

    out = await sa_grant_subscriptions(
        client_id=client.id,
        request=SubscriptionGrantRequest(units=5000, note="INV-2026-0123"),
        db=db, current_user=sa,
    )
    assert out["units"] == 5000
    assert out["note"] == "INV-2026-0123"

    pool = (await db.execute(
        select(SubscriptionPool).where(SubscriptionPool.client_id == client.id)
    )).scalar_one()
    assert pool.razorpay_payment_id is None
    assert pool.purchased_by_user_id == sa.id


@requires_docker
@pytest.mark.asyncio
async def test_sa_grant_subscriptions_supports_6_digit_units(db):
    """User explicitly said 6-digit numbers should work."""
    sa = await _make_sa_user(db)
    client = await make_client(db)
    await db.commit()
    out = await sa_grant_subscriptions(
        client_id=client.id,
        request=SubscriptionGrantRequest(units=500000),
        db=db, current_user=sa,
    )
    assert out["units"] == 500000


@requires_docker
@pytest.mark.asyncio
async def test_sa_grant_enterprise_license_succeeds(db):
    sa = await _make_sa_user(db)
    client = await make_client(db)
    await db.commit()
    today = date.today()

    out = await sa_grant_enterprise_license(
        client_id=client.id,
        request=EnterpriseLicenseRequest(
            from_date=today, to_date=today + timedelta(days=365),
            note="MOU 2026-05",
        ),
        db=db, current_user=sa,
    )
    assert out["status"] == "ACTIVE"
    assert out["note"] == "MOU 2026-05"


@requires_docker
@pytest.mark.asyncio
async def test_sa_grant_el_409_when_active_one_exists(db):
    sa = await _make_sa_user(db)
    client, _ = await _make_active_el_client(db)
    await db.commit()
    today = date.today()

    with pytest.raises(HTTPException) as ei:
        await sa_grant_enterprise_license(
            client_id=client.id,
            request=EnterpriseLicenseRequest(
                from_date=today + timedelta(days=10),
                to_date=today + timedelta(days=100),
            ),
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "active_license_exists"


@requires_docker
@pytest.mark.asyncio
async def test_sa_grant_el_422_when_dates_misordered(db):
    sa = await _make_sa_user(db)
    client = await make_client(db)
    await db.commit()
    today = date.today()

    with pytest.raises(HTTPException) as ei:
        await sa_grant_enterprise_license(
            client_id=client.id,
            request=EnterpriseLicenseRequest(
                from_date=today + timedelta(days=10),
                to_date=today + timedelta(days=5),
            ),
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "to_date_must_follow_from_date"


@requires_docker
@pytest.mark.asyncio
async def test_sa_revoke_el_flips_inactive_immediately(db):
    sa = await _make_sa_user(db)
    client, lic = await _make_active_el_client(db, days_remaining=90)
    await db.commit()

    out = await sa_revoke_enterprise_license(
        client_id=client.id, license_id=lic.id,
        db=db, current_user=sa,
    )
    assert out["status"] == "REVOKED"

    await db.refresh(client)
    assert client.status == ClientStatus.INACTIVE


@requires_docker
@pytest.mark.asyncio
async def test_sa_mgmt_view_returns_full_shape(db):
    sa = await _make_sa_user(db)
    client, lic = await _make_active_el_client(db, days_remaining=20)
    db.add(SubscriptionPool(
        client_id=client.id, units_purchased=200, units_consumed=0,
        purchased_by_user_id=sa.id, note="INV-2026-0099",
    ))
    await db.commit()

    out = await sa_subscription_mgmt_view(
        client_id=client.id, db=db, current_user=sa,
    )
    assert out["pool_totals"]["purchased_total"] == 200
    assert out["pool_totals"]["unlimited"] is True
    assert out["active_license"] is not None
    assert out["active_license"]["days_remaining"] == 20
    assert len(out["grants_history"]) == 1
    assert out["grants_history"][0]["source"] == "SA_GRANT"
    assert out["grants_history"][0]["note"] == "INV-2026-0099"
    assert len(out["licenses_history"]) == 1


# ── D. Daily Celery sweep ───────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_sweep_closes_license_on_to_date_and_flips_client(db, monkeypatch):
    sent: list[tuple[str, str]] = []

    def _stub(to: str, subject: str, html: str, plain: str) -> bool:
        sent.append((to, subject))
        return True

    monkeypatch.setattr(
        "app.tasks.enterprise_license_lifecycle._send_email", _stub,
    )

    client, lic = await _make_active_el_client(db, days_remaining=0)
    await db.commit()

    counts = await _sweep_with_session(db)
    assert counts["closures"] == 1
    assert counts["reminders"] == 0

    await db.refresh(lic)
    await db.refresh(client)
    assert lic.status == "EXPIRED"
    assert client.status == ClientStatus.INACTIVE

    # Both CA + SA mailboxes got the closure email.
    recipients = {r for r, _ in sent}
    assert client.ca_email in recipients
    assert settings.sa_email in recipients


@requires_docker
@pytest.mark.asyncio
@pytest.mark.parametrize("days_out", sorted(REMINDER_DAYS))
async def test_sweep_fires_reminder_on_each_trigger_day(db, monkeypatch, days_out):
    sent: list[str] = []

    def _stub(to: str, subject: str, html: str, plain: str) -> bool:
        sent.append(subject)
        return True

    monkeypatch.setattr(
        "app.tasks.enterprise_license_lifecycle._send_email", _stub,
    )

    client, lic = await _make_active_el_client(db, days_remaining=days_out)
    await db.commit()

    counts = await _sweep_with_session(db)
    assert counts["closures"] == 0
    assert counts["reminders"] == 1
    assert any(f"{days_out} days" in s for s in sent)


@requires_docker
@pytest.mark.asyncio
async def test_sweep_silent_on_non_trigger_day(db, monkeypatch):
    """On a day that isn't a trigger (e.g. 25 days out), nothing
    fires — no email, no state change."""
    sent: list[str] = []

    def _stub(to: str, subject: str, html: str, plain: str) -> bool:
        sent.append(subject)
        return True

    monkeypatch.setattr(
        "app.tasks.enterprise_license_lifecycle._send_email", _stub,
    )

    client, _ = await _make_active_el_client(db, days_remaining=25)
    await db.commit()

    counts = await _sweep_with_session(db)
    assert counts == {"closures": 0, "reminders": 0}
    assert sent == []
