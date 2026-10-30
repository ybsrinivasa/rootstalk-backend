"""NPK Dosage Purchase quantity computation (2026-10-30).

Given a farmer's purchase record (brand they bought) + the practice's
authored NPK dose + the subscription's field size (acres OR plants),
computes how many kg of the specific product the farmer should apply
AND emits a human scope label ("2 acres" / "1000 plants") for the
PWA to render alongside.

Supported dose units on NPK Dosage practices:
  - kg/acre   × farm_area_acres        → kg nutrient total
  - g/acre    × farm_area_acres  /1000 → kg nutrient total
  - kg/plant  × number_of_plants       → kg nutrient total
  - g/plant   × number_of_plants /1000 → kg nutrient total

Fertigation g/L (per-litre dosing) uses the 200 L/acre standard spray
volume; handled only when farm_area_acres is set (foliar practices
authored for acre-based crops). Per-plant g/L isn't shipped yet.

Formula per product role:
  - STRAIGHT_N: kg_product = total_N / (concentration.n / 100)
  - STRAIGHT_P / STRAIGHT_K: analogous.
  - MIXED:     anchor on `match_target` (N/P/K) stored at purchase
               time — matches rank_mixed's kg_product × scope semantic
               the farmer saw at pick-time.

Fail-silent: if any input is missing (no field size matching the
unit, no Cosh concentration, no match_target on a MIXED row), returns
None and the caller omits the "apply X kg" line entirely. Zero
empty-card garbage.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import Element, Practice
from app.modules.subscriptions.models import NpkDosagePurchase, Subscription
from app.modules.sync.models import CoshCoreItem
from app.services.npk_candidates import load_fertiliser_candidates


# L2 types the quantity helper is active for.
NPK_DOSAGE_L2_TYPES = frozenset({
    "CHEMICAL_FERTILIZERS_NPK_DOSAGES",
    "FERTIGATION_NPK_DOSAGES",
})


def _is_uuid(s: str) -> bool:
    import re
    return bool(re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        s or "", flags=re.IGNORECASE,
    ))


async def _resolve_unit_label(
    db: AsyncSession, by_type: dict,
) -> str:
    """Return a lowercased, trimmed unit label like 'g/plant' or
    'kg/acre'. Reads the UNIT Element: prefers `.value`, then raw
    `.cosh_ref` when it's already a readable token, otherwise walks
    Cosh for the Core row's English translation.
    """
    el = by_type.get("UNIT") or by_type.get("unit")
    if el is None:
        return ""
    if el.value:
        return str(el.value).strip().lower()
    ref = el.cosh_ref
    if not ref:
        return ""
    if not _is_uuid(ref):
        return ref.strip().lower()
    core = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == ref)
    )).scalar_one_or_none()
    if core is None:
        return ""
    name = (core.translations or {}).get("en") or ""
    return name.strip().lower()


def _classify_unit(unit: str) -> Optional[tuple[str, float]]:
    """Return (scope: 'acre'|'plant'|'L', mass_scale_to_kg) or None
    when the unit isn't recognised.

    mass_scale_to_kg: 1.0 for kg-based units; 0.001 for g-based.
    scope: whichever subscription field the dose multiplies against.
    """
    u = (unit or "").strip().lower().replace(" ", "")
    # Accept both "g/plant" and "g/plt" variants used in Cosh.
    PLANT_SUFFIXES = ("/plant", "/plt")
    ACRE_SUFFIXES = ("/acre", "/ac")
    LITRE_SUFFIXES = ("/l", "/litre", "/liter")
    if u.startswith("kg"):
        if any(u.endswith(s) for s in PLANT_SUFFIXES): return ("plant", 1.0)
        if any(u.endswith(s) for s in ACRE_SUFFIXES):  return ("acre",  1.0)
        if any(u.endswith(s) for s in LITRE_SUFFIXES): return ("L",     1.0)
    if u.startswith("g"):
        if any(u.endswith(s) for s in PLANT_SUFFIXES): return ("plant", 0.001)
        if any(u.endswith(s) for s in ACRE_SUFFIXES):  return ("acre",  0.001)
        if any(u.endswith(s) for s in LITRE_SUFFIXES): return ("L",     0.001)
    return None


# Standard fertigation spray volume for g/L → kg/acre conversion
# (agronomist-confirmed, same constant the Soil Nutrient engine
# uses in soil_nutrient_mock.py).
_FOLIAR_SPRAY_VOLUME_L_PER_ACRE = 200.0


async def _dose_and_scope_for_practice(
    db: AsyncSession, practice: Practice, sub: Subscription,
) -> Optional[dict]:
    """Resolve the practice's authored dose + the subscription's
    matching field size into the per-nutrient totals in kg, plus a
    scope label the UI renders alongside.

    Returns dict with:
      - dose_total_kg: (n_kg, p_kg, k_kg)  totals across the farmer's field
      - scope_text:    "2 acres" | "1000 plants" | "500 L"  (plural-safe)
    or None when the unit can't be resolved / the matching field is None.
    """
    elements = (await db.execute(
        select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    by_type = {e.element_type: e for e in elements}

    def _val(et: str) -> float:
        el = by_type.get(et) or by_type.get(et.upper()) or by_type.get(et.lower())
        if el is None or el.value is None:
            return 0.0
        try:
            return float(el.value)
        except (TypeError, ValueError):
            return 0.0

    n_raw = _val("N_DOSAGE")
    p_raw = _val("P_DOSAGE")
    k_raw = _val("K_DOSAGE")

    unit = await _resolve_unit_label(db, by_type)
    classified = _classify_unit(unit)
    # Default fallback: if the UNIT element is missing / unrecognised,
    # assume kg/acre for area subscriptions, kg/plant for plant-wise.
    # This keeps old authoring (pre-UNIT) from silently producing
    # zero quantities.
    if classified is None:
        if sub.farm_area_acres is not None:
            classified = ("acre", 1.0)
        elif sub.number_of_plants is not None:
            classified = ("plant", 1.0)
        else:
            return None
    scope_kind, mass_scale = classified

    # Pick the field size based on scope kind.
    if scope_kind == "acre":
        if sub.farm_area_acres is None:
            return None
        area_f = (
            float(sub.farm_area_acres)
            if isinstance(sub.farm_area_acres, Decimal)
            else sub.farm_area_acres
        )
        if area_f <= 0:
            return None
        scope_count = area_f
        scope_label = f"{_fmt_num(area_f)} {'acre' if area_f == 1 else 'acres'}"
    elif scope_kind == "plant":
        if not sub.number_of_plants or sub.number_of_plants <= 0:
            return None
        scope_count = float(sub.number_of_plants)
        scope_label = (
            f"{sub.number_of_plants} "
            f"{'plant' if sub.number_of_plants == 1 else 'plants'}"
        )
    elif scope_kind == "L":
        # Fertigation g/L: compute as L-of-spray-water, using the
        # 200 L/acre standard against the acre-based subscription.
        if sub.farm_area_acres is None:
            return None
        area_f = (
            float(sub.farm_area_acres)
            if isinstance(sub.farm_area_acres, Decimal)
            else sub.farm_area_acres
        )
        if area_f <= 0:
            return None
        scope_count = _FOLIAR_SPRAY_VOLUME_L_PER_ACRE * area_f
        scope_label = f"{_fmt_num(area_f)} {'acre' if area_f == 1 else 'acres'}"
    else:
        return None

    return {
        "dose_total_kg": (
            n_raw * scope_count * mass_scale,
            p_raw * scope_count * mass_scale,
            k_raw * scope_count * mass_scale,
        ),
        "scope_text": scope_label,
    }


def _fmt_num(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return f"{v:g}"


async def _concentration_for_common_name(
    db: AsyncSession, common_name_cosh_id: str,
) -> Optional[tuple[float, float, float]]:
    """Return (n%, p%, k%) for the common_name, or None when Cosh has
    no concentration data for it. Walks the same path
    load_fertiliser_candidates uses — zero chance of divergence."""
    candidates, _ = await load_fertiliser_candidates(db, fertigation=False)
    for c in candidates:
        if c.cosh_id == common_name_cosh_id:
            return (c.concentration.n, c.concentration.p, c.concentration.k)
    return None


def _compute_kg_from_role(
    dose_total: tuple[float, float, float],
    conc: tuple[float, float, float],
    product_role: str,
    match_target: Optional[str],
) -> Optional[float]:
    """Pure math. dose_total is already (N, P, K) kg across the full
    field (dose × scope × mass_scale handled upstream). Returns the
    kg of product the farmer should apply."""
    dose_n, dose_p, dose_k = dose_total
    conc_n, conc_p, conc_k = conc
    role_to_pair = {
        "STRAIGHT_N": (dose_n, conc_n),
        "STRAIGHT_P": (dose_p, conc_p),
        "STRAIGHT_K": (dose_k, conc_k),
    }
    if product_role in role_to_pair:
        dose_nutrient, pct = role_to_pair[product_role]
    elif product_role == "MIXED":
        if match_target == "N":
            dose_nutrient, pct = dose_n, conc_n
        elif match_target == "P":
            dose_nutrient, pct = dose_p, conc_p
        elif match_target == "K":
            dose_nutrient, pct = dose_k, conc_k
        else:
            return None
    else:
        return None
    if pct <= 0 or dose_nutrient <= 0:
        return None
    return dose_nutrient / (pct / 100.0)


async def compute_apply_kg(
    db: AsyncSession, purchase: NpkDosagePurchase,
) -> Optional[dict]:
    """Convenience wrapper: given a purchase record, return
    {'apply_kg': float, 'scope_text': str} or None when any input is
    unresolvable. The PWA renders "{brand} — ~{apply_kg} kg for your
    {scope_text}" (e.g. "Spic Urea — ~22 kg for your 2 acres" or
    "BHARAT UREA — ~22 kg for your 1000 plants")."""
    sub = await db.get(Subscription, purchase.subscription_id)
    if sub is None:
        return None

    practice = await db.get(Practice, purchase.practice_id)
    if practice is None or practice.l2_type not in NPK_DOSAGE_L2_TYPES:
        return None

    dose_scope = await _dose_and_scope_for_practice(db, practice, sub)
    if dose_scope is None:
        return None

    conc = await _concentration_for_common_name(db, purchase.common_name_cosh_id)
    if conc is None:
        return None

    kg = _compute_kg_from_role(
        dose_scope["dose_total_kg"], conc,
        purchase.product_role, purchase.match_target,
    )
    if kg is None:
        return None

    return {"apply_kg": kg, "scope_text": dose_scope["scope_text"]}
