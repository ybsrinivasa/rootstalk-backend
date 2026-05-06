"""BL-14 — pure-function tests for the brand-visibility rule.

Live router wiring is exercised by the integration test in the same
batch (`tests/test_phase_bl14_approval_integration.py`). This file
is hermetic.
"""
from __future__ import annotations

from app.services.bl14_approval import is_brand_visible_to_farmer


def test_brand_revealed_at_sent_for_approval():
    """The headline rule: spec says brand is revealed to the farmer
    for the FIRST TIME at the approval step. Pre-fix the live route
    revealed it only AFTER approval — so the farmer was being asked
    to approve without seeing the brand."""
    assert is_brand_visible_to_farmer("SENT_FOR_APPROVAL") is True


def test_brand_stays_visible_after_approval():
    """Once the farmer approves, the brand stays visible — the
    purchased-items view depends on it."""
    assert is_brand_visible_to_farmer("APPROVED") is True


def test_brand_hidden_before_dealer_submits():
    """Pre-SENT_FOR_APPROVAL the dealer is still working out brand
    selection (PENDING / AVAILABLE / POSTPONED). The farmer should
    not see what the dealer is leaning towards before they commit."""
    for status in ("PENDING", "AVAILABLE", "POSTPONED"):
        assert is_brand_visible_to_farmer(status) is False


def test_brand_hidden_for_terminal_negative_states():
    """Negative terminals shouldn't leak brand either — those items
    won't be approved, so showing brand has no value and could
    confuse the farmer's view."""
    for status in ("NOT_AVAILABLE", "REJECTED", "REMOVED", "SKIPPED"):
        assert is_brand_visible_to_farmer(status) is False


def test_unknown_status_treated_as_hidden():
    """Defence-in-depth: any future enum value not added to the
    visibility set defaults to hidden. Avoids accidental leaks if
    someone adds a state like ARCHIVED later."""
    assert is_brand_visible_to_farmer("ARCHIVED") is False
    assert is_brand_visible_to_farmer("") is False
