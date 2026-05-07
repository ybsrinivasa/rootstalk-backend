"""Pure-function tests for `validate_package_duration_for_create`
and `validate_package_duration_for_update`.

Integration coverage of the API surface (create_package /
update_package returning 422 with the right error code) lives in
`tests/test_phase_cca_step2_integration.py`.
"""
from __future__ import annotations

import pytest

from app.services.package_validation import (
    PackageValidationError,
    validate_package_duration_for_create,
    validate_package_duration_for_update,
)


# ── validate_package_duration_for_create ─────────────────────────────────────

def test_create_annual_valid_duration_returns_input():
    assert validate_package_duration_for_create(
        package_type="ANNUAL", duration_days=120,
    ) == 120


def test_create_annual_boundary_1_and_365():
    assert validate_package_duration_for_create(
        package_type="ANNUAL", duration_days=1,
    ) == 1
    assert validate_package_duration_for_create(
        package_type="ANNUAL", duration_days=365,
    ) == 365


def test_create_annual_missing_duration_raises():
    """Spec §4.1: Annual duration is mandatory. Pre-fix the live
    route silently defaulted to 180 — a CA who omitted the field
    got a Package with 180-day timelines they didn't ask for."""
    with pytest.raises(PackageValidationError) as ei:
        validate_package_duration_for_create(
            package_type="ANNUAL", duration_days=None,
        )
    assert ei.value.code == "duration_required"


def test_create_annual_zero_raises():
    with pytest.raises(PackageValidationError) as ei:
        validate_package_duration_for_create(
            package_type="ANNUAL", duration_days=0,
        )
    assert ei.value.code == "duration_out_of_range"


def test_create_annual_negative_raises():
    with pytest.raises(PackageValidationError) as ei:
        validate_package_duration_for_create(
            package_type="ANNUAL", duration_days=-5,
        )
    assert ei.value.code == "duration_out_of_range"


def test_create_annual_too_large_raises():
    """Spec caps Annual at 365. A typo like 9999 should not ship —
    timeline arithmetic downstream assumes a sane upper bound."""
    with pytest.raises(PackageValidationError) as ei:
        validate_package_duration_for_create(
            package_type="ANNUAL", duration_days=9999,
        )
    assert ei.value.code == "duration_out_of_range"


def test_create_perennial_forces_365_regardless_of_input():
    """Spec §4.1: Perennial duration is system-set, not editable.
    Whatever the caller sends, persist 365."""
    assert validate_package_duration_for_create(
        package_type="PERENNIAL", duration_days=None,
    ) == 365
    assert validate_package_duration_for_create(
        package_type="PERENNIAL", duration_days=100,
    ) == 365
    assert validate_package_duration_for_create(
        package_type="PERENNIAL", duration_days=365,
    ) == 365


# ── validate_package_duration_for_update ─────────────────────────────────────

def test_update_no_change_keeps_current():
    """`new_duration is None` means the field wasn't sent in the
    update body (PackageUpdate fields are all Optional). Keep current."""
    assert validate_package_duration_for_update(
        package_type="ANNUAL", current_duration=120, new_duration=None,
    ) == 120
    assert validate_package_duration_for_update(
        package_type="PERENNIAL", current_duration=365, new_duration=None,
    ) == 365


def test_update_annual_valid_change_accepted():
    assert validate_package_duration_for_update(
        package_type="ANNUAL", current_duration=120, new_duration=180,
    ) == 180


def test_update_annual_out_of_range_raises():
    with pytest.raises(PackageValidationError) as ei:
        validate_package_duration_for_update(
            package_type="ANNUAL", current_duration=120, new_duration=400,
        )
    assert ei.value.code == "duration_out_of_range"


def test_update_perennial_resending_365_accepted():
    """Clients that send the full PackageUpdate body shouldn't fail
    just because they re-sent the unchanged 365 value."""
    assert validate_package_duration_for_update(
        package_type="PERENNIAL", current_duration=365, new_duration=365,
    ) == 365


def test_update_perennial_changing_value_raises():
    """Spec §4.1: Perennial duration is locked. Pre-fix the live
    route would have flipped a Perennial's duration_days to whatever
    was sent."""
    with pytest.raises(PackageValidationError) as ei:
        validate_package_duration_for_update(
            package_type="PERENNIAL", current_duration=365, new_duration=100,
        )
    assert ei.value.code == "perennial_duration_locked"


def test_update_perennial_changing_to_zero_raises():
    with pytest.raises(PackageValidationError) as ei:
        validate_package_duration_for_update(
            package_type="PERENNIAL", current_duration=365, new_duration=0,
        )
    assert ei.value.code == "perennial_duration_locked"
