"""Mock runner for the Soil Nutrient engine (Phase 7c).

Loads the DB inputs the Mock needs, converts them to the engine's
pure-function dataclasses, calls `soil_nutrient_engine`, returns the
Decision Report. No overlay rows, no notifications — those belong
to the Real engine (Decisions R + S).

Shared Cosh query layer: `load_fertiliser_candidates()` — the same
helper the Dealer NPK-select pipeline and the SA/Client picker
endpoints use. Keeps the Mock honest about what the Real engine
will see at runtime.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.advisory.models import Element, Package, Practice, Timeline
from app.modules.clients.models import Client, ClientCrop
from app.modules.soil_nutrient.models import (
    ClientProduct, FallbackProduct,
    PackageNormalRangeOverride, PlatformSoilParameter, PresetSoilProfile,
    SoilRangeTemplate, SoilRangeTemplateRow,
)
from app.services.npk_candidates import load_fertiliser_candidates
from app.services.soil_nutrient_engine import (
    BaselineComputation, CandidateProduct, Classification, DecisionReport,
    DecisionReportSection, EffectiveRange, GapInstruction,
    NormalRangeOperator, PracticeNutrientContribution, SoilValue,
    Tier0Adjustment,
    build_decision_report, classify_soil_values, compute_baseline_per_nutrient,
    compute_gap_instructions, compute_target_adjustments, compute_tier0_plan,
    rank_candidates,
)


# How many top-concentration straights to seed Tier 3 with, per nutrient.
# Keeps the Mock's Section 4 list manageable while still including the
# obvious (urea / DAP / MOP) picks.
_TIER3_PER_NUTRIENT = 3

# Phase 8 — L2 types the engine recognises for Package parsing. The
# first two express intent as nutrient quantities (NPK directly on
# Elements); the latter two express intent as a specific product +
# dose (COMMON_NAME cosh_id, we look up nutrient concentrations in
# Cosh via load_fertiliser_candidates).
_NUTRITION_DOSAGE_L2S = {"CHEMICAL_FERTILIZERS_NPK_DOSAGES",
                         "FERTIGATION_NPK_DOSAGES"}
_NUTRITION_PRODUCT_L2S = {"CHEMICAL_FERTILIZER_PRODUCTS",
                          "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS"}

# Agronomist-confirmed (2026-10-22): standard spray volume for foliar
# conversion. foliar_kg_per_acre = (dose_g_per_L × SPRAY_VOLUME) / 1000.
_FOLIAR_SPRAY_VOLUME_L_PER_ACRE = 200


async def run_mock(
    db: AsyncSession,
    *,
    client_id: str,
    package_id: str,
    preset_id: Optional[str],
    soil_values_input: Optional[dict[str, float]],
) -> DecisionReport:
    """End-to-end Mock run. Caller must have already auth-gated."""

    # ── 1. Context: client, package, crop ──
    client = await db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    package = await db.get(Package, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found")
    if package.client_id != client_id:
        raise HTTPException(
            status_code=403, detail="Package does not belong to this client",
        )

    crop_cosh_id = package.crop_cosh_id
    crop_row = (await db.execute(
        select(ClientCrop).where(
            ClientCrop.client_id == client_id,
            ClientCrop.crop_cosh_id == crop_cosh_id,
        )
    )).scalar_one_or_none()
    crop_name = (crop_row.crop_name_en if crop_row else None) or crop_cosh_id

    # ── 2. Platform parameter canon (name ↔ id) ──
    param_rows = (await db.execute(
        select(PlatformSoilParameter).where(
            PlatformSoilParameter.status == "ACTIVE",
        )
    )).scalars().all()
    param_by_id = {p.id: p for p in param_rows}
    param_by_name = {p.name: p for p in param_rows}

    # ── 3. Soil values — from preset or inline ──
    preset_name: Optional[str] = None
    soil_values: list[SoilValue] = []
    if preset_id:
        preset = await db.get(PresetSoilProfile, preset_id)
        if preset is None:
            raise HTTPException(status_code=404, detail="Preset not found")
        preset_name = preset.name
        # values_json is keyed by parameter *name* (per PresetSoilProfile
        # design). Translate to IDs via the canon so the engine never
        # sees display strings as keys.
        for name, value in (preset.values_json or {}).items():
            p = param_by_name.get(name)
            if p is None:
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            soil_values.append(SoilValue(
                parameter_id=p.id, parameter_name=p.name, value=v,
            ))
    elif soil_values_input:
        for pid, value in soil_values_input.items():
            p = param_by_id.get(pid)
            if p is None:
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            soil_values.append(SoilValue(
                parameter_id=p.id, parameter_name=p.name, value=v,
            ))
    else:
        raise HTTPException(
            status_code=400,
            detail="Supply either preset_id or soil_values",
        )

    # ── 4. Effective ranges (Decision W resolution) ──
    # Start with template bounds (if the Package has a template picked
    # AND the template is still ACTIVE). Overlay Package overrides on
    # top: operator=None rows inherit template bounds but still carry
    # Decision V adjustments; non-None operator rows replace bounds.
    ranges_by_param: dict[str, EffectiveRange] = {}

    if package.soil_range_template_id:
        tpl = (await db.execute(
            select(SoilRangeTemplate).options(
                selectinload(SoilRangeTemplate.rows),
            ).where(SoilRangeTemplate.id == package.soil_range_template_id)
        )).scalar_one_or_none()
        if tpl and tpl.status == "ACTIVE":
            for r in tpl.rows:
                p = param_by_id.get(r.parameter_id)
                if p is None:
                    continue
                ranges_by_param[r.parameter_id] = EffectiveRange(
                    parameter_id=r.parameter_id, parameter_name=p.name,
                    operator=NormalRangeOperator(r.operator.value),
                    low_value=r.low_value, high_value=r.high_value,
                    low_pct_adjustment=0.0,
                    high_pct_adjustment=0.0,
                    source="template",
                )

    pkg_overrides = (await db.execute(
        select(PackageNormalRangeOverride).where(
            PackageNormalRangeOverride.package_id == package_id,
        )
    )).scalars().all()
    for r in pkg_overrides:
        p = param_by_id.get(r.parameter_id)
        if p is None:
            continue
        existing = ranges_by_param.get(r.parameter_id)
        # Resolve the effective operator + bounds. operator=None means
        # inherit from template; non-None means the override replaces
        # the template's bounds for this parameter.
        if r.operator is None:
            if existing is None:
                # No template bounds AND no override bounds — this row
                # carries only adjustments but we have no classification
                # shape to apply them to. Skip; nothing to classify.
                continue
            eff_operator = existing.operator
            eff_low = existing.low_value
            eff_high = existing.high_value
            source = "package-adj"  # adjustments only, bounds inherited
        else:
            eff_operator = NormalRangeOperator(r.operator.value)
            eff_low = r.low_value
            eff_high = r.high_value
            source = "package"
        ranges_by_param[r.parameter_id] = EffectiveRange(
            parameter_id=r.parameter_id, parameter_name=p.name,
            operator=eff_operator,
            low_value=eff_low, high_value=eff_high,
            low_pct_adjustment=r.low_pct_adjustment,
            high_pct_adjustment=r.high_pct_adjustment,
            source=source,
        )
    ranges = list(ranges_by_param.values())

    # ── 5. Candidate products — Tier 1, 2, 3 ──
    cosh_candidates, _ = await load_fertiliser_candidates(db, fertigation=False)
    cosh_by_id = {c.cosh_id: c for c in cosh_candidates}

    # Tier 1 — Client Products (ACTIVE only; assigned to this crop OR
    # universal if the Client never scoped it). The ClientProduct
    # pointer carries `crop_assignments`; empty list = universal.
    cp_rows = (await db.execute(
        select(ClientProduct).options(
            selectinload(ClientProduct.crop_assignments),
        ).where(
            ClientProduct.client_id == client_id,
            ClientProduct.status == "ACTIVE",
        )
    )).scalars().all()
    tier1_cosh_ids: set[str] = set()
    for cp in cp_rows:
        active_crops = {a.crop_cosh_id for a in cp.crop_assignments
                        if a.status == "ACTIVE"}
        if active_crops and crop_cosh_id not in active_crops:
            continue
        if cp.cosh_product_id in cosh_by_id:
            tier1_cosh_ids.add(cp.cosh_product_id)

    # Tier 2 — Fallback Catalogue (ACTIVE; same crop-scope rule).
    fb_rows = (await db.execute(
        select(FallbackProduct).options(
            selectinload(FallbackProduct.crop_assignments),
        ).where(FallbackProduct.status == "ACTIVE")
    )).scalars().all()
    tier2_cosh_ids: set[str] = set()
    for fb in fb_rows:
        active_crops = {a.crop_cosh_id for a in fb.crop_assignments
                        if a.status == "ACTIVE"}
        if active_crops and crop_cosh_id not in active_crops:
            continue
        if fb.cosh_product_id not in tier1_cosh_ids \
                and fb.cosh_product_id in cosh_by_id:
            tier2_cosh_ids.add(fb.cosh_product_id)

    # Tier 3 — pure straights. v1 heuristic: top N-concentration,
    # top P-concentration, top K-concentration from the Cosh catalogue
    # that aren't already in Tier 1 or 2. Captures urea / DAP / MOP
    # naturally without a hand-coded list.
    tier3_cosh_ids: set[str] = set()
    remaining = [c for c in cosh_candidates
                 if c.cosh_id not in tier1_cosh_ids
                 and c.cosh_id not in tier2_cosh_ids]
    for key in ("n", "p", "k"):
        ranked = sorted(remaining,
                        key=lambda c: getattr(c.concentration, key),
                        reverse=True)
        for c in ranked[:_TIER3_PER_NUTRIENT]:
            if getattr(c.concentration, key) > 0:
                tier3_cosh_ids.add(c.cosh_id)

    def to_candidate(cosh_id: str, tier: int) -> CandidateProduct:
        c = cosh_by_id[cosh_id]
        return CandidateProduct(
            cosh_id=cosh_id, name=c.name,
            n=c.concentration.n, p=c.concentration.p, k=c.concentration.k,
            water_soluble=c.water_soluble, tier=tier,
        )
    candidates: list[CandidateProduct] = []
    for cid in tier1_cosh_ids:
        candidates.append(to_candidate(cid, 1))
    for cid in tier2_cosh_ids:
        candidates.append(to_candidate(cid, 2))
    for cid in tier3_cosh_ids:
        candidates.append(to_candidate(cid, 3))

    # ── 6. Package parsing — Phase 8 (2026-10-22) ──
    # Walk the Package's Timelines → Practices → Elements for the four
    # fertilizer L2 types. Build PracticeNutrientContribution per
    # Practice; track Practices whose nutrient contribution couldn't
    # be computed (Cosh non-NPK data gap) for the Section 6 warning.
    contributions, uncomputable, first_future_tl_id, first_future_tl_name = \
        await _parse_package_contributions(
            db, package_id=package_id, cosh_by_id=cosh_by_id,
        )

    # ── 7. Engine run ──
    classifications = classify_soil_values(soil_values, ranges)
    adjustments = compute_target_adjustments(classifications, ranges)
    rankings = rank_candidates(adjustments, candidates)
    baseline = compute_baseline_per_nutrient(contributions)
    tier0_plan = compute_tier0_plan(contributions, baseline, adjustments)
    gap_instructions = compute_gap_instructions(
        adjustments, baseline, first_future_tl_id, first_future_tl_name,
    )

    return build_decision_report(
        is_mock=True,
        client_id=client_id,
        client_name=client.display_name or client.id,
        package_id=package_id,
        package_name=package.name,
        crop_cosh_id=crop_cosh_id,
        crop_name=crop_name,
        preset_name=preset_name,
        classifications=classifications,
        adjustments=adjustments,
        rankings=rankings,
        contributions=contributions,
        baseline=baseline,
        tier0_plan=tier0_plan,
        gap_instructions=gap_instructions,
        uncomputable_contributions=uncomputable,
    )


def report_to_dict(report: DecisionReport) -> dict:
    """Convert the dataclass tree into a dict matching DecisionReportOut
    for the FastAPI response_model layer."""
    return {
        "is_mock": report.is_mock,
        "generated_at": report.generated_at,
        "client_id": report.client_id,
        "package_id": report.package_id,
        "crop_cosh_id": report.crop_cosh_id,
        "sections": [
            {"heading": s.heading, "body_md": s.body_md}
            for s in report.sections
        ],
    }


async def _parse_package_contributions(
    db: AsyncSession,
    *,
    package_id: str,
    cosh_by_id: dict,  # dict[str, CoshFertilizerCandidate] from load_fertiliser_candidates
) -> tuple[list[PracticeNutrientContribution], list[str], Optional[str], Optional[str]]:
    """Walk the Package's Timelines → Practices → Elements and emit:
      - A list of PracticeNutrientContribution (one per
        fertilizer-related Practice whose nutrient contribution is
        computable).
      - A list of human-readable labels for Practices that carry a
        non-NPK product whose nutrient contribution couldn't be
        computed (Cosh data gap). Surfaced in Section 6 warnings.
      - first_future_timeline_id / name — the earliest Timeline on
        the Package (Mock treats "now" = DAS 0, so the first-future
        Timeline is simply the one with the smallest from_value).

    Mock mode assumption: is_acknowledged=False for every Practice
    (no farmer subscription in a Mock, so Decision H's past-ack
    path isn't exercised). The real engine will replace this with
    a BL-05 past-ack query against the farmer's subscription.
    """
    from app.modules.sync.models import CoshCoreItem

    tl_rows = (await db.execute(
        select(Timeline).where(Timeline.package_id == package_id)
        .order_by(Timeline.from_value)
    )).scalars().all()

    first_future_tl_id = tl_rows[0].id if tl_rows else None
    first_future_tl_name = tl_rows[0].name if tl_rows else None

    practice_rows = (await db.execute(
        select(Practice).where(
            Practice.timeline_id.in_([tl.id for tl in tl_rows]) if tl_rows else False,
            Practice.l2_type.in_(_NUTRITION_DOSAGE_L2S | _NUTRITION_PRODUCT_L2S),
        ).order_by(Practice.display_order)
    )).scalars().all() if tl_rows else []

    tl_by_id = {tl.id: tl for tl in tl_rows}

    element_rows = (await db.execute(
        select(Element).where(
            Element.practice_id.in_([pr.id for pr in practice_rows])
            if practice_rows else False,
        ).order_by(Element.display_order)
    )).scalars().all() if practice_rows else []

    elements_by_practice: dict[str, list[Element]] = {}
    for e in element_rows:
        elements_by_practice.setdefault(e.practice_id, []).append(e)

    # Resolve Cosh references (DOSAGE_UNIT, APPLICATION_METHOD,
    # COMMON_NAME) so we can detect foliar + name products in uncomputable
    # cases. Collect all cosh_refs mentioned across all elements.
    cosh_refs_to_resolve: set[str] = set()
    for els in elements_by_practice.values():
        for e in els:
            if e.cosh_ref:
                cosh_refs_to_resolve.add(e.cosh_ref)
    resolved_cosh: dict[str, tuple[str, str]] = {}  # cosh_id → (core_type, name_en)
    if cosh_refs_to_resolve:
        core_rows = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.cosh_id.in_(cosh_refs_to_resolve),
            )
        )).scalars().all()
        for c in core_rows:
            name_en = (c.translations or {}).get("en") if c.translations else None
            resolved_cosh[c.cosh_id] = (c.core_type, name_en or c.cosh_id)

    def _cosh_name(cosh_id: Optional[str]) -> str:
        if not cosh_id:
            return ""
        return resolved_cosh.get(cosh_id, ("", cosh_id))[1]

    def _parse_float(s: Optional[str]) -> float:
        if not s:
            return 0.0
        try:
            return float(s)
        except (TypeError, ValueError):
            return 0.0

    contributions: list[PracticeNutrientContribution] = []
    uncomputable: list[str] = []

    for pr in practice_rows:
        tl = tl_by_id[pr.timeline_id]
        els = elements_by_practice.get(pr.id, [])
        by_type: dict[str, Element] = {e.element_type: e for e in els}

        contrib: dict[str, float] = {}
        display_label = f"{pr.l2_type}"

        # APPLICATION_METHOD is present on both DOSAGES and PRODUCTS
        # Practices; resolve upfront so both branches can carry it on
        # the PracticeNutrientContribution for the engine's Product
        # Reference filter.
        app_method_cosh = (by_type.get("APPLICATION_METHOD") or Element()).cosh_ref
        app_method_name = _cosh_name(app_method_cosh)

        if pr.l2_type in _NUTRITION_DOSAGE_L2S:
            # Nutrient-quantity Practice — read N/P/K directly.
            n = _parse_float((by_type.get("N_DOSAGE") or Element()).value)
            p = _parse_float((by_type.get("P_DOSAGE") or Element()).value)
            k = _parse_float((by_type.get("K_DOSAGE") or Element()).value)
            if n:
                contrib["N"] = n
            if p:
                contrib["P"] = p
            if k:
                contrib["K"] = k
            unit_name = _cosh_name((by_type.get("UNIT") or Element()).cosh_ref)
            label = "Nutrient dosages"
            if pr.l2_type == "FERTIGATION_NPK_DOSAGES":
                label = "Fertigation nutrient dosages"
            pieces = []
            if n:
                pieces.append(f"N {_fmt(n)}")
            if p:
                pieces.append(f"P {_fmt(p)}")
            if k:
                pieces.append(f"K {_fmt(k)}")
            unit_tail = f" {unit_name}" if unit_name else ""
            display_label = f"{label} ({', '.join(pieces)}{unit_tail})" if pieces else label

        else:
            # Product Practice — look up COMMON_NAME in Cosh for NPK
            # concentration, compute kg of elemental nutrient per acre.
            common_name_cosh = (by_type.get("COMMON_NAME") or Element()).cosh_ref
            dosage = _parse_float((by_type.get("DOSAGE") or Element()).value)
            dosage_unit_cosh = (by_type.get("DOSAGE_UNIT") or Element()).cosh_ref
            dosage_unit_name = _cosh_name(dosage_unit_cosh)
            product_name = _cosh_name(common_name_cosh)

            # Foliar conversion — g/L → kg/acre via the 200 L/acre spray
            # standard (agronomist-confirmed 2026-10-22).
            is_foliar_gL = (
                "foliar" in app_method_name.lower()
                and "g/l" in dosage_unit_name.lower()
            )
            kg_product_per_acre = (
                (dosage * _FOLIAR_SPRAY_VOLUME_L_PER_ACRE) / 1000.0
                if is_foliar_gL else dosage
            )

            # Nutrient composition — only NPK via load_fertiliser_candidates
            # today. Products with no NPK (Zinc Sulfate, Boron) won't
            # be in cosh_by_id and are flagged as uncomputable.
            cosh_candidate = cosh_by_id.get(common_name_cosh)
            if cosh_candidate is None:
                uncomputable.append(
                    f"[{tl.name}] {product_name or 'unknown product'} ({_fmt(dosage)} {dosage_unit_name or ''}".rstrip() + ")"
                )
                # Still create a contribution row with empty dict so
                # the display_label shows up in Section 4's reference.
                # Actually no — we skip it entirely; the uncomputable
                # list is the signal.
                continue

            if cosh_candidate.concentration.n:
                contrib["N"] = kg_product_per_acre * cosh_candidate.concentration.n / 100.0
            if cosh_candidate.concentration.p:
                contrib["P"] = kg_product_per_acre * cosh_candidate.concentration.p / 100.0
            if cosh_candidate.concentration.k:
                contrib["K"] = kg_product_per_acre * cosh_candidate.concentration.k / 100.0

            tail = f" via {app_method_name}" if app_method_name else ""
            display_label = (
                f"{product_name} {_fmt(dosage)} {dosage_unit_name}{tail}".strip()
            )

        contributions.append(PracticeNutrientContribution(
            practice_id=pr.id,
            timeline_id=pr.timeline_id,
            timeline_name=tl.name,
            timeline_from_days=tl.from_value,
            l2_type=pr.l2_type,
            display_label=display_label,
            is_acknowledged=False,  # Mock mode — no subscription
            contributions=contrib,
            application_method=app_method_name,
        ))

    return contributions, uncomputable, first_future_tl_id, first_future_tl_name


def _fmt(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return f"{v:g}"
