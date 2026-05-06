"""BL-13 — pure-function tests for the advisory versioning service.

Live router wiring is exercised by the integration tests in batch 3.
This file is hermetic.
"""
from __future__ import annotations

from app.services.bl13_versioning import (
    compute_publish_version, validate_publish_transition,
)


# ── Version arithmetic ────────────────────────────────────────────────────────

def test_first_publish_starts_at_version_1():
    """The headline fix. Pre-audit, the default version=1 plus an
    unconditional `version + 1` on publish meant the first published
    version was v=2. Now first publish (no published_at on the row)
    returns 1."""
    assert compute_publish_version(current_version=1, was_published=False) == 1


def test_first_publish_ignores_a_legacy_default_of_1():
    """Defends against rows that already shipped with default
    version=1 — a legacy DRAFT being published for the first time
    should still come out as v=1, not v=2."""
    assert compute_publish_version(current_version=1, was_published=False) == 1


def test_first_publish_with_unusual_default_still_returns_1():
    """First publish always normalises to 1 regardless of what
    `current_version` happens to be on the row."""
    assert compute_publish_version(current_version=42, was_published=False) == 1
    assert compute_publish_version(current_version=0, was_published=False) == 1


def test_second_publish_increments_from_one_to_two():
    """The standard in-place edit republish path."""
    assert compute_publish_version(current_version=1, was_published=True) == 2


def test_subsequent_publishes_keep_incrementing():
    """A package edited and republished many times keeps walking
    upward."""
    assert compute_publish_version(current_version=2, was_published=True) == 3
    assert compute_publish_version(current_version=10, was_published=True) == 11


def test_inactive_republish_creates_new_number_does_not_restore_old():
    """Spec: 'INACTIVE version can be republished — creates new
    version number, does not restore old number.' A row that was at
    v=5, went INACTIVE due to a sibling publish, then is being
    republished should land at v=6 — never reverting to its previous
    active number."""
    assert compute_publish_version(current_version=5, was_published=True) == 6


# ── validate_publish_transition ───────────────────────────────────────────────

def test_publish_from_draft_is_allowed():
    res = validate_publish_transition("DRAFT")
    assert res.allowed is True


def test_publish_from_inactive_is_allowed():
    """Spec explicitly permits republishing an INACTIVE version."""
    res = validate_publish_transition("INACTIVE")
    assert res.allowed is True


def test_publish_from_active_is_allowed():
    """In-place edit republish (CA edits a live package and bumps
    its version) goes ACTIVE → ACTIVE."""
    res = validate_publish_transition("ACTIVE")
    assert res.allowed is True


def test_publish_from_unknown_status_is_rejected():
    """Defence-in-depth: if a future enum value (ARCHIVED, REVOKED…)
    is added without being added to the publishable set, the existing
    publish endpoints return a stable ILLEGAL_PUBLISH_SOURCE rather
    than silently activating it."""
    res = validate_publish_transition("ARCHIVED")
    assert res.allowed is False
    assert res.error_code == "ILLEGAL_PUBLISH_SOURCE"


def test_publish_from_empty_status_is_rejected():
    """Empty / None-ish status (corrupted row, missing field) shouldn't
    sneak through as a legitimate publish source."""
    res = validate_publish_transition("")
    assert res.allowed is False
    assert res.error_code == "ILLEGAL_PUBLISH_SOURCE"
