"""BL-10 — Order Flow State Machine (pure functions, no DB).

Captures the ALLOWED transitions for both `OrderStatus` and
`OrderItemStatus`, plus the role (FARMER / DEALER / FACILITATOR /
SYSTEM) that may invoke each one. The live order router validates
proposed transitions against these tables before mutating rows;
illegal moves are rejected with a stable error_code so the frontend
can branch programmatically.

Design notes:
- Transitions are explicit pairs `(from, to)`, not "anything → terminal".
  EXPIRED and CANCELLED are reachable only from specific upstream
  statuses, so an admin / sweep can't accidentally resurrect a
  COMPLETED order.
- DRAFT is intentionally retained on the order graph even though the
  live `create_order` jumps straight to SENT — leaving the edge in
  place lets a future "save draft" flow land without a graph change.
- The "abort_order" action (dealer revoke) is a many-to-one collapse
  of items back to PENDING. To avoid wiping the farmer's prior
  approvals, the abort is defined per-item: items in
  `_PROTECTED_ITEM_STATUSES` are NOT touched.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ── Roles allowed to drive transitions ────────────────────────────────────────

FARMER = "FARMER"
DEALER = "DEALER"
FACILITATOR = "FACILITATOR"
SYSTEM = "SYSTEM"            # nightly sweep, expiry, scheduled jobs

ALL_ROLES = frozenset({FARMER, DEALER, FACILITATOR, SYSTEM})


# ── Order-level transitions ───────────────────────────────────────────────────
# (from_status, to_status) -> set of roles that may invoke this edge.
# Symbols are kept as strings so this module doesn't depend on the ORM enum.

_ORDER_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("DRAFT", "SENT"):                    frozenset({FARMER}),
    ("DRAFT", "CANCELLED"):               frozenset({FARMER}),

    ("SENT", "PROCESSING"):               frozenset({DEALER, FACILITATOR}),
    ("SENT", "ACCEPTED"):                 frozenset({DEALER}),
    ("SENT", "CANCELLED"):                frozenset({FARMER}),
    ("SENT", "EXPIRED"):                  frozenset({SYSTEM}),

    ("ACCEPTED", "PROCESSING"):           frozenset({DEALER, FACILITATOR}),
    ("ACCEPTED", "EXPIRED"):              frozenset({SYSTEM}),

    # Dealer abort: reset to SENT so a different dealer can pick it up.
    ("PROCESSING", "SENT"):               frozenset({DEALER, FACILITATOR}),
    ("PROCESSING", "SENT_FOR_APPROVAL"):  frozenset({DEALER}),
    ("PROCESSING", "EXPIRED"):            frozenset({SYSTEM}),

    # After farmer-side approvals; live router writes both edges from
    # `_update_order_status` based on the items' approval ratio.
    ("SENT_FOR_APPROVAL", "COMPLETED"):           frozenset({SYSTEM, FARMER}),
    ("SENT_FOR_APPROVAL", "PARTIALLY_APPROVED"):  frozenset({SYSTEM, FARMER}),

    # Edge cases observed in the live router that the table must allow:
    # - try_another_dealer can land back in PROCESSING from a partially
    #   approved or sent-for-approval order while a single item is
    #   being re-routed. Permitting only the single forward edge keeps
    #   things simple for now; extend if PARTIALLY_APPROVED → PROCESSING
    #   becomes a real flow.
}


# ── Item-level transitions ────────────────────────────────────────────────────

_ITEM_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    # Dealer fulfilment.
    ("PENDING", "AVAILABLE"):                frozenset({DEALER}),
    ("PENDING", "POSTPONED"):                frozenset({DEALER}),
    ("PENDING", "NOT_AVAILABLE"):            frozenset({DEALER}),
    ("POSTPONED", "AVAILABLE"):              frozenset({DEALER}),
    ("POSTPONED", "NOT_AVAILABLE"):          frozenset({DEALER}),

    # Sibling / part-aware closure: dealer marks an OR-group cousin
    # NOT_AVAILABLE while marking the chosen one AVAILABLE.
    ("AVAILABLE", "NOT_AVAILABLE"):          frozenset({DEALER}),

    # Submit-for-approval batch.
    ("AVAILABLE", "SENT_FOR_APPROVAL"):      frozenset({DEALER}),

    # Farmer side.
    ("SENT_FOR_APPROVAL", "APPROVED"):       frozenset({FARMER}),
    ("SENT_FOR_APPROVAL", "REJECTED"):       frozenset({FARMER}),

    # Recovery flows: NOT_AVAILABLE / REJECTED items can be re-routed
    # to a new dealer (back to PENDING) or skipped for this cycle.
    ("NOT_AVAILABLE", "PENDING"):            frozenset({FARMER}),
    ("NOT_AVAILABLE", "SKIPPED"):            frozenset({FARMER}),
    ("REJECTED", "PENDING"):                 frozenset({FARMER}),

    # Pre-approval housekeeping.
    ("PENDING", "REMOVED"):                  frozenset({FARMER}),
    ("AVAILABLE", "REMOVED"):                frozenset({FARMER}),

    # Dealer abort revert. NOT a free edge — the abort handler in the
    # router only rolls back items in `_ABORTABLE_ITEM_STATUSES` so
    # APPROVED / REJECTED / REMOVED / SKIPPED items survive the abort.
    ("AVAILABLE", "PENDING"):                frozenset({DEALER}),
    ("POSTPONED", "PENDING"):                frozenset({DEALER}),
    ("NOT_AVAILABLE", "PENDING"):            frozenset({DEALER}),  # abort path
    ("SENT_FOR_APPROVAL", "PENDING"):        frozenset({DEALER}),
}


# ── Abort policy ──────────────────────────────────────────────────────────────

# The order-level abort flips items in these statuses back to PENDING.
# Items in any other status (APPROVED, REJECTED, REMOVED, SKIPPED,
# NOT_NEEDED) are LEFT ALONE — those carry meaningful farmer or
# system decisions that an abort must not erase.
_ABORTABLE_ITEM_STATUSES: frozenset[str] = frozenset({
    "PENDING", "AVAILABLE", "POSTPONED", "NOT_AVAILABLE", "SENT_FOR_APPROVAL",
})


def is_item_abortable(current_item_status: str) -> bool:
    """True iff the item should be reset to PENDING during an order abort.
    APPROVED / REJECTED / REMOVED / SKIPPED / NOT_NEEDED items are
    preserved across an abort (they carry farmer or system decisions).
    """
    return current_item_status in _ABORTABLE_ITEM_STATUSES


# Order-level abort is only valid in mid-fulfilment statuses. A
# CANCELLED, COMPLETED, or EXPIRED order cannot be resurrected via
# abort; the farmer / system has to take a different action.
_ABORTABLE_ORDER_STATUSES: frozenset[str] = frozenset({
    "PROCESSING", "SENT_FOR_APPROVAL", "PARTIALLY_APPROVED",
})


def is_order_abortable(current_order_status: str) -> bool:
    return current_order_status in _ABORTABLE_ORDER_STATUSES


# ── Validation helper ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TransitionResult:
    allowed: bool
    error_code: Optional[str] = None
    message: Optional[str] = None


def validate_order_transition(
    current: str, target: str, role: str,
) -> TransitionResult:
    return _validate(_ORDER_TRANSITIONS, current, target, role, kind="order")


def validate_item_transition(
    current: str, target: str, role: str,
) -> TransitionResult:
    return _validate(_ITEM_TRANSITIONS, current, target, role, kind="item")


def _validate(
    table: dict[tuple[str, str], frozenset[str]],
    current: str, target: str, role: str, *, kind: str,
) -> TransitionResult:
    if current == target:
        return TransitionResult(
            allowed=False,
            error_code="NO_OP_TRANSITION",
            message=f"{kind} is already in '{current}' — nothing to change.",
        )
    allowed_roles = table.get((current, target))
    if allowed_roles is None:
        return TransitionResult(
            allowed=False,
            error_code="ILLEGAL_TRANSITION",
            message=(
                f"{kind} cannot transition from '{current}' to '{target}'. "
                f"Allowed transitions out of '{current}': "
                f"{sorted({tgt for (src, tgt) in table if src == current}) or 'none'}."
            ),
        )
    if role not in allowed_roles:
        return TransitionResult(
            allowed=False,
            error_code="ROLE_NOT_ALLOWED",
            message=(
                f"Role '{role}' may not move {kind} from '{current}' to "
                f"'{target}'. Allowed roles: {sorted(allowed_roles)}."
            ),
        )
    return TransitionResult(allowed=True)


# ── Convenience: derived order status from item-status counts ─────────────────

def derive_order_status_from_items(item_statuses: list[str]) -> Optional[str]:
    """Mirrors the logic in router._update_order_status, lifted here so
    it can be unit-tested without a DB. Looks at items currently in
    SENT_FOR_APPROVAL or APPROVED — the "approval pool":

    - all in pool are APPROVED (and pool non-empty) → COMPLETED
    - any in pool are APPROVED                     → PARTIALLY_APPROVED
    - otherwise                                    → None (caller leaves
      the order's existing status untouched)
    """
    pool = [s for s in item_statuses if s in ("SENT_FOR_APPROVAL", "APPROVED")]
    approved = [s for s in pool if s == "APPROVED"]
    if not approved:
        return None
    if len(approved) == len(pool):
        return "COMPLETED"
    return "PARTIALLY_APPROVED"
