"""Pure-function tests for `validate_timeline` and its three
sub-checks. Integration coverage of the API surface lives in
`tests/test_phase_cca_step3_integration.py`.
"""
from __future__ import annotations

import pytest

from app.services.timeline_validation import (
    TimelineValidationError,
    validate_timeline,
    validate_timeline_direction,
    validate_timeline_sign,
    validate_timeline_type_for_package,
)


# ── direction ────────────────────────────────────────────────────────────────

def test_dbs_direction_from_greater_than_to_passes():
    """Spec example: 15 → 8 DBS."""
    validate_timeline_direction(from_type="DBS", from_value=15, to_value=8)


def test_dbs_direction_from_equal_to_to_fails():
    """Equal counts as same day; not enough range."""
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline_direction(from_type="DBS", from_value=8, to_value=8)
    assert ei.value.code == "timeline_invalid_direction"


def test_dbs_direction_from_less_than_to_fails():
    """8 → 15 DBS is direction-inverted."""
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline_direction(from_type="DBS", from_value=8, to_value=15)
    assert ei.value.code == "timeline_invalid_direction"


def test_das_direction_to_greater_than_from_passes():
    """Spec example: 0 → 8 DAS."""
    validate_timeline_direction(from_type="DAS", from_value=0, to_value=8)


def test_das_direction_to_equal_to_from_fails():
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline_direction(from_type="DAS", from_value=5, to_value=5)
    assert ei.value.code == "timeline_invalid_direction"


def test_das_direction_to_less_than_from_fails():
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline_direction(from_type="DAS", from_value=10, to_value=5)
    assert ei.value.code == "timeline_invalid_direction"


def test_calendar_direction_uses_das_rule():
    """CALENDAR follows the DAS rule (to > from): values are
    day-of-year ints, time moves forward."""
    validate_timeline_direction(from_type="CALENDAR", from_value=10, to_value=50)
    with pytest.raises(TimelineValidationError):
        validate_timeline_direction(from_type="CALENDAR", from_value=50, to_value=10)


# ── sign ─────────────────────────────────────────────────────────────────────

def test_dbs_sign_positive_passes():
    validate_timeline_sign(from_type="DBS", from_value=15, to_value=8)


def test_dbs_sign_zero_from_fails():
    """DBS from=0 would mean "0 days before crop start" — that's the
    start day itself, which is DAS territory. Reject."""
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline_sign(from_type="DBS", from_value=0, to_value=-5)
    assert ei.value.code == "timeline_invalid_sign"


def test_dbs_sign_zero_to_fails():
    """DBS to=0 means "ends at crop start" — boundary edge case
    that's ambiguous with DAS from=0. Reject."""
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline_sign(from_type="DBS", from_value=10, to_value=0)
    assert ei.value.code == "timeline_invalid_sign"


def test_dbs_sign_negative_fails():
    """DBS values represent days BEFORE start; negative values would
    be days after, contradicting the type."""
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline_sign(from_type="DBS", from_value=10, to_value=-5)
    assert ei.value.code == "timeline_invalid_sign"


def test_das_sign_zero_passes():
    """DAS from=0 IS the start day — explicitly valid (spec example
    0 → 8 DAS)."""
    validate_timeline_sign(from_type="DAS", from_value=0, to_value=8)


def test_das_sign_positive_passes():
    validate_timeline_sign(from_type="DAS", from_value=5, to_value=20)


def test_das_sign_negative_fails():
    """DAS represents start day onwards; negative is days before,
    which is DBS territory."""
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline_sign(from_type="DAS", from_value=-3, to_value=5)
    assert ei.value.code == "timeline_invalid_sign"


def test_calendar_sign_passes_through():
    """CALENDAR has no sign rule — values are day-of-year ints,
    typically 1-365."""
    validate_timeline_sign(from_type="CALENDAR", from_value=1, to_value=365)
    validate_timeline_sign(from_type="CALENDAR", from_value=0, to_value=10)


# ── type ↔ package consistency ───────────────────────────────────────────────

def test_annual_with_das_passes():
    validate_timeline_type_for_package(package_type="ANNUAL", from_type="DAS")


def test_annual_with_dbs_passes():
    validate_timeline_type_for_package(package_type="ANNUAL", from_type="DBS")


def test_annual_with_calendar_fails():
    """Annual lifecycle is anchored to crop_start, not absolute
    calendar dates. CALENDAR makes no sense here."""
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline_type_for_package(
            package_type="ANNUAL", from_type="CALENDAR",
        )
    assert ei.value.code == "timeline_type_mismatch"


def test_perennial_with_calendar_passes():
    validate_timeline_type_for_package(
        package_type="PERENNIAL", from_type="CALENDAR",
    )


def test_perennial_with_das_fails():
    """Perennials repeat annually on calendar dates, not DAS offsets."""
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline_type_for_package(
            package_type="PERENNIAL", from_type="DAS",
        )
    assert ei.value.code == "timeline_type_mismatch"


def test_perennial_with_dbs_fails():
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline_type_for_package(
            package_type="PERENNIAL", from_type="DBS",
        )
    assert ei.value.code == "timeline_type_mismatch"


# ── combined validate_timeline ───────────────────────────────────────────────

def test_validate_timeline_happy_path_annual_das():
    validate_timeline(
        package_type="ANNUAL", from_type="DAS",
        from_value=0, to_value=30,
    )


def test_validate_timeline_happy_path_annual_dbs():
    validate_timeline(
        package_type="ANNUAL", from_type="DBS",
        from_value=15, to_value=8,
    )


def test_validate_timeline_happy_path_perennial_calendar():
    validate_timeline(
        package_type="PERENNIAL", from_type="CALENDAR",
        from_value=60, to_value=120,
    )


def test_validate_timeline_type_check_runs_first():
    """When all three checks would fail, type check fires first
    because it's the most fundamental error."""
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline(
            package_type="ANNUAL", from_type="CALENDAR",
            from_value=100, to_value=50,  # also direction-bad
        )
    assert ei.value.code == "timeline_type_mismatch"


def test_validate_timeline_direction_runs_before_sign():
    """Both direction and sign would fail; direction surfaces first.
    DBS with from=-5, to=10: direction-bad (DBS wants from > to;
    -5 < 10) AND sign-bad (DBS wants strict positives)."""
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline(
            package_type="ANNUAL", from_type="DBS",
            from_value=-5, to_value=10,
        )
    assert ei.value.code == "timeline_invalid_direction"


def test_validate_timeline_sign_after_direction():
    """Direction OK, sign violation surfaces."""
    with pytest.raises(TimelineValidationError) as ei:
        validate_timeline(
            package_type="ANNUAL", from_type="DAS",
            from_value=-5, to_value=10,  # direction OK (10 > -5), sign bad (DAS negative)
        )
    assert ei.value.code == "timeline_invalid_sign"
