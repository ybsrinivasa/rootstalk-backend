"""
BL-06 Volume Calculation
Given a volume_formula record and input variables, evaluates the formula
expression and returns (estimated_volume, brand_unit).

Variables present in formula expressions:
  Dosage        — dosage rate (e.g. mL/L or g/L from the PoP element)
  Total_area    — farm area in acres (from subscription.farm_area_acres)
  Concentration — product concentration, if applicable
  Volume_water  — water volume per pump/acre, application-method specific
  Count         — plant count or row count, for per-plant formulas
  Applications  — number of times the practice will be applied across the timeline.
                  Defaults to 1 for one-time practices (backwards compatible).
                  For frequency-based practices: ceil(timeline_duration_days / frequency_days).

The evaluator is sandboxed (no builtins) to prevent injection.

Frequency-based volume rule (overrides AgriTeam doc §6.8):
When a farmer places ANY order covering part of a timeline, the order is considered
to cover the ENTIRE timeline. So volume = full-timeline volume, NOT remaining-from-order-date.
"""
from __future__ import annotations
from typing import Optional
import math


_ALLOWED_NAMES = {
    "abs": abs, "round": round, "min": min, "max": max,
    "ceil": math.ceil, "floor": math.floor,
}


def evaluate_formula(formula: str, variables: dict[str, float]) -> float:
    """Safely evaluate a formula string with the given numeric variables.

    Per the Volume Calculation Formulas Reference (April 2026): seeded
    formulas use the × character (U+00D7, the Unicode multiplication
    sign) for multiplication. Python's eval cannot parse ×, so we
    substitute it with * before compiling. Without this substitution,
    every seeded formula returns a parse error and the dealer sees
    "Could not calculate estimate" for every item — a silent failure
    that bypasses every test that uses the * character.
    """
    if formula:
        formula = formula.replace("×", "*")
    env = {**_ALLOWED_NAMES, **variables}
    try:
        result = eval(compile(formula, "<formula>", "eval"), {"__builtins__": {}}, env)
        return float(result)
    except Exception as exc:
        raise ValueError(f"Formula evaluation failed: {exc}") from exc


def calculate_volume(
    formula: str,
    brand_unit: str,
    dosage: Optional[float],
    farm_area_acres: Optional[float],
    concentration: Optional[float] = None,
    volume_water_per_acre: Optional[float] = None,
    frequency_days: Optional[int] = None,
    timeline_duration_days: Optional[int] = None,
    applications: Optional[int] = None,
) -> Optional[tuple[float, str]]:
    """
    Returns (estimated_volume, brand_unit) or None if inputs are insufficient.

    Applications resolution (Phase D.3):
    1. If `applications` is provided directly (now the SE-preferred path —
       value comes from a Practice element with element_type='applications'
       which the SE confirms at practice creation time), use it.
    2. Otherwise fall back to the legacy compute:
       - frequency_days + timeline_duration_days both >= 1 →
         Applications = ceil(timeline_duration_days / frequency_days)
       - Either missing or zero → Applications = 1 (one-time).

    The element-first path means the SE sees and confirms the count once,
    and that fixed value flows downstream — no risk of re-computation drift
    when frequency or timeline shifts later. The legacy compute remains for
    practices created before Applications-as-element rolled out.
    """
    if farm_area_acres is None:
        return None

    # Element-first; legacy compute is the fallback.
    if applications is not None and applications >= 1:
        resolved_applications = int(applications)
    elif frequency_days and frequency_days >= 1 and timeline_duration_days and timeline_duration_days >= 1:
        resolved_applications = math.ceil(timeline_duration_days / frequency_days)
    else:
        resolved_applications = 1

    variables: dict[str, float] = {
        "Total_area": float(farm_area_acres),
        "Dosage": float(dosage) if dosage else 0.0,
        "Concentration": float(concentration) if concentration else 1.0,
        "Volume_water": float(volume_water_per_acre) if volume_water_per_acre else 200.0,
        "Applications": float(resolved_applications),
    }
    try:
        volume = evaluate_formula(formula, variables)
        return round(volume, 3), brand_unit
    except ValueError:
        return None
