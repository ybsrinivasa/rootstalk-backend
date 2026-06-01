"""NPK fertiliser ranking — Mixed / Straight matching algorithm.

Implements the spec in RootsTalk_NPK_Handling.pdf (Apr 2026):

  - Mixed fertilisers supply two or all three of (N, P, K) at a fixed
    concentration. Straight fertilisers supply exactly one.
  - For a required (req_N, req_P, req_K), each Mixed runs three
    matching tests (target N, target P, target K). A match is VALID
    iff the resulting supply of the other two nutrients does not
    EXCEED the required amount. Exact match or less only.
  - Per Mixed: pick the single best valid match by
      primary  = highest total kg of N+P+K delivered (capped per
                 nutrient at req — excess is invalid anyway)
      tiebreak = least kg of product needed.
  - Rank all qualifying Mixeds descending on the same criteria.
  - After the dealer picks one (or zero) Mixed, enable Straight
    fertilisers whose nutrient still has a gap.

Pure functions, no DB. Cosh layer feeds in concentrations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

# The spec calls out three forms of Straight; we use those + MIXED.
FertiliserClass = Literal["STRAIGHT_N", "STRAIGHT_P", "STRAIGHT_K", "MIXED"]


@dataclass(frozen=True)
class Concentration:
    """N / P / K per 100 units of product. Example: Urea 46:0:0 →
    Concentration(46, 0, 0). All values clamped non-negative."""
    n: float
    p: float
    k: float

    def __post_init__(self) -> None:
        for label, v in (("n", self.n), ("p", self.p), ("k", self.k)):
            if v < 0:
                raise ValueError(f"{label} percentage must be >= 0, got {v}")


@dataclass(frozen=True)
class Dose:
    """Required nutrient kg the SE specified."""
    n: float
    p: float
    k: float


@dataclass(frozen=True)
class Candidate:
    """A fertiliser product up for ranking. `cosh_id` keys back to the
    Common Name in Cosh (the dealer later picks a brand under it)."""
    cosh_id: str
    name: str
    concentration: Concentration
    water_soluble: bool = True


@dataclass(frozen=True)
class MatchOutcome:
    """Result of one of the three matching tests for a Mixed."""
    target: Literal["N", "P", "K"]
    valid: bool
    # kg of product needed to deliver exactly target_dose of the
    # primary nutrient. None when n/p/k % is zero (test inapplicable).
    kg_product: Optional[float]
    # kg of each nutrient that this kg_product would actually
    # deliver (capped at req for ranking purposes — excess invalidates).
    n_delivered: float
    p_delivered: float
    k_delivered: float
    # Reason string when invalid — useful for debugging in tests but
    # never surfaced to the dealer (per spec, dealer sees no math).
    invalid_reason: Optional[str] = None


@dataclass(frozen=True)
class MixedRanking:
    """Best match for a single Mixed, ready to sort."""
    candidate: Candidate
    best: MatchOutcome  # one of N/P/K, guaranteed valid
    total_delivered: float  # primary sort key (desc)
    # Same kg_product as best.kg_product but de-Optional'd; secondary
    # sort key (asc).
    kg_product: float


def classify_fertiliser(c: Concentration) -> FertiliserClass:
    """Spec §1.1: Mixed = two or more nutrients; Straight = exactly one."""
    bits = (1 if c.n > 0 else 0) + (1 if c.p > 0 else 0) + (1 if c.k > 0 else 0)
    if bits >= 2:
        return "MIXED"
    if c.n > 0:
        return "STRAIGHT_N"
    if c.p > 0:
        return "STRAIGHT_P"
    if c.k > 0:
        return "STRAIGHT_K"
    raise ValueError("Concentration with all-zero NPK is not a fertiliser")


def _run_one_test(
    target: Literal["N", "P", "K"], c: Concentration, dose: Dose,
) -> MatchOutcome:
    """The N/P/K-match calculation from §2.1 Step 1."""
    target_pct = {"N": c.n, "P": c.p, "K": c.k}[target]
    target_dose = {"N": dose.n, "P": dose.p, "K": dose.k}[target]

    # Spec: the target nutrient's percentage must be > 0 for the test to
    # even apply. (You can't "deliver N exactly" with a 0% N product.)
    # A 0-dose target is also degenerate — exact match is 0 kg of
    # product, which delivers 0 of everything; treat it as inapplicable
    # so it doesn't pollute the ranking with bogus zero-volume entries.
    if target_pct <= 0 or target_dose <= 0:
        return MatchOutcome(
            target=target, valid=False, kg_product=None,
            n_delivered=0.0, p_delivered=0.0, k_delivered=0.0,
            invalid_reason="target nutrient absent or dose zero",
        )

    # kg of product to deliver exactly target_dose of the target nutrient.
    kg = 100.0 * target_dose / target_pct
    n_del = kg * c.n / 100.0
    p_del = kg * c.p / 100.0
    k_del = kg * c.k / 100.0

    # Validity: every *other* nutrient supplied must not EXCEED req. A
    # small floating-point slack guards against round-trip imprecision
    # (eg., 50.0 == 50.0 + 1e-12) — the dealer-facing rule stays exact.
    eps = 1e-6
    if target != "N" and n_del - dose.n > eps:
        return MatchOutcome(
            target, False, kg, n_del, p_del, k_del,
            f"excess N: would supply {n_del:.4f} vs req {dose.n:.4f}",
        )
    if target != "P" and p_del - dose.p > eps:
        return MatchOutcome(
            target, False, kg, n_del, p_del, k_del,
            f"excess P: would supply {p_del:.4f} vs req {dose.p:.4f}",
        )
    if target != "K" and k_del - dose.k > eps:
        return MatchOutcome(
            target, False, kg, n_del, p_del, k_del,
            f"excess K: would supply {k_del:.4f} vs req {dose.k:.4f}",
        )

    # Cap delivered values at req for ranking purposes. Excess would have
    # invalidated above; this clamps tiny round-off so the ranking key
    # is exact.
    return MatchOutcome(
        target=target, valid=True, kg_product=kg,
        n_delivered=min(n_del, dose.n),
        p_delivered=min(p_del, dose.p),
        k_delivered=min(k_del, dose.k),
    )


def run_three_tests(c: Concentration, dose: Dose) -> list[MatchOutcome]:
    """Spec §2.1 Step 1 — N/P/K-match independently."""
    return [_run_one_test(t, c, dose) for t in ("N", "P", "K")]


def best_valid_match(matches: Sequence[MatchOutcome]) -> Optional[MatchOutcome]:
    """Spec §2.1 Step 2 — pick the single best valid match for one
    Mixed. Returns None if no test was valid (fertiliser excluded)."""
    valid = [m for m in matches if m.valid]
    if not valid:
        return None
    # Primary: most total nutrients delivered. Tiebreak: least kg.
    return max(
        valid,
        key=lambda m: (
            m.n_delivered + m.p_delivered + m.k_delivered,
            -(m.kg_product or 0.0),  # negate for asc-on-tie
        ),
    )


def rank_mixed(
    candidates: Sequence[Candidate], dose: Dose,
    *, water_soluble_only: bool = False,
) -> list[MixedRanking]:
    """Spec §2.1 Step 3 — rank all qualifying Mixeds descending.

    `water_soluble_only=True` filters for the Fertigation flow (§5.1).
    Non-Mixed candidates and Mixeds with zero valid tests are excluded.
    """
    out: list[MixedRanking] = []
    for cand in candidates:
        if classify_fertiliser(cand.concentration) != "MIXED":
            continue
        if water_soluble_only and not cand.water_soluble:
            continue
        matches = run_three_tests(cand.concentration, dose)
        best = best_valid_match(matches)
        if best is None or best.kg_product is None:
            continue
        out.append(MixedRanking(
            candidate=cand,
            best=best,
            total_delivered=(
                best.n_delivered + best.p_delivered + best.k_delivered
            ),
            kg_product=best.kg_product,
        ))
    # Descending by total, ascending by kg as tiebreak (spec §2.1 Step 2
    # extended across all Mixeds).
    out.sort(key=lambda r: (-r.total_delivered, r.kg_product))
    return out


def compute_gap_after_mixed(
    dose: Dose, picked: Optional[MixedRanking],
) -> Dose:
    """Spec §2.3 — how much of each nutrient is still owed after the
    dealer selects (or skips) a Mixed."""
    if picked is None:
        return dose
    return Dose(
        n=max(0.0, dose.n - picked.best.n_delivered),
        p=max(0.0, dose.p - picked.best.p_delivered),
        k=max(0.0, dose.k - picked.best.k_delivered),
    )


def enabled_straights(
    gap: Dose, candidates: Sequence[Candidate],
    *, water_soluble_only: bool = False,
) -> list[Candidate]:
    """Spec §2.3 — Straight fertilisers whose nutrient still has a gap.

    Order in the returned list mirrors the input — caller sorts
    alphabetically per UI spec.
    """
    eps = 1e-6
    needs = {
        "STRAIGHT_N": gap.n > eps,
        "STRAIGHT_P": gap.p > eps,
        "STRAIGHT_K": gap.k > eps,
    }
    out: list[Candidate] = []
    for cand in candidates:
        cls = classify_fertiliser(cand.concentration)
        if cls == "MIXED":
            continue
        if water_soluble_only and not cand.water_soluble:
            continue
        if needs.get(cls, False):
            out.append(cand)
    return out


def straight_kg_for_gap(c: Concentration, gap: Dose) -> Optional[float]:
    """Given a Straight + the remaining gap, how many kg of product
    delivers exactly the gap of its one nutrient? `None` if the
    Straight's nutrient isn't needed."""
    cls = classify_fertiliser(c)
    if cls == "STRAIGHT_N" and c.n > 0 and gap.n > 0:
        return 100.0 * gap.n / c.n
    if cls == "STRAIGHT_P" and c.p > 0 and gap.p > 0:
        return 100.0 * gap.p / c.p
    if cls == "STRAIGHT_K" and c.k > 0 and gap.k > 0:
        return 100.0 * gap.k / c.k
    return None
