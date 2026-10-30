"""Shared NPK options resolver — used by both dealer and farmer surfaces.

Extracted from `app/modules/orders/router.py::get_item_npk_options` on
2026-10-30 when the Advisory-Only Mode farmer-side Brands flow was
built. The dealer and farmer endpoints share the same core logic
(given a Practice, compute the required_dose → rank the Mixed
candidates → compute enabled Straights for the remaining gap). The
two paths differ only in auth/scoping and in whether they expose
`applications_multiplier` for a Fertigation PO (dealer buys for the
whole timeline; farmer in Advisory-Only Mode just sees the per-
application dose). This module keeps the agronomic logic in one
place so the two endpoints can't drift.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import Element, Practice
from app.services.npk_candidates import load_fertiliser_candidates
from app.services.npk_ranking import (
    Dose,
    classify_fertiliser,
    compute_gap_after_mixed,
    enabled_straights as npk_enabled_straights,
    rank_mixed,
    straight_kg_for_gap,
)


NPK_L2_TYPES = frozenset({
    "CHEMICAL_FERTILIZERS_NPK_DOSAGES",
    "FERTIGATION_NPK_DOSAGES",
})


def _round_or_none(v: Optional[float]) -> Optional[float]:
    return round(v, 2) if v is not None else None


def _candidate_to_dict(cand) -> dict:
    return {
        "cosh_id": cand.cosh_id,
        "name": cand.name,
        "n": cand.concentration.n,
        "p": cand.concentration.p,
        "k": cand.concentration.k,
        "class": classify_fertiliser(cand.concentration),
        "water_soluble": cand.water_soluble,
    }


def _val(by_type: dict, element_type: str) -> float:
    el = by_type.get(element_type) \
        or by_type.get(element_type.upper()) \
        or by_type.get(element_type.lower())
    if el is None or el.value is None:
        return 0.0
    try:
        return float(el.value)
    except (TypeError, ValueError):
        return 0.0


async def resolve_npk_options(
    db: AsyncSession,
    practice: Practice,
    *,
    picked_mixed_cosh_id: Optional[str] = None,
    applications_multiplier: int = 1,
) -> dict:
    """Pure resolver: given a Practice (must be one of NPK_L2_TYPES),
    returns the full ranked-Mixed + enabled-Straights payload.

    Caller is responsible for:
      - Auth / scoping (dealer vs farmer vs admin).
      - Computing `applications_multiplier` for Fertigation if the
        caller wants per-timeline totals (dealer flow). Pass 1 for the
        advisory per-application view (farmer flow).
      - Rejecting non-NPK practices BEFORE calling this — we assume
        the caller has already validated `practice.l2_type in
        NPK_L2_TYPES` and want to return their own error shape.
    """
    elements = (await db.execute(
        select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    by_type = {e.element_type: e for e in elements}

    dose = Dose(
        n=_val(by_type, "N_DOSAGE"),
        p=_val(by_type, "P_DOSAGE"),
        k=_val(by_type, "K_DOSAGE"),
    )
    fertigation = practice.l2_type == "FERTIGATION_NPK_DOSAGES"

    candidates, skipped = await load_fertiliser_candidates(
        db, fertigation=fertigation,
    )
    ranked = rank_mixed(candidates, dose, water_soluble_only=fertigation)

    picked = None
    if picked_mixed_cosh_id:
        for r in ranked:
            if r.candidate.cosh_id == picked_mixed_cosh_id:
                picked = r
                break

    gap = compute_gap_after_mixed(dose, picked)
    straights = npk_enabled_straights(
        gap, candidates, water_soluble_only=fertigation,
    )
    straights.sort(key=lambda c: c.name.lower())

    return {
        "is_npk_practice": True,
        "fertigation": fertigation,
        "applications_multiplier": applications_multiplier,
        "required_dose": {"n": dose.n, "p": dose.p, "k": dose.k},
        "ranked_mixed": [
            {
                "cosh_id": r.candidate.cosh_id,
                "name": r.candidate.name,
                "n": r.candidate.concentration.n,
                "p": r.candidate.concentration.p,
                "k": r.candidate.concentration.k,
                "kg_product": round(r.kg_product, 2),
                "kg_product_total": round(
                    r.kg_product * applications_multiplier, 2,
                ),
                "delivered": {
                    "n": round(r.best.n_delivered, 2),
                    "p": round(r.best.p_delivered, 2),
                    "k": round(r.best.k_delivered, 2),
                },
                "match_target": r.best.target,
                "total_delivered": round(r.total_delivered, 2),
            }
            for r in ranked
        ],
        "enabled_straights": [
            {
                **_candidate_to_dict(c),
                # kg of this straight needed to close the gap for its
                # one nutrient (None if the Straight's class doesn't
                # match any remaining gap — shouldn't happen since
                # npk_enabled_straights already filters, but defensive).
                "kg_product_needed": _round_or_none(
                    straight_kg_for_gap(c.concentration, gap),
                ),
            }
            for c in straights
        ],
        "gap": {"n": gap.n, "p": gap.p, "k": gap.k},
        "cosh_skipped_count": skipped,
    }
