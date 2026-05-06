"""Pure-function tests for the crop-lifecycle cascade.

These exercise `cascade_inactivate_packages_for_crop` and
`restore_cascade_inactivated_packages` against in-memory Package
instances — no DB. Integration coverage of the
add_crop/remove_crop endpoints lives in
`tests/test_phase_cca_step1_integration.py`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.modules.advisory.models import PackageStatus
from app.services.crop_lifecycle import (
    cascade_inactivate_packages_for_crop,
    derive_active_crop_set,
    restore_cascade_inactivated_packages,
)


def _pkg(status: PackageStatus, cascade_at=None, crop_cosh_id="crop:test") -> SimpleNamespace:
    """Stand-in for an ORM Package — only the fields the service touches."""
    return SimpleNamespace(
        status=status, cascade_inactivated_at=cascade_at,
        crop_cosh_id=crop_cosh_id,
    )


# ── cascade_inactivate_packages_for_crop ─────────────────────────────────────

def test_cascade_flips_active_to_inactive_with_timestamp():
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    p = _pkg(PackageStatus.ACTIVE)
    changed = cascade_inactivate_packages_for_crop([p], now)
    assert changed == [p]
    assert p.status == PackageStatus.INACTIVE
    assert p.cascade_inactivated_at == now


def test_cascade_skips_already_inactive():
    """An INACTIVE package was inactivated for an unrelated reason
    (e.g. superseded by a newer published version). The CA crop
    removal must not claim ownership of that inactivation — leaving
    `cascade_inactivated_at` NULL ensures the re-add doesn't revive
    it."""
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    p = _pkg(PackageStatus.INACTIVE, cascade_at=None)
    changed = cascade_inactivate_packages_for_crop([p], now)
    assert changed == []
    assert p.status == PackageStatus.INACTIVE
    assert p.cascade_inactivated_at is None


def test_cascade_skips_draft():
    """DRAFT packages have no farmer subscriptions and are not
    publicly visible. Removing the crop must not silently flip them
    out of DRAFT."""
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    p = _pkg(PackageStatus.DRAFT)
    changed = cascade_inactivate_packages_for_crop([p], now)
    assert changed == []
    assert p.status == PackageStatus.DRAFT
    assert p.cascade_inactivated_at is None


def test_cascade_processes_mixed_set():
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    active = _pkg(PackageStatus.ACTIVE)
    draft = _pkg(PackageStatus.DRAFT)
    inactive = _pkg(PackageStatus.INACTIVE)
    changed = cascade_inactivate_packages_for_crop([active, draft, inactive], now)
    assert changed == [active]
    assert active.status == PackageStatus.INACTIVE
    assert draft.status == PackageStatus.DRAFT
    assert inactive.status == PackageStatus.INACTIVE


def test_cascade_empty_input_no_op():
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    assert cascade_inactivate_packages_for_crop([], now) == []


# ── restore_cascade_inactivated_packages ─────────────────────────────────────

def test_restore_revives_only_cascade_marked():
    """Two INACTIVE packages: one cascade-inactivated, one
    independently inactivated. Re-add must revive only the first."""
    cascade_marked = _pkg(
        PackageStatus.INACTIVE,
        cascade_at=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
    )
    independent = _pkg(PackageStatus.INACTIVE, cascade_at=None)
    changed = restore_cascade_inactivated_packages([cascade_marked, independent])
    assert changed == [cascade_marked]
    assert cascade_marked.status == PackageStatus.ACTIVE
    assert cascade_marked.cascade_inactivated_at is None
    assert independent.status == PackageStatus.INACTIVE
    assert independent.cascade_inactivated_at is None


def test_restore_clears_timestamp():
    p = _pkg(
        PackageStatus.INACTIVE,
        cascade_at=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
    )
    restore_cascade_inactivated_packages([p])
    assert p.cascade_inactivated_at is None


def test_restore_leaves_drafts_alone():
    """A DRAFT package was never cascaded (we don't stamp DRAFTs on
    removal). Re-add must not flip it to ACTIVE."""
    p = _pkg(PackageStatus.DRAFT, cascade_at=None)
    changed = restore_cascade_inactivated_packages([p])
    assert changed == []
    assert p.status == PackageStatus.DRAFT


def test_restore_empty_input_no_op():
    assert restore_cascade_inactivated_packages([]) == []


# ── derive_active_crop_set (Batch 1D) ────────────────────────────────────────

def test_derive_active_set_picks_only_active_packages():
    """Spec: a crop is active iff at least one PoP under it is
    ACTIVE. DRAFT and INACTIVE PoPs do not contribute."""
    pkgs = [
        _pkg(PackageStatus.ACTIVE, crop_cosh_id="crop:paddy"),
        _pkg(PackageStatus.DRAFT, crop_cosh_id="crop:tomato"),
        _pkg(PackageStatus.INACTIVE, crop_cosh_id="crop:coconut"),
    ]
    assert derive_active_crop_set(pkgs) == {"crop:paddy"}


def test_derive_active_set_dedupes_when_multiple_pops_same_crop():
    """Two ACTIVE PoPs for the same crop — set semantics handle it."""
    pkgs = [
        _pkg(PackageStatus.ACTIVE, crop_cosh_id="crop:paddy"),
        _pkg(PackageStatus.ACTIVE, crop_cosh_id="crop:paddy"),
    ]
    assert derive_active_crop_set(pkgs) == {"crop:paddy"}


def test_derive_active_set_empty_when_only_drafts():
    """Crop with only a DRAFT PoP is not yet 'active' — there's no
    live advisory delivery happening for any farmer."""
    pkgs = [_pkg(PackageStatus.DRAFT, crop_cosh_id="crop:paddy")]
    assert derive_active_crop_set(pkgs) == set()


def test_derive_active_set_empty_input():
    assert derive_active_crop_set([]) == set()
