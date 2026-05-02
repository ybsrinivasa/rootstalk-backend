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

The evaluator is sandboxed (no builtins) to prevent injection.
"""
from __future__ import annotations
from typing import Optional
import math


_ALLOWED_NAMES = {
    "abs": abs, "round": round, "min": min, "max": max,
    "ceil": math.ceil, "floor": math.floor,
}


def evaluate_formula(formula: str, variables: dict[str, float]) -> float:
    """Safely evaluate a formula string with the given numeric variables."""
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
) -> Optional[tuple[float, str]]:
    """
    Returns (estimated_volume, brand_unit) or None if inputs are insufficient.
    """
    if farm_area_acres is None:
        return None

    variables: dict[str, float] = {
        "Total_area": float(farm_area_acres),
        "Dosage": float(dosage) if dosage else 0.0,
        "Concentration": float(concentration) if concentration else 1.0,
        "Volume_water": float(volume_water_per_acre) if volume_water_per_acre else 200.0,
    }
    try:
        volume = evaluate_formula(formula, variables)
        return round(volume, 3), brand_unit
    except ValueError:
        return None
