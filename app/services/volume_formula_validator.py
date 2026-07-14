"""Authoring-time validation of (measure, l2, method, dosage_unit) combinations
against the `volume_formulas` lookup table.

The same 5-key lookup that drives `/dealer/orders/{id}/items/{item_id}/volume-estimate`
at order time is replayed here for SE authoring. If a combination has no
formula row, the dealer would see "Estimate unavailable" at order time —
better to flag the SE while they're still authoring.

Per user direction (2026-06-20):
- Warn, don't block. SE can still save the practice.
- Three-tier verdict so the UI can match severity to the actual gap.
- For CCA timelines we know the crop → measure. For CHA / SP / QA
  timelines the practice is cross-crop, so we check across all measures
  and warn only when none has formulas for the pair.
"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import Package, Timeline
from app.modules.sync.models import VolumeFormula


# Verdict codes (string-enum-ish). Frontend keys i18n + colour off these.
VERDICT_OK = "ok"                              # formula row exists
VERDICT_UNSUPPORTED_COMBO = "unsupported_combo"  # method+dose_unit pair has no row
VERDICT_METHOD_UNSEEDED = "method_unseeded"      # no rows with this method at all
VERDICT_L2_UNSEEDED = "l2_unseeded"              # nothing exists for this (measure, l2)
VERDICT_PHASE_MISMATCH = "phase_mismatch"        # 2026-07-14 — dosage-unit phase doesn't fit any brand under this common name


async def _resolve_measure_for_timeline(
    db: AsyncSession, timeline_id: str,
) -> Optional[str]:
    """For a CCA timeline, return the crop's measure. For CHA / SP / QA
    (no package on the timeline), return None — the caller will treat
    that as "any measure" and check across all of them.
    """
    tl = (await db.execute(
        select(Timeline).where(Timeline.id == timeline_id)
    )).scalar_one_or_none()
    if tl is None or tl.package_id is None:
        return None
    pkg = (await db.execute(
        select(Package).where(Package.id == tl.package_id)
    )).scalar_one_or_none()
    if pkg is None or not pkg.crop_cosh_id:
        return None
    # Late import to avoid circulars at startup.
    from app.services.crop_measure import get_measure
    return await get_measure(db, pkg.crop_cosh_id)


async def _resolve_cosh_to_en(db: AsyncSession, value: str) -> str:
    """If `value` looks like a 36-char UUID, look it up in CoshCoreItem
    and return its English translation. Otherwise return as-is.

    The PracticeFormModal stores cosh-driven element values as cosh_ids
    (UUIDs) — that's what gets sent to this endpoint. The `volume_formulas`
    table is keyed on the English name ("Dusting", "ml/L"), so we
    resolve before querying. Mirrors the same resolution the runtime
    estimated-volume endpoint does.
    """
    if not value:
        return value
    # Cheap UUID-shape check.
    if len(value) != 36 or value.count("-") != 4:
        return value
    from app.modules.sync.models import CoshCoreItem
    core = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == value)
    )).scalar_one_or_none()
    if not core:
        return value
    en = (core.translations or {}).get("en")
    return en or value


async def check_volume_formula_combination(
    db: AsyncSession,
    *,
    timeline_id: str,
    l2: str,
    application_method: str,
    dosage_unit: str,
) -> dict:
    """Return `{verdict, message, measure}` for the given combination.

    Skips the check (returns OK) if any required field is missing — the
    UI is expected to wait until both method and dosage_unit are
    populated before calling this. Belt-and-braces: if the SE somehow
    posts a partial form, we don't warn on every keystroke.

    `application_method` and `dosage_unit` may arrive as either English
    names ("Dusting", "ml/L") or Cosh UUIDs (the modal's element store
    holds cosh_ids for cosh-driven fields). We normalise to English
    before querying the formula table.
    """
    if not all([timeline_id, l2, application_method, dosage_unit]):
        return {"verdict": VERDICT_OK, "message": None, "measure": None}

    # Normalise potential cosh_ids → English names.
    application_method = await _resolve_cosh_to_en(db, application_method)
    dosage_unit = await _resolve_cosh_to_en(db, dosage_unit)

    # Same suffix trim the order-time path does ("ml/L of water" → "ml/L").
    dosage_unit_alt = dosage_unit.replace(" of water", "")

    measure = await _resolve_measure_for_timeline(db, timeline_id)

    # Helper: count of ACTIVE rows for an arbitrary filter set.
    async def _count(**filters) -> int:
        q = select(VolumeFormula).where(VolumeFormula.status == "ACTIVE")
        if "measure" in filters and filters["measure"]:
            q = q.where(VolumeFormula.measure == filters["measure"])
        if "l2" in filters:
            q = q.where(VolumeFormula.l2_practice == filters["l2"])
        if "method" in filters:
            q = q.where(VolumeFormula.application_method == filters["method"])
        if "dose" in filters:
            q = q.where(VolumeFormula.dosage_unit.in_([dosage_unit, dosage_unit_alt]))
        rows = (await db.execute(q)).scalars().all()
        return len(rows)

    measure_args = {"measure": measure} if measure else {}

    full = await _count(**measure_args, l2=l2, method=application_method, dose=True)
    if full > 0:
        return {"verdict": VERDICT_OK, "message": None, "measure": measure}

    method_any_dose = await _count(**measure_args, l2=l2, method=application_method)
    l2_any_method = await _count(**measure_args, l2=l2)

    if l2_any_method == 0:
        # Table not yet seeded for this (measure, l2). Don't alarm —
        # the SA hasn't backfilled anything here yet, and warning on
        # everything during seed time is noise.
        return {
            "verdict": VERDICT_L2_UNSEEDED,
            "message": (
                f"No volume formulas configured yet for {l2} on this measure. "
                "An SA will need to seed the formula table."
            ),
            "measure": measure,
        }

    if method_any_dose == 0:
        return {
            "verdict": VERDICT_METHOD_UNSEEDED,
            "message": (
                f"No volume formulas exist for the application method "
                f"'{application_method}' on {l2}. "
                "Either pick a supported method or ask an SA to add a formula row."
            ),
            "measure": measure,
        }

    return {
        "verdict": VERDICT_UNSUPPORTED_COMBO,
        "message": (
            f"No volume formula exists for dosage unit '{dosage_unit}' with "
            f"'{application_method}' on {l2}. "
            "Dealers will not see an estimated volume — they will enter quantities manually."
        ),
        "measure": measure,
    }


def resolve_common_name_cosh_id_from_elements(elements: list) -> Optional[str]:
    """Extract COMMON_NAME's cosh_ref from a Practice's element list.
    Returns the UUID; the caller downstream resolves brand rows via
    `brand_cache.get_brands_for_common_name`. Returns None when the
    element is absent (rare — most Input L2s make COMMON_NAME
    mandatory) OR present without a cosh_ref (SE typed free text
    instead of picking from Cosh).
    """
    def _get(e, k):
        if isinstance(e, dict):
            return e.get(k)
        return getattr(e, k, None)
    for el in elements or []:
        et = (_get(el, "element_type") or "").upper()
        if et == "COMMON_NAME":
            return _get(el, "cosh_ref")
    return None


async def check_dosage_phase_vs_common_name_brands(
    db: AsyncSession,
    *,
    common_name_cosh_id: Optional[str],
    dosage_unit_en: Optional[str],
) -> dict:
    """Phase-mismatch check (2026-07-14).

    Verifies that at least one Cosh brand under the practice's
    common name has a formulation whose unit family (solid / liquid
    / discrete) matches the dosage unit's numerator phase. If none
    do, warn the SE — otherwise the dealer's brand list will be
    empty after the runtime phase filter runs.

    Returns `{verdict, message}`. `VERDICT_OK` when either input is
    missing (defensive — can't judge without both), when the dosage
    unit's numerator doesn't map to a family, when the common name
    has no cached brand rows, or when at least one brand fits.
    """
    if not common_name_cosh_id or not dosage_unit_en:
        return {"verdict": VERDICT_OK, "message": None}

    from app.services.bl07_brand_options import (
        _dosage_unit_phase, _classify_formulation_to_unit_family,
    )
    dosage_phase = _dosage_unit_phase(dosage_unit_en)
    if dosage_phase is None:
        return {"verdict": VERDICT_OK, "message": None}

    from app.services.brand_cache import get_brands_for_common_name
    brands = await get_brands_for_common_name(db, common_name_cosh_id)
    if not brands:
        # No brand rows yet — can't decide. Silent to avoid alarming
        # on Cosh gaps that the SA is still filling in.
        return {"verdict": VERDICT_OK, "message": None}

    any_match = any(
        _classify_formulation_to_unit_family(b.formulation_name) == dosage_phase
        for b in brands
    )
    if any_match:
        return {"verdict": VERDICT_OK, "message": None}

    return {
        "verdict": VERDICT_PHASE_MISMATCH,
        "message": (
            f"No brand under this common name has a {dosage_phase} unit that "
            f"fits your dosage unit '{dosage_unit_en}'. Either pick a dosage "
            f"unit whose phase matches an available brand, or check that "
            f"the common name has the right formulations in Cosh."
        ),
    }


async def resolve_method_and_dosage_unit_from_elements(
    db: AsyncSession,
    elements: list,
) -> tuple[Optional[str], Optional[str]]:
    """Walk a list of element dicts / objects (anything with element_type,
    value, cosh_ref) and resolve APPLICATION_METHOD + DOSAGE_UNIT to their
    English names — the same shape `volume_formulas.application_method` /
    `volume_formulas.dosage_unit` are keyed by.

    Mirrors the runtime resolution in `/dealer/orders/.../volume-estimate`.
    Each element can be either a Pydantic / dataclass instance OR a dict;
    we use getattr-or-key access.
    """
    from app.modules.sync.models import CoshCoreItem

    def _get(e, k):
        if isinstance(e, dict):
            return e.get(k)
        return getattr(e, k, None)

    by_type: dict[str, object] = {}
    for el in elements or []:
        et = (_get(el, "element_type") or "").lower()
        if et:
            by_type[et] = el

    method_el = by_type.get("application_method")
    dosage_unit_el = by_type.get("dosage_unit")
    dosage_el = by_type.get("dosage")

    method: Optional[str] = None
    if method_el:
        method = _get(method_el, "value")
        if not method and _get(method_el, "cosh_ref"):
            core = (await db.execute(
                select(CoshCoreItem).where(CoshCoreItem.cosh_id == _get(method_el, "cosh_ref"))
            )).scalar_one_or_none()
            if core:
                method = (core.translations or {}).get("en")

    dosage_unit: Optional[str] = None
    if dosage_unit_el:
        if _get(dosage_unit_el, "cosh_ref"):
            core = (await db.execute(
                select(CoshCoreItem).where(CoshCoreItem.cosh_id == _get(dosage_unit_el, "cosh_ref"))
            )).scalar_one_or_none()
            if core:
                dosage_unit = (core.translations or {}).get("en")
        if not dosage_unit:
            dosage_unit = _get(dosage_unit_el, "value")
    if not dosage_unit and dosage_el:
        unit_cosh = _get(dosage_el, "unit_cosh_id")
        if unit_cosh:
            core = (await db.execute(
                select(CoshCoreItem).where(CoshCoreItem.cosh_id == unit_cosh)
            )).scalar_one_or_none()
            dosage_unit = (
                (core.translations or {}).get("en") if core else unit_cosh
            )

    return method, dosage_unit


async def attach_volume_formula_warning_header(
    response,
    db: AsyncSession,
    *,
    timeline_id: str,
    l2: Optional[str],
    elements: list,
) -> None:
    """Save-time defence in depth. Call after a practice POST/PUT commits.

    Runs the same check the GET endpoint does and stamps the result on the
    response headers if non-ok:
      X-RT-Practice-Volume-Warning: <verdict>
      X-RT-Practice-Volume-Warning-Message: <human-readable>

    The CA portal's PracticeFormModal reads the GET endpoint for live UI
    feedback; direct-API users get the header on save so they can't
    silently slip past the same check.
    """
    if not l2 or not timeline_id:
        return
    method, dosage_unit = await resolve_method_and_dosage_unit_from_elements(
        db, elements,
    )
    if not method or not dosage_unit:
        return
    verdict = await check_volume_formula_combination(
        db, timeline_id=timeline_id, l2=l2,
        application_method=method, dosage_unit=dosage_unit,
    )
    if verdict["verdict"] != VERDICT_OK:
        # Headers must be latin-1 encodable. The verdict code is ASCII;
        # the message contains em-dashes / other unicode — only ship
        # the verdict via header. Direct-API users call
        # /practice-taxonomy/check-volume-formula if they want the
        # full localised message.
        response.headers["X-RT-Practice-Volume-Warning"] = verdict["verdict"]
        return

    # 2026-07-14 — Formula existence check passed. Also run the phase-
    # mismatch check: if no brand under this practice's common name
    # has a unit family matching the dosage unit's phase, the dealer
    # will see an empty brand list at order time. Warn the SE now.
    common_name_cosh_id = resolve_common_name_cosh_id_from_elements(elements)
    phase_v = await check_dosage_phase_vs_common_name_brands(
        db,
        common_name_cosh_id=common_name_cosh_id,
        dosage_unit_en=dosage_unit,
    )
    if phase_v["verdict"] != VERDICT_OK:
        response.headers["X-RT-Practice-Volume-Warning"] = phase_v["verdict"]
