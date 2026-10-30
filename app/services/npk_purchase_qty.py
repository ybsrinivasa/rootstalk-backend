"""NPK Dosage Purchase quantity computation (2026-10-30).

Given a farmer's purchase record (brand they bought) + the practice's
authored NPK dose + the subscription's farm area, computes how many
kg of the specific product the farmer should apply.

Formula per product role:
  - STRAIGHT_N: kg = (dose.n × area) / (concentration.n / 100)
  - STRAIGHT_P: kg = (dose.p × area) / (concentration.p / 100)
  - STRAIGHT_K: kg = (dose.k × area) / (concentration.k / 100)
  - MIXED:     anchor on `match_target` (N/P/K) stored at purchase
               time; same arithmetic on the target nutrient. Matches
               `rank_mixed`'s kg_product × area semantic the farmer
               saw at pick-time.

Fail-silent: if any input is missing (no area, no Cosh concentration,
no match_target on a MIXED row), returns None and the caller omits
the "apply X kg" line entirely. Zero empty-card garbage.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import Element, Practice
from app.modules.subscriptions.models import NpkDosagePurchase, Subscription
from app.services.npk_candidates import load_fertiliser_candidates


# L2 types the quantity helper is active for.
NPK_DOSAGE_L2_TYPES = frozenset({
    "CHEMICAL_FERTILIZERS_NPK_DOSAGES",
    "FERTIGATION_NPK_DOSAGES",
})


async def _dose_for_practice(
    db: AsyncSession, practice_id: str,
) -> Optional[tuple[float, float, float]]:
    """Return (n, p, k) kg/acre authored on the practice elements, or
    None when the practice isn't an NPK dosage practice."""
    elements = (await db.execute(
        select(Element).where(Element.practice_id == practice_id)
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

    return (_val("N_DOSAGE"), _val("P_DOSAGE"), _val("K_DOSAGE"))


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
    dose: tuple[float, float, float],
    conc: tuple[float, float, float],
    product_role: str,
    match_target: Optional[str],
    area_acres: float,
) -> Optional[float]:
    """Pure math. Returns kg of product the farmer should apply across
    `area_acres` acres given the dose + concentration + role."""
    dose_n, dose_p, dose_k = dose
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
    if pct <= 0 or dose_nutrient <= 0 or area_acres <= 0:
        return None
    return (dose_nutrient * area_acres) / (pct / 100.0)


async def compute_apply_kg(
    db: AsyncSession, purchase: NpkDosagePurchase,
) -> Optional[float]:
    """Convenience wrapper: given a purchase record, return kg of
    product the farmer should apply (None if any input unresolvable).
    Looks up the practice, the subscription's area, and the Cosh
    concentration for the common_name."""
    sub = await db.get(Subscription, purchase.subscription_id)
    if sub is None or sub.farm_area_acres is None:
        return None
    area = (
        float(sub.farm_area_acres)
        if isinstance(sub.farm_area_acres, Decimal)
        else sub.farm_area_acres
    )

    practice = await db.get(Practice, purchase.practice_id)
    if practice is None or practice.l2_type not in NPK_DOSAGE_L2_TYPES:
        return None

    dose = await _dose_for_practice(db, purchase.practice_id)
    if dose is None:
        return None

    conc = await _concentration_for_common_name(db, purchase.common_name_cosh_id)
    if conc is None:
        return None

    return _compute_kg_from_role(
        dose, conc, purchase.product_role, purchase.match_target, area,
    )
