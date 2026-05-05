"""BL-11 — Subscription State Machine (pure functions, no DB).

Captures the allowed transitions for `SubscriptionStatus` (WAITLISTED,
ACTIVE, LAPSED, CANCELLED, SUSPENDED) plus the role (FARMER / DEALER /
SA / SYSTEM) that may invoke each one. The live subscription router
validates proposed transitions against this table before mutating
rows; illegal moves are rejected with a stable error_code so the
frontend can branch programmatically.

Design notes:
- Transitions are explicit pairs `(from, to)`, not "anywhere → terminal".
  Closes the bug class where any `verify`/`pay` route could re-write
  ACTIVE on top of itself, double-charging the promoter allocation
  and resetting subscription_date / reference_number.
- The SELF-vs-ASSIGNED unsubscribe rule (only SELF subscribers can
  voluntarily cancel; ASSIGNED ones must go through the company) is
  modelled as a separate predicate `is_self_unsubscribable` rather
  than as a role check, because the actor (FARMER) is the same in
  both cases — the difference is the subscription_type carried by
  the row.
- LAPSED is on the graph (terminal) but no live route writes it
  today. A future end-of-cycle sweep would need SYSTEM to perform
  ACTIVE → LAPSED. Leaving the edge in keeps the table truthful
  about what's permitted when that sweep is wired.
- SUSPENDED ⇄ ACTIVE is the only reversible terminal pair. Driven
  exclusively by SA-triggered client-status cascades, so role is
  SYSTEM (the cascade write is done by the system on the SA's
  behalf, not directly by SA's hand).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ── Roles allowed to drive transitions ────────────────────────────────────────

FARMER = "FARMER"
DEALER = "DEALER"           # also covers FACILITATOR — both pay-on-behalf paths
SA = "SA"
SYSTEM = "SYSTEM"           # cascades, scheduled sweeps, client-status flips


# ── Transition table ──────────────────────────────────────────────────────────
# (from_status, to_status) -> set of roles that may invoke this edge.
# Symbols are kept as strings so this module doesn't depend on the ORM enum.

_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    # Activation paths — reached by paying the ₹199 farmer fee or by a
    # promoter completing payment on the farmer's behalf.
    ("WAITLISTED", "ACTIVE"):     frozenset({FARMER, DEALER}),

    # Rejection of a promoter assignment by the farmer.
    ("WAITLISTED", "CANCELLED"):  frozenset({FARMER}),

    # Voluntary unsubscribe (SELF subs only — see is_self_unsubscribable).
    ("ACTIVE", "CANCELLED"):      frozenset({FARMER}),

    # End-of-cycle terminal — currently unwired (no live writer); on
    # the table so the future sweep can be added without re-opening it.
    ("ACTIVE", "LAPSED"):         frozenset({SYSTEM}),

    # Client suspension cascade.
    ("ACTIVE", "SUSPENDED"):      frozenset({SYSTEM}),
    ("SUSPENDED", "ACTIVE"):      frozenset({SYSTEM}),

    # Notably ABSENT (closed-by-design):
    # - WAITLISTED → SUSPENDED   (a paused client doesn't auto-suspend the queue)
    # - ACTIVE → WAITLISTED      (no rollback to waitlist after activation)
    # - CANCELLED → anything     (CANCELLED is terminal — closes "un-cancel" bug class)
    # - LAPSED → anything        (LAPSED is terminal end-of-cycle)
    # - SUSPENDED → CANCELLED    (resume-or-stay-suspended only; cancellation
    #                             goes through the active path)
}


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TransitionResult:
    allowed: bool
    error_code: Optional[str] = None
    message: Optional[str] = None


def validate_transition(
    current: str, target: str, role: str,
) -> TransitionResult:
    """Check whether the role may move a subscription from current → target.

    Returns TransitionResult.allowed=False with one of:
    - NO_OP_TRANSITION   — current == target (re-write would silently
                            double-charge / reset subscription_date)
    - ILLEGAL_TRANSITION — the pair is not on the graph
    - ROLE_NOT_ALLOWED   — the pair is on the graph but this role can't
                            invoke it
    """
    if current == target:
        return TransitionResult(
            allowed=False,
            error_code="NO_OP_TRANSITION",
            message=(
                f"Subscription is already in '{current}' — re-writing "
                "the same status would silently reset subscription_date "
                "and (for promoter-paid subs) double-charge the promoter "
                "allocation."
            ),
        )
    allowed_roles = _TRANSITIONS.get((current, target))
    if allowed_roles is None:
        return TransitionResult(
            allowed=False,
            error_code="ILLEGAL_TRANSITION",
            message=(
                f"Subscription cannot transition from '{current}' to "
                f"'{target}'. Allowed transitions out of '{current}': "
                f"{sorted({tgt for (src, tgt) in _TRANSITIONS if src == current}) or 'none'}."
            ),
        )
    if role not in allowed_roles:
        return TransitionResult(
            allowed=False,
            error_code="ROLE_NOT_ALLOWED",
            message=(
                f"Role '{role}' may not move subscription from "
                f"'{current}' to '{target}'. Allowed roles: "
                f"{sorted(allowed_roles)}."
            ),
        )
    return TransitionResult(allowed=True)


# ── SELF-vs-ASSIGNED voluntary cancellation rule ──────────────────────────────

def is_self_unsubscribable(
    subscription_type: str, current_status: str,
) -> bool:
    """True iff the subscription can be voluntarily cancelled by the
    farmer (BL-11 rule: SELF-subscribed only, only while ACTIVE).
    Company-assigned subscriptions go through the company instead —
    matches the live `/farmer/subscriptions/{id}/unsubscribe` 400."""
    return subscription_type == "SELF" and current_status == "ACTIVE"
