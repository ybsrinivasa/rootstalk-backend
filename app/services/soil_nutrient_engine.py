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
_PARAM_NAME_TO_NUTRIENT = {
    "Available Nitrogen": "N",
    "Available Phosphorus": "P",
    "Available Potassium": "K",
}


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


def _section_personalised_plan(
    rankings: list[RankedCandidates],
) -> DecisionReportSection:
    header = "## 4. Personalised Plan\n\n"
    if not rankings:
        return DecisionReportSection(
            heading="Personalised Plan",
            body_md=header + "_No NPK adjustment required for this soil._",
        )
    parts = []
    for r in rankings:
        label = {"N": "Nitrogen (N)", "P": "Phosphorus (P₂O₅)",
                 "K": "Potassium (K₂O)"}[r.nutrient]
        parts.append(f"### {label}\n")
        for tier_name, tier_rows in (
            ("Tier 1 — Client's Global Product List", r.tier1),
            ("Tier 2 — Fallback Catalogue", r.tier2),
            ("Tier 3 — Pure straights", r.tier3),
        ):
            parts.append(f"**{tier_name}**")
            if not tier_rows:
                parts.append("- _No candidate available at this tier._")
                continue
            for c in tier_rows[:5]:  # top 5 per tier
                conc = {"N": c.n, "P": c.p, "K": c.k}[r.nutrient]
                extra = " · water-soluble" if c.water_soluble else ""
                parts.append(f"- {c.name} — {r.nutrient}: {_fmt_num(conc)}{extra}")
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
) -> DecisionReport:
    """Assemble the six-section Decision Report per Decision T."""
    generated_at = datetime.now(timezone.utc)
    sections = [
        _section_summary(
            is_mock=is_mock, package_name=package_name, crop_name=crop_name,
            client_name=client_name, preset_name=preset_name,
            classifications=classifications,
        ),
        _section_soil_interpretation(classifications),
        _section_rules_applied(adjustments),
        _section_personalised_plan(rankings),
        _section_advisory_text(),
        _section_warnings(
            is_mock=is_mock, generated_at=generated_at,
            classifications=classifications, adjustments=adjustments,
            rankings=rankings,
        ),
    ]
    return DecisionReport(
        is_mock=is_mock, generated_at=generated_at,
        client_id=client_id, package_id=package_id, crop_cosh_id=crop_cosh_id,
        sections=sections,
    )
