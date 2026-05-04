"""Pure-function tests for subscription pool pricing.

Formula: Total = [N × 199] − [0.5 × N^1.4887593]
"""
import pytest

from app.services.subscription_pricing import (
    MAX_UNITS, MIN_UNITS, PER_UNIT_GROSS_PAISE, Quote, quote_for,
)


# ── Validation ──────────────────────────────────────────────────────────────

def test_rejects_zero_units():
    with pytest.raises(ValueError, match="at least"):
        quote_for(0)


def test_rejects_negative_units():
    with pytest.raises(ValueError, match="at least"):
        quote_for(-5)


def test_rejects_above_max():
    with pytest.raises(ValueError, match="must not exceed"):
        quote_for(MAX_UNITS + 1)


def test_rejects_non_int():
    with pytest.raises(ValueError, match="integer"):
        quote_for("100")  # type: ignore[arg-type]


def test_rejects_bool():
    """bool is technically an int in Python; explicitly reject."""
    with pytest.raises(ValueError, match="integer"):
        quote_for(True)  # type: ignore[arg-type]


# ── Numeric correctness against hand-computed values ───────────────────────

def test_n_1_minimum_purchase():
    """N=1: gross 199.00, discount 0.5×1^1.49 = ₹0.50, total ₹198.50."""
    q = quote_for(1)
    assert q.units == 1
    assert q.gross_paise == 199_00
    assert q.discount_paise == 50           # ₹0.50
    assert q.total_paise == 198_50


def test_n_10():
    """N=10: gross ₹1,990. Discount ₹15.41 → total ₹1,974.59.
    These are regression-style assertions against the live formula —
    if anyone tunes the formula constants this test fails loudly."""
    q = quote_for(10)
    assert q.gross_paise == 1990_00
    assert q.discount_paise == 1541
    assert q.total_paise == 197459
    assert q.total_paise == q.gross_paise - q.discount_paise


def test_n_100():
    """N=100: gross ₹19,900, discount ₹474.78, total ₹19,425.22."""
    q = quote_for(100)
    assert q.gross_paise == 19900_00
    assert q.discount_paise == 47478
    assert q.total_paise == 1942522


def test_n_1000():
    """N=1000: gross ₹1,99,000, discount ₹14,630.12, total ₹1,84,369.88."""
    q = quote_for(1000)
    assert q.gross_paise == 199000_00
    assert q.discount_paise == 1463012
    assert q.total_paise == 18436988


def test_n_max():
    """At MAX_UNITS=50,000 the formula must still yield a positive total
    well above zero (sanity check that the cap is set safely)."""
    q = quote_for(50_000)
    assert q.gross_paise == 995000000        # ₹99,50,000
    assert q.total_paise > 0
    # Discount is approaching gross at this scale; effective per-unit
    # drops to ~half the gross unit price (₹100ish vs ₹199 gross).
    assert q.per_unit_effective_paise < PER_UNIT_GROSS_PAISE * 0.6


# ── Monotonicity & shape ───────────────────────────────────────────────────

def test_total_increases_with_units():
    """More units → higher total. Always."""
    last = -1
    for n in [1, 2, 5, 10, 50, 100, 500, 1000, 5000, 10000]:
        q = quote_for(n)
        assert q.total_paise > last, f"total dropped at N={n}"
        last = q.total_paise


def test_per_unit_effective_decreases_with_units():
    """Discount is sublinear → per-unit effective price drops as N grows."""
    q1 = quote_for(1)
    q10 = quote_for(10)
    q1000 = quote_for(1000)
    assert q1.per_unit_effective_paise > q10.per_unit_effective_paise
    assert q10.per_unit_effective_paise > q1000.per_unit_effective_paise


def test_total_never_exceeds_gross():
    """Total should always be ≤ gross (discount can't be negative)."""
    for n in [1, 10, 100, 1000, 10000, MAX_UNITS]:
        q = quote_for(n)
        assert q.total_paise <= q.gross_paise
        assert q.discount_paise >= 0


def test_total_never_negative_at_max():
    """At MAX_UNITS the formula must still yield a positive total."""
    q = quote_for(MAX_UNITS)
    assert q.total_paise > 0
    assert q.discount_paise < q.gross_paise


def test_quote_is_frozen():
    """Quote dataclass is immutable so callers can pass it around safely."""
    q = quote_for(10)
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        q.units = 999  # type: ignore[misc]


def test_per_unit_constant_matches_spec():
    """₹199 = 19,900 paise, the documented per-unit gross."""
    assert PER_UNIT_GROSS_PAISE == 19900


def test_min_max_constants():
    assert MIN_UNITS == 1
    assert MAX_UNITS == 50_000
