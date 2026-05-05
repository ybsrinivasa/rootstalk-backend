"""BL-11 — pure-function tests for the subscription state machine.

Live router wiring is exercised by the integration tests in batch 3.
This file is hermetic.
"""
from __future__ import annotations

from app.services.bl11_subscription_state import (
    DEALER, FARMER, SA, SYSTEM,
    is_self_unsubscribable, validate_transition,
)


# ── Activation paths ──────────────────────────────────────────────────────────

def test_farmer_activating_via_self_payment_is_allowed():
    res = validate_transition("WAITLISTED", "ACTIVE", FARMER)
    assert res.allowed is True


def test_dealer_activating_via_pay_on_behalf_is_allowed():
    """Dealer/facilitator paying on the farmer's behalf is the same edge
    as farmer self-payment — the routes converge on ACTIVE."""
    res = validate_transition("WAITLISTED", "ACTIVE", DEALER)
    assert res.allowed is True


def test_re_activating_an_already_active_sub_is_blocked():
    """The headline bug fix from this audit: re-writing ACTIVE on an
    already-ACTIVE sub used to silently reset subscription_date and
    re-consume a unit from the promoter's allocation. NO_OP_TRANSITION
    is the stable error_code clients see."""
    res = validate_transition("ACTIVE", "ACTIVE", DEALER)
    assert res.allowed is False
    assert res.error_code == "NO_OP_TRANSITION"


# ── Cancellation paths ────────────────────────────────────────────────────────

def test_farmer_rejecting_promoter_assignment_cancels_waitlisted():
    res = validate_transition("WAITLISTED", "CANCELLED", FARMER)
    assert res.allowed is True


def test_farmer_unsubscribing_an_active_sub_is_allowed_at_state_level():
    """The state-level transition is allowed; the SELF-vs-ASSIGNED
    business rule is enforced separately by `is_self_unsubscribable`."""
    res = validate_transition("ACTIVE", "CANCELLED", FARMER)
    assert res.allowed is True


def test_dealer_cannot_cancel_a_subscription():
    res = validate_transition("ACTIVE", "CANCELLED", DEALER)
    assert res.allowed is False
    assert res.error_code == "ROLE_NOT_ALLOWED"


def test_cancelled_is_terminal_no_revival_path():
    """Closes a class of bug where a stale verify or respond hits a
    CANCELLED sub and silently un-cancels it."""
    for target in ("ACTIVE", "WAITLISTED", "SUSPENDED", "LAPSED"):
        res = validate_transition("CANCELLED", target, FARMER)
        assert res.allowed is False
        assert res.error_code == "ILLEGAL_TRANSITION"


# ── Suspension cascade (client-status flip) ───────────────────────────────────

def test_system_cascade_suspends_active_when_client_goes_inactive():
    res = validate_transition("ACTIVE", "SUSPENDED", SYSTEM)
    assert res.allowed is True


def test_system_cascade_resumes_suspended_when_client_re_activates():
    res = validate_transition("SUSPENDED", "ACTIVE", SYSTEM)
    assert res.allowed is True


def test_sa_directly_writing_suspension_is_blocked_only_system_may():
    """The SA triggers the cascade by flipping client.status; the
    actual subscription mutation is performed by the system on the
    SA's behalf, so the role tag is SYSTEM, not SA. This test pins
    that distinction so a future refactor doesn't accidentally hand
    SA a direct write."""
    res = validate_transition("ACTIVE", "SUSPENDED", SA)
    assert res.allowed is False
    assert res.error_code == "ROLE_NOT_ALLOWED"


# ── LAPSED (currently unwired) ────────────────────────────────────────────────

def test_lapsed_edge_is_on_the_graph_for_a_future_sweep():
    """LAPSED is a known-unwired terminal — no live route writes it
    today, but the edge stays on the table so a future end-of-cycle
    sweep (SYSTEM-driven) can be added without re-opening this file."""
    res = validate_transition("ACTIVE", "LAPSED", SYSTEM)
    assert res.allowed is True


def test_lapsed_cannot_be_written_by_a_user_role():
    res = validate_transition("ACTIVE", "LAPSED", FARMER)
    assert res.allowed is False
    assert res.error_code == "ROLE_NOT_ALLOWED"


# ── SELF-vs-ASSIGNED unsubscribe rule ─────────────────────────────────────────

def test_self_subscribed_active_can_self_unsubscribe():
    assert is_self_unsubscribable("SELF", "ACTIVE") is True


def test_company_assigned_cannot_self_unsubscribe():
    """Company-assigned subscriptions must go through the company —
    farmer can't cancel themselves out of an assigned advisory."""
    assert is_self_unsubscribable("ASSIGNED", "ACTIVE") is False


def test_waitlisted_self_sub_is_not_yet_unsubscribable():
    """A SELF sub that hasn't paid yet isn't unsubscribable via the
    /unsubscribe route — it just stays WAITLISTED until either paid
    or eventually cleaned up. Closes the door on a half-finished
    cancel that hits an unpaid sub."""
    assert is_self_unsubscribable("SELF", "WAITLISTED") is False
