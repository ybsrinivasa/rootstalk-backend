"""Soil Nutrient engine core — pure functions.

Shared between the Mock (Phase 7) and the Real engine (future phase).
The Mock router loads all DB inputs, calls these functions, and
returns the Decision Report inline. The Real engine will do the same
plus write overlay rows (Decision R) and fire SE/CA notifications
(Decision S). The *compute* half — classification, target
adjustment, candidate ranking, Report assembly — is identical and
lives here so there's one place to audit the engine logic.

Data flow (per invocation):

    SoilValues (measured) + EffectiveRanges (merged Package→crop)
            │
            ▼
    classify_soil_values()  →  list[Classification]
            │                       (Low / Optimum / High / NOT_CLASSIFIED
            │                        / NO_MEASUREMENT)
            ▼
    compute_target_adjustments()  →  dict[parameter_name → signed pct]
            │                        (Decision V — +X% raise, -X% lower)
            ▼
    rank_candidates()      →  dict[nutrient_letter → RankedCandidates]
      with Tier 1 (Client Products) → Tier 2 (Fallbacks) →
      Tier 3 (pure straights). Each tier ordered by nutrient
      concentration (desc) so the engine picks the most efficient
      first.
            ▼
    build_decision_report()  →  DecisionReport (six sections)

All functions here are pure — no DB access, no I/O. The Mock router
loads everything from DB, converts to these dataclasses, calls these
in sequence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ── Input shapes ──────────────────────────────────────────────────────────

class NormalRangeOperator(str, Enum):
    """Mirror of the SQLAlchemy enum. Duplicated here so this module
    has zero DB imports — keeps the engine testable in isolation."""
    RANGE = "RANGE"
    UPPER_BOUND = "UPPER_BOUND"
    LOWER_BOUND = "LOWER_BOUND"


@dataclass(frozen=True)
class SoilValue:
    """One measured parameter value for the soil-under-analysis."""
    parameter_id: str
    parameter_name: str
    value: float


@dataclass(frozen=True)
class EffectiveRange:
    """The Normal Range the engine uses for one parameter — already
    merged from Package override (if any) and crop default (else)."""
    parameter_id: str
    parameter_name: str
    operator: NormalRangeOperator
    low_value: Optional[float]
    high_value: Optional[float]
    low_pct_adjustment: float   # Decision V
    high_pct_adjustment: float  # Decision V
    source: str  # "package" | "crop" — for the Report


@dataclass(frozen=True)
class CandidateProduct:
    """A fertilizer the engine can recommend. Tier is set by the
    router when converting ClientProduct / FallbackProduct / pure
    straight into this shape."""
    cosh_id: str
    name: str
    n: float      # grams N per kg product
    p: float      # grams P2O5 per kg product
    k: float      # grams K2O per kg product
    water_soluble: bool
    tier: int     # 1 = Client, 2 = Fallback, 3 = pure straight


# ── Output shapes ─────────────────────────────────────────────────────────

class Classification(str, Enum):
    LOW = "LOW"
    OPTIMUM = "OPTIMUM"
    HIGH = "HIGH"
    # The parameter has a measurement but no Normal Range row (nothing
    # to classify against) — show "no classification available" in the
    # Report.
    NOT_CLASSIFIED = "NOT_CLASSIFIED"
    # The Client authored a Normal Range for this parameter but the
    # soil input didn't carry a measurement — show "no measurement
    # provided" in the Report (matches partial-lab-report behaviour).
    NO_MEASUREMENT = "NO_MEASUREMENT"


@dataclass(frozen=True)
class ClassificationResult:
    parameter_id: str
    parameter_name: str
    classification: Classification
    measured_value: Optional[float]
    low_bound: Optional[float]
    high_bound: Optional[float]
    range_source: Optional[str]  # "package" / "crop" / None


@dataclass(frozen=True)
class TargetAdjustment:
    """What Decision V said the target shift should be for this
    parameter. `None` when the classification doesn't trigger an
    adjustment (OPTIMUM) or isn't actionable (NOT_CLASSIFIED /
    NO_MEASUREMENT)."""
    parameter_id: str
    parameter_name: str
    classification: Classification
    pct: Optional[float]


@dataclass(frozen=True)
class RankedCandidates:
    """Per-nutrient candidate ranking across the three tiers."""
    nutrient: str  # "N" / "P" / "K"
    tier1: list[CandidateProduct]
    tier2: list[CandidateProduct]
    tier3: list[CandidateProduct]


# ── Phase 8 shapes (2026-10-22) ───────────────────────────────────────────
# The engine extends from "which products rank highest" to a Package-aware
# action plan: baseline per nutrient → delta from Decision V % → Tier 0
# proportional distribution over existing Practices → gap-case Instruction
# for Low nutrients the Package has no Practice for.

@dataclass(frozen=True)
class PracticeNutrientContribution:
    """One Practice's authored contribution to one or more nutrients, in
    kg of elemental nutrient per acre. Caller does all the parsing +
    unit conversion (NPK_DOSAGES → read elements; PRODUCTS → multiply
    DOSAGE × Cosh concentration; foliar g/L × 200 / 1000 → kg/acre)."""
    practice_id: str
    timeline_id: str
    timeline_name: str
    timeline_from_days: int   # anchor-day; for first-future detection
    l2_type: str
    display_label: str        # for Mock rendering, e.g. "Urea 9 kg/acre @ DAS 38–42"
    is_acknowledged: bool     # Decision H past-ack skip; Mock sets False
    contributions: dict[str, float]  # nutrient_letter → kg/acre
    # Cosh name of the APPLICATION_METHOD Element on the Practice
    # (e.g., "Direct Soil Application", "Foliar Spray"). Used by the
    # Product Reference ranking to filter candidates to the ones the SE
    # could actually swap in — a foliar Practice must only reference
    # water-soluble products. Empty string when no APPLICATION_METHOD
    # was authored.
    application_method: str = ""


@dataclass(frozen=True)
class BaselineComputation:
    """Package-level baseline per nutrient (sum across all Practice
    contributions). `per_nutrient` keys are the 11 letters covered by
    `_PARAM_NAME_TO_NUTRIENT_FULL`."""
    per_nutrient: dict[str, float]


@dataclass(frozen=True)
class Tier0Adjustment:
    """A single Practice's proposed modification for one nutrient.

    `scaled_kg_per_acre` is what the Decision V math says the new dose
    should be. `applied_kg_per_acre` is what the engine will actually
    use — equals scaled unless the Practice is past-acknowledged, in
    which case it equals original (Decision H: no retroactive edits,
    no redistribution of missed share to future Practices)."""
    practice_id: str
    timeline_name: str
    nutrient: str
    original_kg_per_acre: float
    scaled_kg_per_acre: float
    applied_kg_per_acre: float
    skipped_reason: Optional[str]  # "past-acknowledged" when frozen


@dataclass(frozen=True)
class GapInstruction:
    """A brand-new INSTRUCTION Practice to insert on the first-future
    Timeline, for Low-classified nutrients the Package carries no
    Practice for. Only fires for LOW; HIGH gap cases are skipped
    (no Practice = no over-application risk)."""
    nutrient: str       # letter: "Mg" / "Zn" etc.
    nutrient_name: str  # full name for the message: "Magnesium"
    message: str        # the agronomist-signed-off templated text
    place_on_timeline_id: Optional[str]
    place_on_timeline_name: Optional[str]


@dataclass(frozen=True)
class DecisionReportSection:
    heading: str
    body_md: str


@dataclass(frozen=True)
class DecisionReport:
    """Six-section report per Decision T. `body_md` on each section
    is Markdown — the SA/Client Portal UI renders it with the same
    renderer used elsewhere."""
    is_mock: bool
    generated_at: datetime
    client_id: str
    package_id: str
    crop_cosh_id: str
    sections: list[DecisionReportSection]


# ── Core functions ────────────────────────────────────────────────────────

def classify_soil_values(
    soil_values: list[SoilValue],
    ranges: list[EffectiveRange],
) -> list[ClassificationResult]:
    """Classify each parameter as Low / Optimum / High (or
    NOT_CLASSIFIED / NO_MEASUREMENT).

    Resolution:
      - parameter has both a value and a range → classify
      - parameter has a value but no range → NOT_CLASSIFIED
      - parameter has a range but no value → NO_MEASUREMENT
      - parameter has neither → not in the Report (silent)

    Within classification:
      RANGE        value < low → LOW; value > high → HIGH; else OPTIMUM.
      UPPER_BOUND  value > high → HIGH; else OPTIMUM. (No Low concept.)
      LOWER_BOUND  value < low → LOW;  else OPTIMUM. (No High concept.)
    """
    values_by_id = {v.parameter_id: v for v in soil_values}
    ranges_by_id = {r.parameter_id: r for r in ranges}
    all_param_ids = set(values_by_id) | set(ranges_by_id)
    results: list[ClassificationResult] = []

    for pid in all_param_ids:
        v = values_by_id.get(pid)
        r = ranges_by_id.get(pid)
        name = (v.parameter_name if v else r.parameter_name)  # type: ignore[union-attr]

        if v and not r:
            results.append(ClassificationResult(
                parameter_id=pid, parameter_name=name,
                classification=Classification.NOT_CLASSIFIED,
                measured_value=v.value, low_bound=None, high_bound=None,
                range_source=None,
            ))
            continue
        if r and not v:
            results.append(ClassificationResult(
                parameter_id=pid, parameter_name=name,
                classification=Classification.NO_MEASUREMENT,
                measured_value=None,
                low_bound=r.low_value, high_bound=r.high_value,
                range_source=r.source,
            ))
            continue

        assert v is not None and r is not None  # for type-checkers
        clazz = Classification.OPTIMUM
        if r.operator == NormalRangeOperator.RANGE:
            if v.value < (r.low_value or 0):
                clazz = Classification.LOW
            elif v.value > (r.high_value or 0):
                clazz = Classification.HIGH
        elif r.operator == NormalRangeOperator.UPPER_BOUND:
            if r.high_value is not None and v.value > r.high_value:
                clazz = Classification.HIGH
        elif r.operator == NormalRangeOperator.LOWER_BOUND:
            if r.low_value is not None and v.value < r.low_value:
                clazz = Classification.LOW

        results.append(ClassificationResult(
            parameter_id=pid, parameter_name=name,
            classification=clazz, measured_value=v.value,
            low_bound=r.low_value, high_bound=r.high_value,
            range_source=r.source,
        ))

    # Deterministic order — follows parameter name alphabetically. The
    # UI applies display_order from PlatformSoilParameter for the final
    # render.
    results.sort(key=lambda c: c.parameter_name.lower())
    return results


def compute_target_adjustments(
    classifications: list[ClassificationResult],
    ranges: list[EffectiveRange],
) -> list[TargetAdjustment]:
    """Apply Decision V's % adjustment per classification.

    - LOW → low_pct_adjustment (typically positive — raise the target)
    - HIGH → high_pct_adjustment (typically negative — lower it)
    - OPTIMUM → pct = 0.0 (included in output so UI can show "no shift")
    - NOT_CLASSIFIED / NO_MEASUREMENT → pct = None (nothing to apply)
    """
    ranges_by_id = {r.parameter_id: r for r in ranges}
    out: list[TargetAdjustment] = []
    for c in classifications:
        r = ranges_by_id.get(c.parameter_id)
        pct: Optional[float]
        if c.classification == Classification.LOW:
            pct = r.low_pct_adjustment if r else None
        elif c.classification == Classification.HIGH:
            pct = r.high_pct_adjustment if r else None
        elif c.classification == Classification.OPTIMUM:
            pct = 0.0
        else:
            pct = None
        out.append(TargetAdjustment(
            parameter_id=c.parameter_id,
            parameter_name=c.parameter_name,
            classification=c.classification,
            pct=pct,
        ))
    return out


# Parameter names (from PlatformSoilParameter.name) that the engine
# treats as the primary NPK axis. Mapped to nutrient letters for the
# ranking step. Any other parameter the Client authors (secondaries,
# micros, pH) classifies and surfaces in the Report but doesn't drive
# NPK candidate ranking in v1 — Decision C's complex-rule-type covers
# those in a later phase.
#
# Names must match the seed data in
# alembic/versions/d2a0f574e215_soil_nutrient_phase1_seed.py exactly —
# the Indian soil-health-card vocabulary uses "Phosphorous" (with the
# extra 'o') and "Potash" (not Potassium). Prior values here were
# generic chemistry names and produced false negatives — P and K rows
# were skipped from candidate ranking and incorrectly counted as
# "non-NPK" in Section 6 warnings.
_PARAM_NAME_TO_NUTRIENT = {
    "Available Nitrogen": "N",
    "Available Phosphorous": "P",
    "Available Potash": "K",
}


# ── Phase 8 maps (2026-10-22) ─────────────────────────────────────────────
# Full 11-nutrient map used by Phase 8's baseline + Tier 0 + gap-case
# Instruction logic. Separate from _PARAM_NAME_TO_NUTRIENT above (which
# stays NPK-only because rank_candidates can only key off the n/p/k
# fields on CandidateProduct — secondary/micro concentrations aren't
# in the Cosh fert_nutrient_concentration Connect yet).
_PARAM_NAME_TO_NUTRIENT_FULL = {
    "Available Nitrogen":   "N",
    "Available Phosphorous": "P",
    "Available Potash":     "K",
    "Available Calcium":    "Ca",
    "Available Magnesium":  "Mg",
    "Available Sulphur":    "S",
    "Available Zinc":       "Zn",
    "Available Iron":       "Fe",
    "Available Manganese":  "Mn",
    "Available Copper":     "Cu",
    "Available Boron":      "B",
}

_NUTRIENT_LETTER_TO_NAME = {
    "N":  "Nitrogen",
    "P":  "Phosphorus",
    "K":  "Potassium",
    "Ca": "Calcium",
    "Mg": "Magnesium",
    "S":  "Sulphur",
    "Zn": "Zinc",
    "Fe": "Iron",
    "Mn": "Manganese",
    "Cu": "Copper",
    "B":  "Boron",
}

# Agronomist-signed-off messages (2026-10-22). Each describes the
# visible symptoms, then routes the farmer through the Diagnosis
# feature with a confirm-before-act gate so false signals don't force
# an unnecessary application.
_GAP_INSTRUCTION_MESSAGES = {
    "N":  "The soil report suggests Nitrogen may be insufficient. Watch for pale yellow older leaves, stunted growth, or thin stems. If you notice these symptoms, open the Diagnosis feature. If it confirms the deficiency, follow the recommended plan.",
    "P":  "The soil report suggests Phosphorus may be insufficient. Watch for purplish or dark-green older leaves, poor root development, or delayed flowering. If you notice these symptoms, open the Diagnosis feature. If it confirms the deficiency, follow the recommended plan.",
    "K":  "The soil report suggests Potassium may be insufficient. Watch for scorched or browning leaf edges on older leaves, weak stems, or lodging. If you notice these symptoms, open the Diagnosis feature. If it confirms the deficiency, follow the recommended plan.",
    "Ca": "The soil report suggests Calcium may be insufficient. Watch for distorted young leaves, blossom-end rot on fruits, or hollow stems. If you notice these symptoms, open the Diagnosis feature. If it confirms the deficiency, follow the recommended plan.",
    "Mg": "The soil report suggests Magnesium may be insufficient. Watch for interveinal yellowing on older leaves (the leaf veins stay green). If you notice these symptoms, open the Diagnosis feature. If it confirms the deficiency, follow the recommended plan.",
    "S":  "The soil report suggests Sulphur may be insufficient. Watch for pale young leaves (unlike Nitrogen, where older leaves pale first). If you notice these symptoms, open the Diagnosis feature. If it confirms the deficiency, follow the recommended plan.",
    "Zn": "The soil report suggests Zinc may be insufficient. Watch for small leaves, shortened internodes, or bronzing on young leaves. If you notice these symptoms, open the Diagnosis feature. If it confirms the deficiency, follow the recommended plan.",
    "Fe": "The soil report suggests Iron may be insufficient. Watch for interveinal yellowing on young leaves (opposite of Magnesium — young leaves first). If you notice these symptoms, open the Diagnosis feature. If it confirms the deficiency, follow the recommended plan.",
    "Mn": "The soil report suggests Manganese may be insufficient. Watch for yellowing between veins on young-to-middle leaves, often with brown speckling. If you notice these symptoms, open the Diagnosis feature. If it confirms the deficiency, follow the recommended plan.",
    "Cu": "The soil report suggests Copper may be insufficient. Watch for wilting, curling, or dieback of young leaves and shoot tips. If you notice these symptoms, open the Diagnosis feature. If it confirms the deficiency, follow the recommended plan.",
    "B":  "The soil report suggests Boron may be insufficient. Watch for hollow or cracked stems, death of growing points, or poor flower/fruit set. If you notice these symptoms, open the Diagnosis feature. If it confirms the deficiency, follow the recommended plan.",
}


def compute_baseline_per_nutrient(
    contributions: list[PracticeNutrientContribution],
) -> BaselineComputation:
    """Sum each Practice's nutrient contributions into Package-level
    baselines. Returns kg of elemental nutrient per acre, keyed by
    the nutrient letter (N / P / K / Ca / Mg / S / Zn / Fe / Mn /
    Cu / B).

    Agronomist-confirmed (2026-10-22): summing per-nutrient
    contributions across all Practices equals the SE's prescribed
    total for the crop.
    """
    totals: dict[str, float] = {}
    for c in contributions:
        for nut, amount in c.contributions.items():
            if amount == 0.0:
                continue
            totals[nut] = totals.get(nut, 0.0) + amount
    return BaselineComputation(per_nutrient=totals)


def compute_tier0_plan(
    contributions: list[PracticeNutrientContribution],
    baseline: BaselineComputation,
    adjustments: list[TargetAdjustment],
) -> list[Tier0Adjustment]:
    """For each actionable Decision V adjustment, distribute the
    delta proportionally across all baseline Practices that carry
    that nutrient. Past-acknowledged Practices are skipped and their
    share is NOT redistributed (Decision H — over-application harms
    the crop + soil, so the farmer loses that share for this season).

    Agronomist-confirmed worked example (2026-10-22):
      Baseline 10 kg Basal + 5 kg Top-dress N = 15 kg total
      Low N +20% → delta 3 kg → proportional +2 Basal, +1 Top-dress
      Final: Basal 12, Top-dress 6. If Basal already applied:
      Basal frozen at 10, Top-dress stays at 6 (no +2 redistribution).
    """
    # Pick actionable adjustments only.
    adj_by_letter: dict[str, TargetAdjustment] = {}
    for adj in adjustments:
        letter = _PARAM_NAME_TO_NUTRIENT_FULL.get(adj.parameter_name)
        if letter is None:
            continue
        if adj.classification not in (Classification.LOW, Classification.HIGH):
            continue
        if adj.pct is None or adj.pct == 0.0:
            continue
        adj_by_letter[letter] = adj

    out: list[Tier0Adjustment] = []
    for letter, adj in adj_by_letter.items():
        baseline_total = baseline.per_nutrient.get(letter, 0.0)
        if baseline_total == 0.0:
            # Gap case — handled by compute_gap_instructions, not Tier 0.
            continue
        # Decision V: the % lands on the summed baseline.
        delta_total = baseline_total * ((adj.pct or 0.0) / 100.0)
        for c in contributions:
            share = c.contributions.get(letter, 0.0)
            if share == 0.0:
                continue
            # Proportional slice.
            per_practice_delta = delta_total * (share / baseline_total)
            scaled = share + per_practice_delta
            if c.is_acknowledged:
                applied = share
                skipped = "past-acknowledged"
            else:
                applied = scaled
                skipped = None
            out.append(Tier0Adjustment(
                practice_id=c.practice_id,
                timeline_name=c.timeline_name,
                nutrient=letter,
                original_kg_per_acre=share,
                scaled_kg_per_acre=scaled,
                applied_kg_per_acre=applied,
                skipped_reason=skipped,
            ))
    return out


def compute_gap_instructions(
    adjustments: list[TargetAdjustment],
    baseline: BaselineComputation,
    first_future_timeline_id: Optional[str],
    first_future_timeline_name: Optional[str],
) -> list[GapInstruction]:
    """For each nutrient classified LOW with zero baseline (no
    Practice in the Package carries it), emit a GapInstruction with
    the agronomist's nutrient-specific message. One Instruction per
    deficient nutrient. HIGH gap cases are skipped (no Practice =
    no over-application risk).

    `first_future_timeline_id` + name come from the caller — the
    earliest Timeline whose from_days > the soil-report-submission
    DAS. The engine positions the Instruction here so the farmer
    sees it on their next advisory cycle. May be None if the
    Package has no future Timelines (surface a warning in Section 6)."""
    out: list[GapInstruction] = []
    for adj in adjustments:
        letter = _PARAM_NAME_TO_NUTRIENT_FULL.get(adj.parameter_name)
        if letter is None:
            continue
        if adj.classification != Classification.LOW:
            continue
        if baseline.per_nutrient.get(letter, 0.0) != 0.0:
            continue
        message = _GAP_INSTRUCTION_MESSAGES.get(letter)
        if message is None:
            continue
        out.append(GapInstruction(
            nutrient=letter,
            nutrient_name=_NUTRIENT_LETTER_TO_NAME.get(letter, letter),
            message=message,
            place_on_timeline_id=first_future_timeline_id,
            place_on_timeline_name=first_future_timeline_name,
        ))
    return out


def rank_candidates(
    adjustments: list[TargetAdjustment],
    candidates: list[CandidateProduct],
) -> list[RankedCandidates]:
    """For each primary NPK nutrient whose target was shifted (either
    direction), rank available candidates by tier then concentration.

    v1 scoping: only N/P/K drive candidate ranking. OPTIMUM parameters
    produce no ranking entry (nothing to recommend). NOT_CLASSIFIED /
    NO_MEASUREMENT also produce no entry (we can't recommend without
    a signal).

    Within each tier the sort key is the nutrient concentration
    descending — the engine prefers the most efficient delivery for a
    given shift (fewer kg of product to meet the same gram target).
    Tier 3 (pure straights) is seeded by the router from well-known
    Cosh commons (urea / DAP / MOP) — this function just orders what
    it's given.
    """
    out: list[RankedCandidates] = []
    for adj in adjustments:
        nut = _PARAM_NAME_TO_NUTRIENT.get(adj.parameter_name)
        if nut is None:
            continue
        if adj.classification not in (Classification.LOW, Classification.HIGH):
            continue
        if adj.pct is None or adj.pct == 0.0:
            continue

        def has_nut(c: CandidateProduct) -> bool:
            return (
                (nut == "N" and c.n > 0)
                or (nut == "P" and c.p > 0)
                or (nut == "K" and c.k > 0)
            )

        def conc(c: CandidateProduct) -> float:
            return {"N": c.n, "P": c.p, "K": c.k}[nut]

        in_scope = [c for c in candidates if has_nut(c)]
        tier1 = sorted((c for c in in_scope if c.tier == 1), key=conc, reverse=True)
        tier2 = sorted((c for c in in_scope if c.tier == 2), key=conc, reverse=True)
        tier3 = sorted((c for c in in_scope if c.tier == 3), key=conc, reverse=True)
        out.append(RankedCandidates(
            nutrient=nut, tier1=tier1, tier2=tier2, tier3=tier3,
        ))
    return out


# ── Decision Report builder ───────────────────────────────────────────────

def _fmt_num(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1 and v == int(v):
        return str(int(v))
    return f"{v:g}"


def _classification_label(c: Classification) -> str:
    return {
        Classification.LOW: "**Low**",
        Classification.OPTIMUM: "Optimum",
        Classification.HIGH: "**High**",
        Classification.NOT_CLASSIFIED: "_not classified_",
        Classification.NO_MEASUREMENT: "_no measurement_",
    }[c]


def _section_summary(
    *, is_mock: bool, package_name: str, crop_name: str,
    client_name: str, preset_name: Optional[str],
    classifications: list[ClassificationResult],
) -> DecisionReportSection:
    counts = {c: 0 for c in Classification}
    for c in classifications:
        counts[c.classification] += 1
    header = "## 1. Summary\n\n"
    body = (
        f"- **Mode**: {'Sandbox (Mock)' if is_mock else 'Real'}\n"
        f"- **Client**: {client_name}\n"
        f"- **Crop**: {crop_name}\n"
        f"- **Package**: {package_name}\n"
    )
    if preset_name:
        body += f"- **Soil preset**: {preset_name}\n"
    body += (
        f"\n**Classifications**: "
        f"{counts[Classification.LOW]} Low · "
        f"{counts[Classification.OPTIMUM]} Optimum · "
        f"{counts[Classification.HIGH]} High · "
        f"{counts[Classification.NOT_CLASSIFIED]} unclassified · "
        f"{counts[Classification.NO_MEASUREMENT]} missing measurement"
    )
    return DecisionReportSection(heading="Summary", body_md=header + body)


def _section_soil_interpretation(
    classifications: list[ClassificationResult],
) -> DecisionReportSection:
    header = "## 2. Soil Interpretation\n\n"
    if not classifications:
        return DecisionReportSection(
            heading="Soil Interpretation",
            body_md=header + "_No parameters supplied._",
        )
    rows = ["| Parameter | Measured | Low | High | Classification | Range from |",
            "|-----------|---------:|----:|-----:|:--------------:|:-----------|"]
    for c in classifications:
        rows.append(
            f"| {c.parameter_name} "
            f"| {_fmt_num(c.measured_value)} "
            f"| {_fmt_num(c.low_bound)} "
            f"| {_fmt_num(c.high_bound)} "
            f"| {_classification_label(c.classification)} "
            f"| {c.range_source or '—'} |"
        )
    return DecisionReportSection(
        heading="Soil Interpretation",
        body_md=header + "\n".join(rows),
    )


def _section_rules_applied(
    adjustments: list[TargetAdjustment],
) -> DecisionReportSection:
    header = "## 3. Rules Applied\n\n"
    actionable = [a for a in adjustments
                  if a.classification in (Classification.LOW, Classification.HIGH)
                  and a.pct is not None and a.pct != 0.0]
    if not actionable:
        return DecisionReportSection(
            heading="Rules Applied",
            body_md=header + "_No parameter triggered a target adjustment._",
        )
    lines = []
    for a in actionable:
        direction = "raise" if (a.pct or 0) > 0 else "lower"
        lines.append(
            f"- **{a.parameter_name}** classified "
            f"{_classification_label(a.classification)} → "
            f"{direction} target by **{abs(a.pct or 0):g}%** "
            f"(Decision V)"
        )
    return DecisionReportSection(
        heading="Rules Applied",
        body_md=header + "\n".join(lines),
    )


def _render_product_reference_tiers(
    parts: list[str],
    r: "RankedCandidates",
    *,
    filter_label: Optional[str] = None,
    water_soluble_only: bool = False,
) -> None:
    """Render one Tier 1 / 2 / 3 block for a nutrient's Product Reference.
    When `water_soluble_only=True`, drops non-water-soluble candidates
    (used for Foliar Practice contexts)."""
    if filter_label:
        parts.append(filter_label)
    for tier_name, tier_rows in (
        ("Tier 1 — Client's Global Product List", r.tier1),
        ("Tier 2 — Fallback Catalogue", r.tier2),
        ("Tier 3 — Pure straights", r.tier3),
    ):
        parts.append(f"**{tier_name}**")
        rows_in_scope = (
            [c for c in tier_rows if c.water_soluble]
            if water_soluble_only else list(tier_rows)
        )
        if not rows_in_scope:
            parts.append(
                "- _No water-soluble candidate available at this tier._"
                if water_soluble_only
                else "- _No candidate available at this tier._"
            )
            continue
        for c in rows_in_scope[:5]:
            conc = {"N": c.n, "P": c.p, "K": c.k}[r.nutrient]
            extra = " · water-soluble" if c.water_soluble else ""
            parts.append(f"- {c.name} — {r.nutrient}: {_fmt_num(conc)}{extra}")


def _section_personalised_plan(
    rankings: list[RankedCandidates],
    contributions: list[PracticeNutrientContribution],
    baseline: BaselineComputation,
    adjustments: list[TargetAdjustment],
    tier0_plan: list[Tier0Adjustment],
    gap_instructions: list[GapInstruction],
) -> DecisionReportSection:
    """Section 4 rewritten for Phase 8: baseline + target table →
    Tier 0 proposed Practice modifications → gap-case Instructions
    → candidate-ranking reference (now informational, not primary)."""
    header = "## 4. Personalised Plan\n\n"
    parts: list[str] = []

    # ── Baseline + Target table ──
    adj_by_letter: dict[str, TargetAdjustment] = {}
    for adj in adjustments:
        letter = _PARAM_NAME_TO_NUTRIENT_FULL.get(adj.parameter_name)
        if letter and adj.classification in (Classification.LOW, Classification.HIGH):
            adj_by_letter[letter] = adj

    nutrients_in_scope = sorted(
        set(baseline.per_nutrient.keys()) | set(adj_by_letter.keys()),
        key=lambda letter: list(_NUTRIENT_LETTER_TO_NAME.keys()).index(letter)
            if letter in _NUTRIENT_LETTER_TO_NAME else 999,
    )
    if nutrients_in_scope:
        parts.append("### Baseline + Target\n")
        parts.append("| Nutrient | Baseline (kg/acre) | Shift | Target (kg/acre) | Delta |")
        parts.append("|---|---:|---:|---:|---:|")
        for letter in nutrients_in_scope:
            name = _NUTRIENT_LETTER_TO_NAME.get(letter, letter)
            base = baseline.per_nutrient.get(letter, 0.0)
            adj = adj_by_letter.get(letter)
            if adj is None or adj.pct is None or adj.pct == 0.0:
                shift_txt = "—"
                target = base
                delta = 0.0
            else:
                shift_txt = f"{'+' if adj.pct > 0 else ''}{adj.pct:g}%"
                target = base * (1 + adj.pct / 100.0)
                delta = target - base
            parts.append(
                f"| {name} | {_fmt_num(base)} | {shift_txt} | "
                f"{_fmt_num(target)} | {('+' if delta > 0 else '') + _fmt_num(delta)} |"
            )
        parts.append("")

    # ── Tier 0 — Scaling existing Practices ──
    if tier0_plan:
        parts.append("### Tier 0 — Scaling existing Practices\n")
        # Group by nutrient for readability.
        by_nutrient: dict[str, list[Tier0Adjustment]] = {}
        for t in tier0_plan:
            by_nutrient.setdefault(t.nutrient, []).append(t)
        contrib_label_by_id = {c.practice_id: (c.display_label, c.timeline_name)
                               for c in contributions}
        for letter in [l for l in _NUTRIENT_LETTER_TO_NAME if l in by_nutrient]:
            items = by_nutrient[letter]
            name = _NUTRIENT_LETTER_TO_NAME.get(letter, letter)
            parts.append(f"**{name} ({letter})**")
            for t in items:
                label, tl_name = contrib_label_by_id.get(
                    t.practice_id, (t.practice_id, t.timeline_name))
                arrow = f"{_fmt_num(t.original_kg_per_acre)} → {_fmt_num(t.applied_kg_per_acre)} kg/acre"
                if t.skipped_reason == "past-acknowledged":
                    note = f" _(frozen — already applied; scaled target would have been {_fmt_num(t.scaled_kg_per_acre)})_"
                else:
                    pct_change = ((t.applied_kg_per_acre - t.original_kg_per_acre) / t.original_kg_per_acre * 100
                                  if t.original_kg_per_acre else 0.0)
                    note = f" (**{'+' if pct_change >= 0 else ''}{pct_change:.1f}%**)"
                parts.append(f"- [{tl_name}] {label}: {letter} {arrow}{note}")
            parts.append("")
    else:
        parts.append("_No Tier 0 adjustments — either no nutrient needs scaling, or no existing Practice carries the deficient nutrient (see Instructions below)._\n")

    # ── Gap-case Instructions ──
    if gap_instructions:
        parts.append("### New Instructions — nutrients without an existing Practice\n")
        for g in gap_instructions:
            where = (f"**first-future Timeline: {g.place_on_timeline_name}**"
                     if g.place_on_timeline_name
                     else "**no future Timeline available — see Section 6**")
            parts.append(f"**{g.nutrient_name} ({g.nutrient})** → {where}")
            parts.append(f"> {g.message}")
            parts.append("")

    # ── Candidate ranking (informational / reference) ──
    # Filtered per nutrient by the APPLICATION_METHOD(s) the SE
    # authored on their Package's Practices for that nutrient — so a
    # Foliar Practice only sees water-soluble swap candidates, matching
    # the agronomist's "focus on the SE's authored baseline" principle
    # (2026-10-22).
    if rankings:
        app_methods_by_nutrient: dict[str, set[str]] = {}
        for c in contributions:
            if not c.application_method:
                continue
            for nut in c.contributions:
                app_methods_by_nutrient.setdefault(nut, set()).add(c.application_method)

        parts.append("### Product reference — ranked candidates\n")
        parts.append("_Informational: if the SE wants to swap a product, these are the ranked options by nutrient concentration. Candidates are filtered by the application method on the SE's authored Practice(s) for each nutrient._\n")
        for r in rankings:
            label = {"N": "Nitrogen (N)", "P": "Phosphorus (P₂O₅)",
                     "K": "Potassium (K₂O)"}[r.nutrient]
            parts.append(f"#### {label}")
            methods = sorted(app_methods_by_nutrient.get(r.nutrient, set()))
            if not methods:
                # No Package Practice carries this nutrient (gap case) —
                # show all candidates unfiltered; SE will decide method
                # when authoring any new Practice.
                _render_product_reference_tiers(parts, r, filter_label=None)
            else:
                for method in methods:
                    is_foliar = "foliar" in method.lower()
                    filter_label = (
                        f"**For {method} (water-soluble only)**"
                        if is_foliar
                        else f"**For {method}**"
                    )
                    _render_product_reference_tiers(
                        parts, r, filter_label=filter_label,
                        water_soluble_only=is_foliar,
                    )

    return DecisionReportSection(
        heading="Personalised Plan",
        body_md=header + "\n".join(parts),
    )


def _section_advisory_text() -> DecisionReportSection:
    # v1 placeholder — L2 advisory text selection (Decision C complex
    # rules) lands in a later phase. Shown in the Report so the Client
    # sees the engine thought about it; empty body is honest about the
    # current scope.
    return DecisionReportSection(
        heading="Advisory Text Selected",
        body_md=(
            "## 5. Advisory Text Selected\n\n"
            "_v1 scope: no advisory text is injected yet. L2 Practice-level "
            "advisory selection lands in a later phase._"
        ),
    )


def _section_warnings(
    *, is_mock: bool, generated_at: datetime,
    classifications: list[ClassificationResult],
    adjustments: list[TargetAdjustment],
    rankings: list[RankedCandidates],
    uncomputable_contributions: Optional[list[str]] = None,
    gap_instructions: Optional[list[GapInstruction]] = None,
) -> DecisionReportSection:
    header = "## 6. Warnings & Metadata\n\n"
    lines = []
    unclassified = [c for c in classifications
                    if c.classification == Classification.NOT_CLASSIFIED]
    no_measurement = [c for c in classifications
                      if c.classification == Classification.NO_MEASUREMENT]
    if unclassified:
        lines.append(
            f"- ⚠️ {len(unclassified)} parameter(s) had a measurement but no "
            f"Normal Range configured — not classified. "
            f"({', '.join(c.parameter_name for c in unclassified)})"
        )
    if no_measurement:
        lines.append(
            f"- ⚠️ {len(no_measurement)} parameter(s) had a Normal Range "
            f"configured but no measurement in the input — not classified. "
            f"({', '.join(c.parameter_name for c in no_measurement)})"
        )
    actionable_adjs = [a for a in adjustments
                       if a.classification in (Classification.LOW, Classification.HIGH)
                       and a.pct is not None and a.pct != 0.0]
    tiers_without_candidates = []
    for r in rankings:
        if not r.tier1 and not r.tier2 and not r.tier3:
            tiers_without_candidates.append(r.nutrient)
    if tiers_without_candidates:
        lines.append(
            f"- ⚠️ Nutrient(s) {', '.join(tiers_without_candidates)} needed "
            f"an adjustment but no fertilizer candidate carried them."
        )
    # Non-NPK actionable adjustments — flagged as "informational only"
    # since v1 ranking is NPK-only.
    non_npk = [a for a in actionable_adjs
               if a.parameter_name not in _PARAM_NAME_TO_NUTRIENT]
    if non_npk:
        lines.append(
            f"- ℹ️ {len(non_npk)} non-NPK parameter(s) classified Low/High "
            f"({', '.join(a.parameter_name for a in non_npk)}). "
            f"v1 engine shows the classification only — product ranking "
            f"for secondaries/micros lands with Decision C in a later phase."
        )
    # Phase 8 — non-NPK Practices whose nutrient contributions couldn't
    # be computed because Cosh's fert_nutrient_concentration Connect
    # only covers N/P/K today. Surfaces every such Practice so the SE
    # knows the baseline is incomplete for those nutrients.
    if uncomputable_contributions:
        lines.append(
            f"- ℹ️ {len(uncomputable_contributions)} Practice(s) carry a "
            f"non-NPK product whose nutrient contribution isn't in Cosh's "
            f"concentration data yet, so they're excluded from the baseline. "
            f"({'; '.join(uncomputable_contributions)}) "
            f"Expand Cosh's `fert_nutrient_concentration` Connect to cover "
            f"these commons, or author them into the Fallback Catalogue."
        )
    # Phase 8 — gap-case Instructions with no future Timeline to place on
    # (Package has no Timeline after today's DAS). Surface so the SE
    # knows to add a late-season Timeline or the Instructions silently
    # drop.
    if gap_instructions:
        orphan_gaps = [g for g in gap_instructions
                       if g.place_on_timeline_id is None]
        if orphan_gaps:
            lines.append(
                f"- ⚠️ {len(orphan_gaps)} gap-case Instruction(s) couldn't be "
                f"placed on any Timeline (no future Timelines exist on this "
                f"Package): {', '.join(g.nutrient_name for g in orphan_gaps)}."
            )
    if not lines:
        lines.append("- No warnings.")
    lines.append("")
    lines.append(
        f"- **Generated at**: {generated_at.isoformat()}  \n"
        f"- **Mode**: {'Sandbox (Mock)' if is_mock else 'Real'}"
    )
    return DecisionReportSection(
        heading="Warnings & Metadata",
        body_md=header + "\n".join(lines),
    )


def build_decision_report(
    *,
    is_mock: bool,
    client_id: str,
    client_name: str,
    package_id: str,
    package_name: str,
    crop_cosh_id: str,
    crop_name: str,
    preset_name: Optional[str],
    classifications: list[ClassificationResult],
    adjustments: list[TargetAdjustment],
    rankings: list[RankedCandidates],
    contributions: Optional[list[PracticeNutrientContribution]] = None,
    baseline: Optional[BaselineComputation] = None,
    tier0_plan: Optional[list[Tier0Adjustment]] = None,
    gap_instructions: Optional[list[GapInstruction]] = None,
    uncomputable_contributions: Optional[list[str]] = None,
) -> DecisionReport:
    """Assemble the six-section Decision Report per Decision T.

    Phase 8 extras (optional for backward compat; caller passes None for
    the old-shape output): `contributions` (per-Practice nutrient
    contributions), `baseline` (per-nutrient totals), `tier0_plan` (the
    proposed proportional-scaling of existing Practices), and
    `gap_instructions` (new INSTRUCTION Practices for Low nutrients with
    no baseline). `uncomputable_contributions` is a list of human-
    readable labels for Practices whose nutrient contribution couldn't
    be computed (Cosh data gap)."""
    generated_at = datetime.now(timezone.utc)
    sections = [
        _section_summary(
            is_mock=is_mock, package_name=package_name, crop_name=crop_name,
            client_name=client_name, preset_name=preset_name,
            classifications=classifications,
        ),
        _section_soil_interpretation(classifications),
        _section_rules_applied(adjustments),
        _section_personalised_plan(
            rankings=rankings,
            contributions=contributions or [],
            baseline=baseline or BaselineComputation(per_nutrient={}),
            adjustments=adjustments,
            tier0_plan=tier0_plan or [],
            gap_instructions=gap_instructions or [],
        ),
        _section_advisory_text(),
        _section_warnings(
            is_mock=is_mock, generated_at=generated_at,
            classifications=classifications, adjustments=adjustments,
            rankings=rankings,
            uncomputable_contributions=uncomputable_contributions,
            gap_instructions=gap_instructions,
        ),
    ]
    return DecisionReport(
        is_mock=is_mock, generated_at=generated_at,
        client_id=client_id, package_id=package_id, crop_cosh_id=crop_cosh_id,
        sections=sections,
    )
