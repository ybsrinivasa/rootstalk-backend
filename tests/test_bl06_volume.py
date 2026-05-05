"""
BL-06 Volume Calculation tests.

Covers one-time formulas (backwards compatible) and frequency-based formulas
that use the new `Applications` variable.
"""
from app.services.bl06_volume_calc import calculate_volume, evaluate_formula


def test_one_time_volume():
    # 2 kg/acre dosage, 5 acres -> 10 kg
    result = calculate_volume(
        formula="Dosage * Total_area",
        brand_unit="kg",
        dosage=2.0,
        farm_area_acres=5.0,
    )
    assert result == (10.0, "kg")


def test_one_time_with_applications_default():
    # When Applications is in the formula but no frequency given, Applications = 1.
    result = calculate_volume(
        formula="Dosage * Total_area * Applications",
        brand_unit="kg",
        dosage=2.0,
        farm_area_acres=5.0,
    )
    assert result == (10.0, "kg")


def test_frequency_based_volume():
    # 30-day timeline, every 2 days, 2 kg per application, 1 acre.
    # Applications = ceil(30/2) = 15. Volume = 2 * 1 * 15 = 30 kg.
    result = calculate_volume(
        formula="Dosage * Total_area * Applications",
        brand_unit="kg",
        dosage=2.0,
        farm_area_acres=1.0,
        frequency_days=2,
        timeline_duration_days=30,
    )
    assert result == (30.0, "kg")


def test_frequency_ceiling():
    # 7-day timeline, every 3 days. Applications = ceil(7/3) = 3 (Day 1, 4, 7).
    result = calculate_volume(
        formula="Dosage * Applications * Total_area",
        brand_unit="L",
        dosage=1.0,
        farm_area_acres=1.0,
        frequency_days=3,
        timeline_duration_days=7,
    )
    assert result == (3.0, "L")


def test_frequency_one_day_per_acre():
    # Daily, 10 days. 5 g * 2 acres * 10 applications = 100 g.
    result = calculate_volume(
        formula="Dosage * Total_area * Applications",
        brand_unit="g",
        dosage=5.0,
        farm_area_acres=2.0,
        frequency_days=1,
        timeline_duration_days=10,
    )
    assert result == (100.0, "g")


def test_no_farm_area_returns_none():
    result = calculate_volume(
        formula="Dosage * Total_area",
        brand_unit="kg",
        dosage=2.0,
        farm_area_acres=None,
    )
    assert result is None


def test_frequency_only_one_param_falls_back_to_one_time():
    # If only frequency_days is given (no duration), Applications = 1.
    result = calculate_volume(
        formula="Dosage * Total_area * Applications",
        brand_unit="kg",
        dosage=2.0,
        farm_area_acres=5.0,
        frequency_days=2,
    )
    assert result == (10.0, "kg")


def test_frequency_zero_duration_falls_back_to_one_time():
    # Defensive: zero duration shouldn't divide-by-zero or explode; Applications = 1.
    result = calculate_volume(
        formula="Dosage * Total_area * Applications",
        brand_unit="kg",
        dosage=2.0,
        farm_area_acres=5.0,
        frequency_days=2,
        timeline_duration_days=0,
    )
    assert result == (10.0, "kg")


# ── × (U+00D7) substitution — production formulas use × not * ──────────────
# Per the Volume Calculation Formulas Reference (April 2026), every seeded
# formula uses × instead of *. Without the substitution in evaluate_formula,
# Python's eval would parse-error on every one of them and the dealer would
# see "Could not calculate estimate" for every item.

def test_unicode_times_substituted_simple():
    """Real seeded formula: `Dosage × Total_area` for a kg/acre direct dose."""
    result = calculate_volume(
        formula="Dosage × Total_area",
        brand_unit="kg",
        dosage=3.0,
        farm_area_acres=4.0,
    )
    assert result == (12.0, "kg")


def test_unicode_times_substituted_foliar_spray():
    """Real seeded formula: `(Dosage × 150 × Total_area)/1000` — foliar spray
    with 150 L/acre water rate baked in. Dosage 2 g/L, 5 acres → 1.5 kg."""
    result = calculate_volume(
        formula="(Dosage × 150 × Total_area)/1000",
        brand_unit="kg",
        dosage=2.0,
        farm_area_acres=5.0,
    )
    assert result == (1.5, "kg")


def test_unicode_times_substituted_soil_drench():
    """Real seeded formula: `Dosage × 200 × Total_area` — soil drenching
    with 200 L/acre water rate. Dosage 1 ml/L, 3 acres → 600 ml."""
    result = calculate_volume(
        formula="Dosage × 200 × Total_area",
        brand_unit="ml",
        dosage=1.0,
        farm_area_acres=3.0,
    )
    assert result == (600.0, "ml")


def test_unicode_times_evaluator_directly():
    """evaluate_formula handles × independently of calculate_volume."""
    out = evaluate_formula("a × b × c", {"a": 2.0, "b": 3.0, "c": 4.0})
    assert out == 24.0


def test_mixed_times_and_star_both_work():
    """Defensive: a formula could mix × and * (e.g. a hand-edited row).
    Both should evaluate."""
    out = evaluate_formula("a × b * c", {"a": 2.0, "b": 3.0, "c": 4.0})
    assert out == 24.0


def test_empty_formula_does_not_crash_substitution():
    """No formula is invalid input — must raise ValueError, not surprise."""
    import pytest
    with pytest.raises(ValueError):
        evaluate_formula("", {})
