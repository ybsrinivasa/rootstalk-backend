"""BL-09 — pure-function tests for the daily alert decisions.

Live wiring is exercised separately by the integration tests in
`tests/test_phase_bl09_alerts_integration.py`. This file is hermetic:
no DB, no network.
"""
from __future__ import annotations

from datetime import date

from app.services.bl09_alerts import (
    AlertRecipientSpec, ConfiguredRecipient, SubscriptionView, TimelineWindow,
    find_input_practices_due_today, practices_still_unordered,
    resolve_alert_recipients, should_send_input_alert,
    should_send_start_date_alert,
)


# ── Recipient resolution ──────────────────────────────────────────────────────

def test_resolve_recipients_defaults_to_farmer_plus_promoter_when_unconfigured():
    sub = SubscriptionView(
        subscription_id="s1", subscription_type="ASSIGNED",
        farmer_user_id="farmer", promoter_user_id="promoter",
        crop_start_date=None,
    )
    out = resolve_alert_recipients(sub, configured=[])
    assert out == [
        AlertRecipientSpec(user_id="farmer", role="FARMER"),
        AlertRecipientSpec(user_id="promoter", role="LOCAL_PERSON"),
    ]


def test_resolve_recipients_self_subscribed_defaults_to_farmer_only():
    """Self-subscribed has no promoter — default is the farmer alone, no
    duplicate or empty placeholder local-person row."""
    sub = SubscriptionView(
        subscription_id="s1", subscription_type="SELF",
        farmer_user_id="farmer", promoter_user_id=None,
        crop_start_date=None,
    )
    out = resolve_alert_recipients(sub, configured=[])
    assert out == [AlertRecipientSpec(user_id="farmer", role="FARMER")]


def test_resolve_recipients_skips_promoter_if_same_as_farmer():
    """Defensive: if a buggy row makes promoter_user_id == farmer_user_id,
    we don't double-alert the farmer."""
    sub = SubscriptionView(
        subscription_id="s1", subscription_type="ASSIGNED",
        farmer_user_id="farmer", promoter_user_id="farmer",
        crop_start_date=None,
    )
    out = resolve_alert_recipients(sub, configured=[])
    assert out == [AlertRecipientSpec(user_id="farmer", role="FARMER")]


def test_resolve_recipients_uses_configured_rows_when_present():
    """Once the farmer has saved alert preferences, those rows are
    authoritative. Default farmer+promoter behaviour is overridden —
    even an empty FARMER row (i.e. opted out) is respected."""
    sub = SubscriptionView(
        subscription_id="s1", subscription_type="ASSIGNED",
        farmer_user_id="farmer", promoter_user_id="promoter_default",
        crop_start_date=None,
    )
    configured = [
        ConfiguredRecipient(user_id="dealer_chosen_by_farmer", role="LOCAL_PERSON"),
    ]
    out = resolve_alert_recipients(sub, configured=configured)
    # Farmer's chosen dealer wins over the assigning promoter; farmer
    # not in the list because the farmer chose to opt out (no FARMER
    # row in the configured list).
    assert out == [
        AlertRecipientSpec(user_id="dealer_chosen_by_farmer", role="LOCAL_PERSON"),
    ]


def test_resolve_recipients_normalises_legacy_promoter_role():
    """The `alert_recipients` table stores 'PROMOTER' historically; the
    BL-09 spec says 'local person'. Output uses the BL-09 vocabulary."""
    sub = SubscriptionView(
        subscription_id="s1", subscription_type="ASSIGNED",
        farmer_user_id="farmer", promoter_user_id="promoter",
        crop_start_date=None,
    )
    configured = [
        ConfiguredRecipient(user_id="farmer", role="FARMER"),
        ConfiguredRecipient(user_id="promoter", role="PROMOTER"),
    ]
    out = resolve_alert_recipients(sub, configured=configured)
    assert AlertRecipientSpec(user_id="promoter", role="LOCAL_PERSON") in out
    assert all(r.role != "PROMOTER" for r in out)


def test_resolve_recipients_dedupes_by_user_id():
    """Misconfigured rows shouldn't cause duplicate SMS fan-out."""
    sub = SubscriptionView(
        subscription_id="s1", subscription_type="ASSIGNED",
        farmer_user_id="farmer", promoter_user_id="p",
        crop_start_date=None,
    )
    configured = [
        ConfiguredRecipient(user_id="dealer", role="LOCAL_PERSON"),
        ConfiguredRecipient(user_id="dealer", role="PROMOTER"),  # duplicate
    ]
    out = resolve_alert_recipients(sub, configured=configured)
    assert len(out) == 1
    assert out[0].user_id == "dealer"


# ── START_DATE alert ──────────────────────────────────────────────────────────

def test_start_date_alert_fires_when_unset_and_not_yet_sent_today():
    sub = SubscriptionView(
        "s1", "ASSIGNED", "farmer", "promoter", crop_start_date=None,
    )
    assert should_send_start_date_alert(sub, sent_today=False) is True


def test_start_date_alert_does_not_fire_when_already_sent_today():
    """Idempotency: same subscription, same day, no second SMS."""
    sub = SubscriptionView(
        "s1", "ASSIGNED", "farmer", "promoter", crop_start_date=None,
    )
    assert should_send_start_date_alert(sub, sent_today=True) is False


def test_start_date_alert_does_not_fire_when_start_date_set():
    """Once the farmer has sown, the alert silences automatically."""
    sub = SubscriptionView(
        "s1", "ASSIGNED", "farmer", "promoter",
        crop_start_date=date(2026, 5, 1),
    )
    assert should_send_start_date_alert(sub, sent_today=False) is False


# ── Input window detection ────────────────────────────────────────────────────

def test_find_input_practices_due_today_handles_das_window():
    """DAS: from <= day_offset <= to (positive offsets)."""
    timelines = [
        TimelineWindow(
            timeline_id="t1", from_type="DAS", from_value=10, to_value=20,
            input_practice_ids=("p1", "p2"),
        ),
    ]
    assert find_input_practices_due_today(timelines, day_offset=15) == ["p1", "p2"]
    assert find_input_practices_due_today(timelines, day_offset=5) == []
    assert find_input_practices_due_today(timelines, day_offset=25) == []


def test_find_input_practices_due_today_handles_dbs_with_production_convention():
    """DBS: production seed has from > to (e.g. 'active 15→8 days
    before sowing'); window is -from <= offset <= -to. This was the
    inverted-inequality bug fixed in BL-04 — same family of bug
    surfaced in alerts.py."""
    timelines = [
        TimelineWindow(
            timeline_id="t1", from_type="DBS", from_value=15, to_value=8,
            input_practice_ids=("base_fertiliser",),
        ),
    ]
    # 12 days before sowing → in window.
    assert find_input_practices_due_today(timelines, day_offset=-12) == ["base_fertiliser"]
    # 5 days before sowing → past the window.
    assert find_input_practices_due_today(timelines, day_offset=-5) == []
    # 20 days before sowing → before the window.
    assert find_input_practices_due_today(timelines, day_offset=-20) == []


def test_find_input_practices_due_today_skips_calendar_timelines():
    """CALENDAR is deferred by `cca_window_active` to match the BL-04
    today route; this test pins the behaviour so it's not re-broken."""
    timelines = [
        TimelineWindow(
            timeline_id="t1", from_type="CALENDAR",
            from_value=6, to_value=8, input_practice_ids=("p1",),
        ),
    ]
    assert find_input_practices_due_today(timelines, day_offset=0) == []


# ── INPUT alert decision (incl. order suppression) ────────────────────────────

def test_input_alert_blocked_until_start_date_set():
    sub = SubscriptionView(
        "s1", "ASSIGNED", "farmer", "promoter", crop_start_date=None,
    )
    assert should_send_input_alert(
        sub, due_practice_ids=["p1"],
        practice_ids_with_active_orders=set(), sent_today=False,
    ) is False


def test_input_alert_fires_when_any_due_practice_unordered():
    sub = SubscriptionView(
        "s1", "ASSIGNED", "farmer", "promoter",
        crop_start_date=date(2026, 5, 1),
    )
    assert should_send_input_alert(
        sub, due_practice_ids=["p1", "p2"],
        practice_ids_with_active_orders={"p1"},  # p2 still un-ordered
        sent_today=False,
    ) is True


def test_input_alert_silences_when_every_due_practice_ordered():
    """Spec: alert removed once farmer orders the input."""
    sub = SubscriptionView(
        "s1", "ASSIGNED", "farmer", "promoter",
        crop_start_date=date(2026, 5, 1),
    )
    assert should_send_input_alert(
        sub, due_practice_ids=["p1", "p2"],
        practice_ids_with_active_orders={"p1", "p2"},
        sent_today=False,
    ) is False


def test_input_alert_not_resent_same_day():
    sub = SubscriptionView(
        "s1", "ASSIGNED", "farmer", "promoter",
        crop_start_date=date(2026, 5, 1),
    )
    assert should_send_input_alert(
        sub, due_practice_ids=["p1"],
        practice_ids_with_active_orders=set(), sent_today=True,
    ) is False


def test_practices_still_unordered_filters_correctly():
    out = practices_still_unordered(
        due_practice_ids=["a", "b", "c"],
        practice_ids_with_active_orders={"b"},
    )
    assert out == ["a", "c"]
