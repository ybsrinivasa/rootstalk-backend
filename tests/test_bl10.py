"""BL-10 — pure-function tests for the order state machine.

Live router wiring is exercised by the integration tests in batch 3.
This file is hermetic.
"""
from __future__ import annotations

from app.services.bl10_order_state import (
    DEALER, FACILITATOR, FARMER, SYSTEM,
    derive_order_status_from_items,
    is_item_abortable, is_order_abortable,
    validate_item_transition, validate_order_transition,
)


# ── Order-level transitions ───────────────────────────────────────────────────

def test_dealer_accepting_a_sent_order_is_allowed():
    res = validate_order_transition("SENT", "PROCESSING", DEALER)
    assert res.allowed is True


def test_farmer_cancelling_a_sent_order_is_allowed():
    res = validate_order_transition("SENT", "CANCELLED", FARMER)
    assert res.allowed is True


def test_farmer_cannot_resurrect_a_cancelled_order():
    """CANCELLED is terminal; an admin-style move back to SENT is
    not in the table. Closes a class of bug where a privileged actor
    could undo a farmer-cancelled order."""
    res = validate_order_transition("CANCELLED", "SENT", FARMER)
    assert res.allowed is False
    assert res.error_code == "ILLEGAL_TRANSITION"


def test_dealer_cannot_skip_processing_and_jump_straight_to_completed():
    res = validate_order_transition("PROCESSING", "COMPLETED", DEALER)
    assert res.allowed is False
    assert res.error_code == "ILLEGAL_TRANSITION"


def test_completion_can_only_be_written_by_system_or_farmer():
    """SENT_FOR_APPROVAL → COMPLETED is the system's bookkeeping move
    after a farmer's approve-all; a dealer cannot write COMPLETED."""
    bad = validate_order_transition("SENT_FOR_APPROVAL", "COMPLETED", DEALER)
    assert bad.allowed is False
    assert bad.error_code == "ROLE_NOT_ALLOWED"
    good = validate_order_transition("SENT_FOR_APPROVAL", "COMPLETED", SYSTEM)
    assert good.allowed is True


def test_no_op_transition_is_rejected():
    res = validate_order_transition("PROCESSING", "PROCESSING", DEALER)
    assert res.allowed is False
    assert res.error_code == "NO_OP_TRANSITION"


def test_facilitator_can_route_a_sent_order_to_processing():
    res = validate_order_transition("SENT", "PROCESSING", FACILITATOR)
    assert res.allowed is True


# ── Item-level transitions ────────────────────────────────────────────────────

def test_dealer_marking_pending_item_available_is_allowed():
    res = validate_item_transition("PENDING", "AVAILABLE", DEALER)
    assert res.allowed is True


def test_farmer_cannot_mark_an_item_available():
    """Brand+volume+price come from the dealer; a farmer should not
    be able to write AVAILABLE."""
    res = validate_item_transition("PENDING", "AVAILABLE", FARMER)
    assert res.allowed is False
    assert res.error_code == "ROLE_NOT_ALLOWED"


def test_farmer_can_approve_an_item_in_sent_for_approval():
    res = validate_item_transition("SENT_FOR_APPROVAL", "APPROVED", FARMER)
    assert res.allowed is True


def test_farmer_cannot_reject_an_already_approved_item():
    """Approval is terminal; closes the live bug where reject_order_item
    accepted any current status."""
    res = validate_item_transition("APPROVED", "REJECTED", FARMER)
    assert res.allowed is False
    assert res.error_code == "ILLEGAL_TRANSITION"


def test_farmer_can_reroute_a_rejected_item_back_to_pending():
    res = validate_item_transition("REJECTED", "PENDING", FARMER)
    assert res.allowed is True


def test_dealer_cannot_flip_a_skipped_item_back_to_available():
    """SKIPPED is the farmer's call; the dealer can't reach back into
    a closed cycle."""
    res = validate_item_transition("SKIPPED", "AVAILABLE", DEALER)
    assert res.allowed is False
    assert res.error_code == "ILLEGAL_TRANSITION"


# ── Abort policy ──────────────────────────────────────────────────────────────

def test_abortable_order_statuses_match_spec():
    assert is_order_abortable("PROCESSING") is True
    assert is_order_abortable("SENT_FOR_APPROVAL") is True
    assert is_order_abortable("PARTIALLY_APPROVED") is True
    # Cannot abort what is not in flight or already terminal.
    assert is_order_abortable("SENT") is False
    assert is_order_abortable("COMPLETED") is False
    assert is_order_abortable("CANCELLED") is False
    assert is_order_abortable("EXPIRED") is False


def test_approved_items_survive_an_order_abort():
    """Spec: an abort must not erase the farmer's prior approvals.
    Closes a real bug — the live abort_order resets ALL items."""
    assert is_item_abortable("APPROVED") is False
    assert is_item_abortable("REJECTED") is False
    assert is_item_abortable("REMOVED") is False
    assert is_item_abortable("SKIPPED") is False
    assert is_item_abortable("NOT_NEEDED") is False
    # Mid-fulfilment items DO get reset.
    assert is_item_abortable("PENDING") is True
    assert is_item_abortable("AVAILABLE") is True
    assert is_item_abortable("POSTPONED") is True
    assert is_item_abortable("NOT_AVAILABLE") is True
    assert is_item_abortable("SENT_FOR_APPROVAL") is True


# ── Derived order status ──────────────────────────────────────────────────────

def test_derive_order_status_returns_completed_when_all_in_pool_approved():
    assert derive_order_status_from_items(
        ["APPROVED", "APPROVED", "REMOVED"],  # REMOVED is outside the pool
    ) == "COMPLETED"


def test_derive_order_status_returns_partially_approved_when_mixed():
    assert derive_order_status_from_items(
        ["APPROVED", "SENT_FOR_APPROVAL"],
    ) == "PARTIALLY_APPROVED"


def test_derive_order_status_returns_none_when_no_approvals_yet():
    """Caller leaves the order's existing status untouched."""
    assert derive_order_status_from_items(
        ["SENT_FOR_APPROVAL", "SENT_FOR_APPROVAL"],
    ) is None


def test_derive_order_status_returns_none_for_empty_pool():
    """Pool size 0 (everything REMOVED / SKIPPED) — no automatic move."""
    assert derive_order_status_from_items(["REMOVED", "SKIPPED"]) is None
