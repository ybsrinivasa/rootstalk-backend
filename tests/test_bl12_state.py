"""BL-12 — pure-function tests for the query state machine.

The routing-priority service `app/services/bl12_query_routing.py`
has its own coverage in `tests/test_bl12.py` (11 tests). This file
covers the new state-machine service. Live router wiring is
exercised by the integration tests in batch 3.
"""
from __future__ import annotations

from app.services.bl12_query_state import (
    PANEL, PRIMARY, SYSTEM,
    can_forward, can_reject, validate_transition,
)


# ── Forward / Return / Respond / Reject from NEW ──────────────────────────────

def test_primary_can_forward_a_new_query():
    res = validate_transition("NEW", "FORWARDED", PRIMARY)
    assert res.allowed is True


def test_panel_cannot_forward_a_new_query():
    """TC-BL12-04: Panel Experts cannot forward queries."""
    res = validate_transition("NEW", "FORWARDED", PANEL)
    assert res.allowed is False
    assert res.error_code == "ROLE_NOT_ALLOWED"


def test_panel_can_respond_and_return():
    """PANEL pundits can respond and return — they just can't initiate
    (forward/reject) anything."""
    assert validate_transition("NEW", "RESPONDED", PANEL).allowed is True
    assert validate_transition("NEW", "RETURNED", PANEL).allowed is True


def test_panel_cannot_reject_a_query():
    """Spec: 'Primary Expert only' for reject. Pre-fix the live route
    didn't enforce this — test pins the rule."""
    res = validate_transition("NEW", "REJECTED", PANEL)
    assert res.allowed is False
    assert res.error_code == "ROLE_NOT_ALLOWED"


# ── Chained forwards are NOT a status transition ─────────────────────────────

def test_chained_forwards_are_not_a_status_transition():
    """A → B → C → … chains keep status=FORWARDED and only rotate
    current_holder_id. The router short-circuits validate_transition
    when current == target for forwards, so a chained forward writes
    only the holder. The table reflects this by NOT listing
    FORWARDED → FORWARDED — a NO_OP_TRANSITION error fires if a
    caller ever asks for it (defence-in-depth)."""
    res = validate_transition("FORWARDED", "FORWARDED", PRIMARY)
    assert res.allowed is False
    assert res.error_code == "NO_OP_TRANSITION"


# ── Return / Respond / Reject from FORWARDED ─────────────────────────────────

def test_recipient_can_return_a_forwarded_query():
    """Either role can return — that's how the recipient hands the
    query back to the sender."""
    assert validate_transition("FORWARDED", "RETURNED", PANEL).allowed is True
    assert validate_transition("FORWARDED", "RETURNED", PRIMARY).allowed is True


def test_recipient_can_respond_to_a_forwarded_query():
    assert validate_transition("FORWARDED", "RESPONDED", PRIMARY).allowed is True
    assert validate_transition("FORWARDED", "RESPONDED", PANEL).allowed is True


# ── RETURNED can be re-forwarded ──────────────────────────────────────────────

def test_returned_query_can_be_re_forwarded_by_primary():
    """After A forwards to B and B returns it, A (PRIMARY) can
    forward it onward to C."""
    res = validate_transition("RETURNED", "FORWARDED", PRIMARY)
    assert res.allowed is True


# ── Terminal states block all outgoing transitions ───────────────────────────

def test_responded_is_terminal():
    """Closes the bug class where a stale forward/return/reject lands
    on an already-RESPONDED query."""
    for role in (PRIMARY, PANEL, SYSTEM):
        for target in ("FORWARDED", "RETURNED", "RESPONDED", "REJECTED", "EXPIRED", "NEW"):
            res = validate_transition("RESPONDED", target, role)
            assert res.allowed is False


def test_rejected_is_terminal():
    for role in (PRIMARY, PANEL):
        for target in ("FORWARDED", "RETURNED", "RESPONDED"):
            res = validate_transition("REJECTED", target, role)
            assert res.allowed is False


def test_expired_is_terminal():
    """Once the hourly sweep moves a query to EXPIRED, no further
    actions land — even by the system itself."""
    for role in (PRIMARY, PANEL, SYSTEM):
        for target in ("RESPONDED", "REJECTED", "FORWARDED"):
            res = validate_transition("EXPIRED", target, role)
            assert res.allowed is False


# ── EXPIRED is system-only ────────────────────────────────────────────────────

def test_only_system_writes_expired():
    """The hourly Celery sweep is the sole writer of EXPIRED. A pundit
    accidentally hitting an "expire" code path would get
    ROLE_NOT_ALLOWED."""
    assert validate_transition("FORWARDED", "EXPIRED", SYSTEM).allowed is True
    res = validate_transition("FORWARDED", "EXPIRED", PRIMARY)
    assert res.allowed is False
    assert res.error_code == "ROLE_NOT_ALLOWED"


# ── No-op rejection ──────────────────────────────────────────────────────────

def test_no_op_transition_is_rejected_for_unchanged_status():
    """Same-state writes (NEW → NEW, RESPONDED → RESPONDED, etc.) are
    rejected with NO_OP_TRANSITION rather than reaching the table.
    Mirrors the BL-11 design — gives the router a stable error_code
    to surface for replayed requests."""
    res = validate_transition("NEW", "NEW", PRIMARY)
    assert res.allowed is False
    assert res.error_code == "NO_OP_TRANSITION"


# ── can_forward / can_reject predicates ───────────────────────────────────────

def test_can_forward_only_primary():
    assert can_forward(PRIMARY) is True
    assert can_forward(PANEL) is False
    assert can_forward(SYSTEM) is False


def test_can_reject_only_primary():
    assert can_reject(PRIMARY) is True
    assert can_reject(PANEL) is False
