"""BL-09 audit — DB-backed integration tests for the live daily-alerts task.

Pure-function coverage lives in `tests/test_bl09.py` (17 tests). This
file exercises `app.tasks.alerts._run_daily_alerts_with_session` end
to end against a real Postgres test container. Each test seeds a
subscription + recipients + (optionally) timelines/practices/orders,
runs the task, and asserts on the `alerts` rows it writes.

`send_sms` is monkeypatched to a recorder so the tests stay hermetic
(no Draft4SMS calls). The recorder also lets us assert that the SMS
body is the BL-09 template — not the OTP boilerplate that the old
code accidentally produced.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import Order, OrderItem, OrderItemStatus, OrderStatus
from app.modules.subscriptions.models import (
    Alert, AlertRecipient, AlertType, SubscriptionType,
)
from app.tasks import alerts as alerts_task
from app.tasks.alerts import _run_daily_alerts_with_session
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_practice, make_subscription,
    make_timeline, make_user,
)


@pytest.fixture
def sms_recorder(monkeypatch):
    """Replace send_sms with a recorder. Returns the list of (phone, message)
    tuples sent during the test. Hermetic — no network."""
    sent: list[tuple[str, str]] = []

    async def fake_send_sms(phone: str, message: str) -> bool:
        sent.append((phone, message))
        return True

    monkeypatch.setattr(alerts_task, "send_sms", fake_send_sms)
    return sent


@pytest.fixture
def fcm_recorder(monkeypatch):
    """Replace send_fcm with a recorder. Returns the list of
    (token, title, body, data) tuples sent during the test."""
    sent: list[tuple[str, str, str, dict]] = []

    async def fake_send_fcm(token, title, body, data=None):
        sent.append((token, title, body, data or {}))
        return True

    monkeypatch.setattr(alerts_task, "send_fcm", fake_send_fcm)
    return sent


async def _seed_assigned_active_sub(db, *, with_promoter=True, with_start_date=False):
    farmer = await make_user(db, name="Farmer A")
    promoter = await make_user(db, name="Promoter P") if with_promoter else None
    client = await make_client(db)
    package = await make_package(db, client, name="Tomato Pack")
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.subscription_type = SubscriptionType.ASSIGNED if with_promoter else SubscriptionType.SELF
    if with_promoter:
        sub.promoter_user_id = promoter.id
    if with_start_date:
        sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=15)
    await db.commit()
    return sub, farmer, promoter, package


# ── START_DATE alert ──────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_start_date_alert_defaults_to_farmer_plus_promoter(db, sms_recorder):
    """No alert_recipients rows configured ⇒ default fan-out reaches both
    the farmer and the assigning promoter (was farmer-only before the
    audit)."""
    sub, farmer, promoter, _ = await _seed_assigned_active_sub(db)

    await _run_daily_alerts_with_session(db)

    alert_rows = (await db.execute(
        select(Alert).where(Alert.subscription_id == sub.id)
    )).scalars().all()
    recipient_ids = {a.recipient_user_id for a in alert_rows}
    assert recipient_ids == {farmer.id, promoter.id}
    assert all(a.alert_type == AlertType.START_DATE for a in alert_rows)
    # Two SMS sent, both with the BL-09 template (NOT the OTP boilerplate).
    assert len(sms_recorder) == 2
    for _phone, body in sms_recorder:
        assert "no start date is set" in body
        assert "sign-in code" not in body  # the old send_otp_sms misuse


@requires_docker
@pytest.mark.asyncio
async def test_self_subscribed_start_date_alert_goes_to_farmer_only(db, sms_recorder):
    sub, farmer, _, _ = await _seed_assigned_active_sub(db, with_promoter=False)

    await _run_daily_alerts_with_session(db)

    rows = (await db.execute(
        select(Alert).where(Alert.subscription_id == sub.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].recipient_user_id == farmer.id
    assert len(sms_recorder) == 1


@requires_docker
@pytest.mark.asyncio
async def test_configured_local_person_overrides_default_promoter(db, sms_recorder):
    """The farmer has chosen a different dealer in the PWA — that dealer
    is the only local person alerted, regardless of who originally
    assigned the subscription."""
    sub, farmer, promoter, _ = await _seed_assigned_active_sub(db)
    chosen_dealer = await make_user(db, name="Chosen Dealer")
    db.add(AlertRecipient(
        subscription_id=sub.id, recipient_user_id=chosen_dealer.id,
        recipient_type="LOCAL_PERSON", status="ACTIVE",
    ))
    await db.commit()

    await _run_daily_alerts_with_session(db)

    recipient_ids = {
        r.recipient_user_id for r in (await db.execute(
            select(Alert).where(Alert.subscription_id == sub.id)
        )).scalars().all()
    }
    assert recipient_ids == {chosen_dealer.id}
    assert promoter.id not in recipient_ids
    assert farmer.id not in recipient_ids


@requires_docker
@pytest.mark.asyncio
async def test_start_date_alert_idempotent_within_a_day(db, sms_recorder):
    """Running the daily task twice on the same day must not duplicate
    Alert rows or SMS sends."""
    sub, _, _, _ = await _seed_assigned_active_sub(db)

    await _run_daily_alerts_with_session(db)
    first_count = len((await db.execute(
        select(Alert).where(Alert.subscription_id == sub.id)
    )).scalars().all())
    sms_after_first = len(sms_recorder)

    await _run_daily_alerts_with_session(db)
    second_count = len((await db.execute(
        select(Alert).where(Alert.subscription_id == sub.id)
    )).scalars().all())

    assert first_count == second_count
    assert len(sms_recorder) == sms_after_first


# ── INPUT alert ───────────────────────────────────────────────────────────────

async def _seed_input_due_today(db, sub, package, *, das_offset_days: int = 0):
    """Seed a DAS timeline whose window includes today, plus an INPUT
    practice. `das_offset_days` controls how far past sowing we are
    (sub.crop_start_date is set so today - start = das_offset_days)."""
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=das_offset_days)
    await db.commit()
    tl = await make_timeline(
        db, package, name="TL_INPUT",
        from_type=TimelineFromType.DAS,
        from_value=max(0, das_offset_days - 1),
        to_value=das_offset_days + 1,
    )
    practice = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2="UREA",
    )
    await make_element(db, practice, value="50", unit_cosh_id="kg_per_acre")
    await db.commit()
    return tl, practice


@requires_docker
@pytest.mark.asyncio
async def test_input_alert_fires_when_input_due_and_no_order(db, sms_recorder):
    sub, farmer, promoter, package = await _seed_assigned_active_sub(db)
    await _seed_input_due_today(db, sub, package, das_offset_days=10)

    await _run_daily_alerts_with_session(db)

    rows = (await db.execute(
        select(Alert).where(
            Alert.subscription_id == sub.id,
            Alert.alert_type == AlertType.INPUT,
        )
    )).scalars().all()
    assert {r.recipient_user_id for r in rows} == {farmer.id, promoter.id}
    assert all("input is due today" in body for _, body in sms_recorder)


@requires_docker
@pytest.mark.asyncio
async def test_input_alert_suppressed_by_active_order(db, sms_recorder):
    """Spec: INPUT alert removed once farmer orders the input. We seed an
    Order in PROCESSING covering the practice — no INPUT alert should fire."""
    sub, farmer, _, package = await _seed_assigned_active_sub(db)
    _, practice = await _seed_input_due_today(db, sub, package, das_offset_days=10)

    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id,
        client_id=sub.client_id,
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=14),
        status=OrderStatus.PROCESSING,
    )
    db.add(order); await db.flush()
    db.add(OrderItem(
        order_id=order.id, practice_id=practice.id,
        timeline_id=practice.timeline_id, status=OrderItemStatus.PENDING,
    ))
    await db.commit()

    await _run_daily_alerts_with_session(db)

    rows = (await db.execute(
        select(Alert).where(
            Alert.subscription_id == sub.id,
            Alert.alert_type == AlertType.INPUT,
        )
    )).scalars().all()
    assert rows == []


# ── FCM channel ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_start_date_alert_pushes_fcm_when_recipient_has_token(
    db, sms_recorder, fcm_recorder,
):
    """FCM Batch 2 wiring. Recipients with an fcm_token set get a push
    notification in addition to the SMS. The push payload's `data` field
    carries `alert_type` and `subscription_id` so the PWA can deep-link
    into the right screen on tap."""
    sub, farmer, promoter, _ = await _seed_assigned_active_sub(db)
    farmer.fcm_token = "fcm-token-farmer"
    promoter.fcm_token = "fcm-token-promoter"
    await db.commit()

    await _run_daily_alerts_with_session(db)

    assert len(fcm_recorder) == 2
    tokens = {entry[0] for entry in fcm_recorder}
    assert tokens == {"fcm-token-farmer", "fcm-token-promoter"}

    # Title / body shape matches BL-09 design — short title for the
    # banner, fuller body for the lock-screen preview.
    titles = {entry[1] for entry in fcm_recorder}
    assert titles == {"Set your sowing date"}

    # Data payload carries the routing info the PWA needs.
    for _token, _title, _body, data in fcm_recorder:
        assert data["alert_type"] == "START_DATE"
        assert data["subscription_id"] == sub.id


@requires_docker
@pytest.mark.asyncio
async def test_alert_skips_fcm_when_recipient_has_no_token(
    db, sms_recorder, fcm_recorder,
):
    """A recipient with no fcm_token registered (most farmers in V1
    until the PWA wires the registration call) gets SMS only — no
    FCM call attempted."""
    sub, _, _, _ = await _seed_assigned_active_sub(db)
    # Don't set any fcm_token — defaults to None in the factory.
    await _run_daily_alerts_with_session(db)
    assert len(fcm_recorder) == 0
    # SMS still fires for both recipients (farmer + default promoter).
    assert len(sms_recorder) == 2


@requires_docker
@pytest.mark.asyncio
async def test_alert_writes_db_row_even_when_both_channels_unavailable(
    db, sms_recorder, fcm_recorder,
):
    """A recipient with neither phone nor fcm_token still gets an
    Alert row written for the audit trail. The dashboard / RM portal
    can use the row to escalate ('this farmer was never reached')."""
    farmer = await make_user(db, name="Phoneless Farmer")
    farmer.phone = None
    farmer.fcm_token = None
    client = await make_client(db)
    package = await make_package(db, client, name="Pack")
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.subscription_type = SubscriptionType.SELF
    await db.commit()

    await _run_daily_alerts_with_session(db)

    rows = (await db.execute(
        select(Alert).where(Alert.subscription_id == sub.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].recipient_user_id == farmer.id
    assert len(sms_recorder) == 0
    assert len(fcm_recorder) == 0


@requires_docker
@pytest.mark.asyncio
async def test_input_alert_pushes_fcm_with_input_payload(
    db, sms_recorder, fcm_recorder,
):
    """INPUT alerts get their own FCM title / body distinct from
    START_DATE. Verifies the per-alert-type FCM constants flow through
    correctly."""
    sub, farmer, _, package = await _seed_assigned_active_sub(db)
    farmer.fcm_token = "fcm-token-input"
    await _seed_input_due_today(db, sub, package, das_offset_days=10)

    await _run_daily_alerts_with_session(db)

    farmer_pushes = [e for e in fcm_recorder if e[0] == "fcm-token-input"]
    assert len(farmer_pushes) == 1
    _, title, _body, data = farmer_pushes[0]
    assert title == "Input due today"
    assert data["alert_type"] == "INPUT"
    assert data["subscription_id"] == sub.id


@requires_docker
@pytest.mark.asyncio
async def test_input_alert_fires_for_pre_sowing_dbs_window(db, sms_recorder):
    """DBS production convention: from > to (e.g. 'active 15→8 days
    before sowing'). The pre-audit code's inverted inequality meant
    pre-sowing INPUT alerts never fired in production. This test pins
    the BL-04-style fix carried into alerts.py."""
    farmer = await make_user(db, name="Farmer DBS")
    promoter = await make_user(db, name="Promoter DBS")
    client = await make_client(db)
    package = await make_package(db, client, name="Brinjal Pack")
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.subscription_type = SubscriptionType.ASSIGNED
    sub.promoter_user_id = promoter.id
    # Today is 12 days BEFORE planned sowing → day_offset = -12.
    sub.crop_start_date = datetime.now(timezone.utc) + timedelta(days=12)
    await db.commit()

    tl = await make_timeline(
        db, package, name="TL_DBS",
        from_type=TimelineFromType.DBS, from_value=15, to_value=8,
    )
    practice = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="SOIL_TREATMENT", l2="LIME",
    )
    await make_element(db, practice, value="100", unit_cosh_id="kg_per_acre")
    await db.commit()

    await _run_daily_alerts_with_session(db)

    rows = (await db.execute(
        select(Alert).where(
            Alert.subscription_id == sub.id,
            Alert.alert_type == AlertType.INPUT,
        )
    )).scalars().all()
    assert {r.recipient_user_id for r in rows} == {farmer.id, promoter.id}
