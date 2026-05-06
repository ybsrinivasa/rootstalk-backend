"""BL-13 — Advisory Versioning (pure functions, no DB).

Spec: EXACTLY ONE ACTIVE version per package at any time. Every
publish increments version. Previous ACTIVE → INACTIVE
automatically. INACTIVE version can be republished (creates new
version number, does not restore old number).

This service holds the two pure rules that the live publish
endpoints share — `compute_publish_version` and
`validate_publish_transition`. Sibling deactivation
(matching on `(client_id, crop_cosh_id)` and flipping prior
ACTIVE to INACTIVE) lives in the route handlers because it
needs DB access; this module only governs version arithmetic
and status legality.

Used by all five publish endpoints in
`app/modules/advisory/router.py`:
- POST /client/{id}/packages/{id}/publish
- POST /advisory/global/packages/{id}/publish
- POST /advisory/global/pg-recommendations/{id}/publish
- POST /client/{id}/pg-recommendations/{id}/publish
- POST /client/{id}/sp-recommendations/{id}/publish
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ── Version arithmetic ────────────────────────────────────────────────────────

def compute_publish_version(current_version: int, was_published: bool) -> int:
    """Return the version number to write on publish.

    First publish (was_published=False) → version 1, regardless of
    `current_version`. Defends against legacy rows whose default
    `version=1` would have produced v=2 on first publish under the
    pre-fix `current + 1` logic.

    Subsequent publishes (was_published=True) → `current_version + 1`.
    Honours the spec rule: "INACTIVE version can be republished —
    creates new number, does not restore old." A republish from
    INACTIVE v=5 returns 6, not 5.
    """
    if not was_published:
        return 1
    return current_version + 1


# ── Status transition legality ────────────────────────────────────────────────

@dataclass(frozen=True)
class TransitionResult:
    allowed: bool
    error_code: Optional[str] = None
    message: Optional[str] = None


# DRAFT / ACTIVE / INACTIVE are all legitimate publish sources:
#   DRAFT    → ACTIVE: first publish.
#   ACTIVE   → ACTIVE: in-place edit republish (CA edited a live
#                       package and is bumping its version).
#   INACTIVE → ACTIVE: explicit republish of a previously deactivated
#                       package — version becomes a fresh higher
#                       number (not a restore of the old one).
_PUBLISHABLE_FROM: frozenset[str] = frozenset({"DRAFT", "ACTIVE", "INACTIVE"})


def validate_publish_transition(current_status: str) -> TransitionResult:
    """Confirm the current status is a legitimate source for a publish.

    Every known status (DRAFT/ACTIVE/INACTIVE) is publishable today, so
    this is mostly defence-in-depth: if a future status (e.g. ARCHIVED
    or REVOKED) is added without explicitly being added to the
    publishable set, the existing publish endpoints will reject it with
    a stable ILLEGAL_PUBLISH_SOURCE error_code instead of silently
    activating it.
    """
    if current_status in _PUBLISHABLE_FROM:
        return TransitionResult(allowed=True)
    return TransitionResult(
        allowed=False,
        error_code="ILLEGAL_PUBLISH_SOURCE",
        message=(
            f"Cannot publish from status '{current_status}'. "
            f"Allowed sources: {sorted(_PUBLISHABLE_FROM)}."
        ),
    )
