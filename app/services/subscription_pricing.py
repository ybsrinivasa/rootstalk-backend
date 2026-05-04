"""
Subscription pool pricing — volume-discount formula.

Per the user's spec:

    Total = [N × 199] − [0.5 × N^1.4887593]

where N is the number of subscription units the CA is buying. The discount
term is sublinear in N so larger purchases get a steeper effective per-unit
discount. ₹199 is the per-unit gross price (same as the farmer's
self-subscribe price).

All money values are returned in **paise** (integer) to avoid float-rounding
drift across the wire. Convert to rupees only at display time.

This module is a pure function — no DB access. The HTTP endpoint
(/client/{id}/subscription-pool/quote) builds the response on top.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


# Per-unit gross price (₹199 = 19,900 paise).
PER_UNIT_GROSS_PAISE: int = 199_00

# Discount formula constants. Held as Decimal for stable rounding behaviour
# across platforms / Python versions.
_DISCOUNT_COEFF = Decimal("0.5")
_DISCOUNT_EXPONENT = Decimal("1.4887593")

# Validation bounds. The formula's gross > discount break-even is around
# N ≈ 209,000; capping well below that avoids absurd inputs and keeps
# server compute bounded.
MIN_UNITS = 1
MAX_UNITS = 50_000


@dataclass(frozen=True)
class Quote:
    """Immutable price quote for a given unit count."""
    units: int
    gross_paise: int        # units × per-unit price
    discount_paise: int     # rounded value of 0.5 × N^1.4887593 in rupees → paise
    total_paise: int        # gross − discount, never negative

    @property
    def per_unit_effective_paise(self) -> int:
        """Effective per-unit price after discount, rounded down to paise."""
        if self.units <= 0:
            return 0
        return self.total_paise // self.units


def quote_for(units: int) -> Quote:
    """Return a Quote for the given unit count. Raises ValueError on
    out-of-range input."""
    if not isinstance(units, int) or isinstance(units, bool):
        raise ValueError("units must be an integer")
    if units < MIN_UNITS:
        raise ValueError(f"units must be at least {MIN_UNITS}")
    if units > MAX_UNITS:
        raise ValueError(f"units must not exceed {MAX_UNITS}")

    gross_paise = units * PER_UNIT_GROSS_PAISE

    # 0.5 × N^1.4887593, evaluated as Decimal then rounded to two decimal
    # places (paise). Decimal's `power` is exact-ish; convert to float only
    # to use the standard library's pow() for non-integer exponents,
    # then back to Decimal for stable rounding.
    discount_rupees = _DISCOUNT_COEFF * Decimal(
        str(pow(units, float(_DISCOUNT_EXPONENT)))
    )
    discount_paise = int(
        (discount_rupees * Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP,
        )
    )

    # Defensive: never let discount exceed gross (would happen at absurd
    # unit counts only — MAX_UNITS already guards against this, but the
    # check makes the invariant explicit).
    if discount_paise > gross_paise:
        discount_paise = gross_paise

    total_paise = gross_paise - discount_paise

    return Quote(
        units=units,
        gross_paise=gross_paise,
        discount_paise=discount_paise,
        total_paise=total_paise,
    )
