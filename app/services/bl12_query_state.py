"""BL-12 — FarmPundit Query State Machine (pure functions, no DB).

Captures allowed transitions for `QueryStatus` (NEW / FORWARDED /
RETURNED → RESPONDED / REJECTED / EXPIRED) and the roles (PRIMARY /
PANEL / SYSTEM) that may invoke each one. The live FarmPundit
router validates proposed transitions against this table before
mutating rows; illegal moves are rejected with a stable error_code.

Spec rules encoded:
- 7-day expiry is set at creation and never reset on forward/return.
  This module does not write expires_at — it only governs status
  transitions, so the rule is upheld by silence.
- PANEL pundits cannot forward (TC-BL12-04) and cannot reject
  (PRIMARY-only rule from the spec). Both are encoded as
  PRIMARY-only edges in the transition table — a PANEL caller
  hitting the corresponding endpoint will get ROLE_NOT_ALLOWED
  from `validate_transition`.
- RESPONDED / REJECTED / EXPIRED are terminal — no outgoing
  edges. Closes the bug class where a stale forward/return/
  reject hits an already-closed query.
- SYSTEM (the hourly Celery sweep) is the only writer for the
  EXPIRED state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ── Roles allowed to drive transitions ────────────────────────────────────────

PRIMARY = "PRIMARY"
PANEL = "PANEL"
SYSTEM = "SYSTEM"            # the hourly expiry sweep


# ── Transition table ──────────────────────────────────────────────────────────
# (from_status, to_status) -> set of roles that may invoke this edge.

_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    # From NEW
    ("NEW", "FORWARDED"):     frozenset({PRIMARY}),       # PANEL cannot forward
    ("NEW", "RETURNED"):      frozenset({PRIMARY, PANEL}),
    ("NEW", "RESPONDED"):     frozenset({PRIMARY, PANEL}),
    ("NEW", "REJECTED"):      frozenset({PRIMARY}),       # PANEL cannot reject
    ("NEW", "EXPIRED"):       frozenset({SYSTEM}),

    # From FORWARDED. Chained forwards (A→B, then B forwards onward) keep
    # status=FORWARDED and only rotate `current_holder_id` — they don't
    # constitute a status transition, so they aren't on this graph. The
    # router short-circuits validate_transition when current == target
    # for forwards, so a chained forward writes only the holder.
    ("FORWARDED", "RETURNED"):  frozenset({PRIMARY, PANEL}),
    ("FORWARDED", "RESPONDED"): frozenset({PRIMARY, PANEL}),
    ("FORWARDED", "REJECTED"):  frozenset({PRIMARY}),
    ("FORWARDED", "EXPIRED"):   frozenset({SYSTEM}),

    # From RETURNED (the original sender now holds it again)
    ("RETURNED", "FORWARDED"):  frozenset({PRIMARY}),
    ("RETURNED", "RESPONDED"):  frozenset({PRIMARY, PANEL}),
    ("RETURNED", "REJECTED"):   frozenset({PRIMARY}),
    ("RETURNED", "EXPIRED"):    frozenset({SYSTEM}),

    # Terminals (RESPONDED / REJECTED / EXPIRED) have no outgoing edges.
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
    """Check whether the role may move a query from current → target.

    Returns TransitionResult.allowed=False with one of:
    - NO_OP_TRANSITION    — current == target
    - ILLEGAL_TRANSITION  — the pair is not on the graph (terminal,
                             or simply not allowed)
    - ROLE_NOT_ALLOWED    — the pair is on the graph but this role
                             can't invoke it (PANEL forward/reject)
    """
    if current == target:
        return TransitionResult(
            allowed=False,
            error_code="NO_OP_TRANSITION",
            message=f"Query is already in '{current}'.",
        )
    allowed_roles = _TRANSITIONS.get((current, target))
    if allowed_roles is None:
        return TransitionResult(
            allowed=False,
            error_code="ILLEGAL_TRANSITION",
            message=(
                f"Query cannot transition from '{current}' to '{target}'. "
                f"Allowed transitions out of '{current}': "
                f"{sorted({tgt for (src, tgt) in _TRANSITIONS if src == current}) or 'none'}."
            ),
        )
    if role not in allowed_roles:
        return TransitionResult(
            allowed=False,
            error_code="ROLE_NOT_ALLOWED",
            message=(
                f"Role '{role}' may not move query from '{current}' to "
                f"'{target}'. Allowed roles: {sorted(allowed_roles)}."
            ),
        )
    return TransitionResult(allowed=True)


# ── Convenience predicates (mirror the spec language) ─────────────────────────

def can_forward(role: str) -> bool:
    """PRIMARY-only — encodes TC-BL12-04 ("Panel Experts cannot forward")."""
    return role == PRIMARY


def can_reject(role: str) -> bool:
    """PRIMARY-only per the spec. Live router's docstring already says
    'Primary Expert only', but the runtime check was missing — fixed
    in batch 2."""
    return role == PRIMARY
