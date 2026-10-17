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

from app.modules.advisory.models import Package
from app.modules.clients.models import Client, ClientCrop
from app.modules.soil_nutrient.models import (
    ClientNormalRange, ClientProduct, FallbackProduct,
    PackageNormalRangeOverride, PlatformSoilParameter, PresetSoilProfile,
)
from app.services.npk_candidates import load_fertiliser_candidates
from app.services.soil_nutrient_engine import (
    CandidateProduct, Classification, DecisionReport, DecisionReportSection,
    EffectiveRange, NormalRangeOperator, SoilValue,
    build_decision_report, classify_soil_values, compute_target_adjustments,
    rank_candidates,
)


# How many top-concentration straights to seed Tier 3 with, per nutrient.
# Keeps the Mock's Section 4 list manageable while still including the
# obvious (urea / DAP / MOP) picks.
_TIER3_PER_NUTRIENT = 3


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

    # ── 4. Effective ranges = Package override > crop default ──
    crop_ranges = (await db.execute(
        select(ClientNormalRange).where(
            ClientNormalRange.client_id == client_id,
            ClientNormalRange.crop_cosh_id == crop_cosh_id,
        )
    )).scalars().all()
    pkg_overrides = (await db.execute(
        select(PackageNormalRangeOverride).where(
            PackageNormalRangeOverride.package_id == package_id,
        )
    )).scalars().all()
    ranges_by_param: dict[str, EffectiveRange] = {}
    for r in crop_ranges:
        p = param_by_id.get(r.parameter_id)
        if p is None:
            continue
        ranges_by_param[r.parameter_id] = EffectiveRange(
            parameter_id=r.parameter_id, parameter_name=p.name,
            operator=NormalRangeOperator(r.operator.value),
            low_value=r.low_value, high_value=r.high_value,
            low_pct_adjustment=r.low_pct_adjustment,
            high_pct_adjustment=r.high_pct_adjustment,
            source="crop",
        )
    for r in pkg_overrides:
        p = param_by_id.get(r.parameter_id)
        if p is None:
            continue
        ranges_by_param[r.parameter_id] = EffectiveRange(
            parameter_id=r.parameter_id, parameter_name=p.name,
            operator=NormalRangeOperator(r.operator.value),
            low_value=r.low_value, high_value=r.high_value,
            low_pct_adjustment=r.low_pct_adjustment,
            high_pct_adjustment=r.high_pct_adjustment,
            source="package",
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

    # ── 6. Engine run ──
    classifications = classify_soil_values(soil_values, ranges)
    adjustments = compute_target_adjustments(classifications, ranges)
    rankings = rank_candidates(adjustments, candidates)

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
