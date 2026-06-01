"""Build NPK candidate list from the real Cosh data shape.

Cosh contract (synced 2026-06-01 — confirmed against testing payload):

  Cores
    common_names_of_inputs            — the fertiliser identity
    fert_nutrients                    — 3 rows: Nitrogen / Phosphorus / Potassium
    straight_complex                  — 2 rows: Straight fertilizer / Complex fertilizer
    fert_nutrient_concentration_core  — concentration values. translations.en
                                        carries the numeric percentage as text
                                        ("8", "10", "16.5", "46", etc.).

  Connects
    fert_nutrient_concentration (4-endpoint)
      role=common_names_of_inputs            position 1
      role=straight_complex                  position 2
      role=fert_nutrients                    position 3
      role=fert_nutrient_concentration_core  position 4

    npk_fertigation_products (3-endpoint, Connect-references-Connect)
      position 1 → commonnames_l2 connect_id
      position 2 → tradename_manufacturer connect_id
      position 3 → formulations Core

A Straight fertiliser has ONE Connect row (one nutrient). A Complex
(Mixed) fertiliser has TWO or THREE Connect rows — one per nutrient
it carries. We group by common_name and assemble a {N, P, K} tuple
per fertiliser.

Fertigation filtering (spec §5.1) — IMPLEMENTED via `npk_fertigation_
products`. Common names absent from that Connect's reachable set are
dropped when `fertigation=True`. The auto-generated role names at
positions 1 and 2 are unstable, so we identify endpoints by POSITION.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_COMMON_NAMES_CORE,
    COSH_COMMONNAMES_L2_CONNECT,
    COSH_FERT_NUTRIENT_CONCENTRATION_CONNECT,
    COSH_FERT_NUTRIENT_CONCENTRATION_CORE,
    COSH_FERT_NUTRIENTS_CORE,
    COSH_NPK_FERT_POS_COMMONNAMES_L2_ID,
    COSH_NPK_FERTIGATION_PRODUCTS_CONNECT,
)
from app.services.npk_ranking import Candidate, Concentration


# Cosh ships nutrient identities as English translations on the
# fert_nutrients Core. Map them to NPK letter codes so the rest of
# the algorithm stays terse.
_NUTRIENT_NAME_TO_LETTER = {
    "Nitrogen":   "N",
    "Phosphorus": "P",
    "Potassium":  "K",
}


async def load_fertigation_approved_common_name_ids(
    db: AsyncSession,
) -> set[str]:
    """Resolve the set of common-name cosh_ids that have at least one
    Fertigation-approved trade name (per `npk_fertigation_products`).

    Two-hop walk:
      npk_fertigation_products row → position-1 cosh_id =
        connect_id of a commonnames_l2 row → THAT row's position-1
        endpoint cosh_id = the common_name.
    """
    fert_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_NPK_FERTIGATION_PRODUCTS_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    if not fert_rows:
        return set()

    commonnames_l2_ids: set[str] = set()
    for r in fert_rows:
        for ep in r.endpoints or []:
            pos = ep.get("position")
            if pos == COSH_NPK_FERT_POS_COMMONNAMES_L2_ID and ep.get("cosh_id"):
                commonnames_l2_ids.add(ep["cosh_id"])
    if not commonnames_l2_ids:
        return set()

    cnl2_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_COMMONNAMES_L2_CONNECT,
            CoshConnectRow.connect_id.in_(commonnames_l2_ids),
            CoshConnectRow.status == "active",
        )
    )).scalars().all()

    approved: set[str] = set()
    for r in cnl2_rows:
        for ep in r.endpoints or []:
            if (
                ep.get("role") == COSH_COMMON_NAMES_CORE
                and ep.get("cosh_id")
            ):
                approved.add(ep["cosh_id"])
    return approved


async def load_fertiliser_candidates(
    db: AsyncSession,
    *,
    fertigation: bool = False,
) -> tuple[list[Candidate], int]:
    """Return (candidates, skipped_count).

    When `fertigation=True`, only common names approved via
    `npk_fertigation_products` are returned (spec §5.1 water-soluble
    filter). The skipped count counts both Cosh-side gaps and rows
    filtered out by the fertigation gate, so the endpoint can show a
    single diagnostic to the CM.
    """
    # Pull everything we need in three queries — cheaper than per-row
    # lookups inside the loop.
    rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_FERT_NUTRIENT_CONCENTRATION_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    if not rows:
        return [], 0

    needed_cosh_ids: set[str] = set()
    for r in rows:
        for ep in r.endpoints or []:
            cid = ep.get("cosh_id")
            if cid:
                needed_cosh_ids.add(cid)

    if not needed_cosh_ids:
        return [], 0

    core_rows = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id.in_(needed_cosh_ids),
            CoshCoreItem.status == "active",
        )
    )).scalars().all()
    cores_by_id = {c.cosh_id: c for c in core_rows}

    # accumulator: common_name_cosh_id → {"N": pct, "P": pct, "K": pct}
    acc: dict[str, dict[str, float]] = defaultdict(lambda: {"N": 0.0, "P": 0.0, "K": 0.0})
    common_name_seen: set[str] = set()
    skipped = 0

    for row in rows:
        ep_map = {
            ep.get("role"): ep.get("cosh_id")
            for ep in (row.endpoints or [])
        }
        cn_id = ep_map.get(COSH_COMMON_NAMES_CORE)
        nut_id = ep_map.get(COSH_FERT_NUTRIENTS_CORE)
        conc_id = ep_map.get(COSH_FERT_NUTRIENT_CONCENTRATION_CORE)
        if not (cn_id and nut_id and conc_id):
            skipped += 1
            continue
        common_name_seen.add(cn_id)

        nut_core = cores_by_id.get(nut_id)
        conc_core = cores_by_id.get(conc_id)
        if nut_core is None or conc_core is None:
            skipped += 1
            continue

        nut_en = (nut_core.translations or {}).get("en")
        letter = _NUTRIENT_NAME_TO_LETTER.get(nut_en or "")
        if letter is None:
            skipped += 1
            continue

        conc_en = (conc_core.translations or {}).get("en")
        try:
            pct = float(conc_en) if conc_en is not None else 0.0
        except (TypeError, ValueError):
            skipped += 1
            continue
        if pct <= 0:
            # 0% concentration carries no information for the matcher;
            # don't write it into the accumulator (the default 0.0 stays).
            continue
        acc[cn_id][letter] = pct

    # Fertigation gate (spec §5.1). Only common names with at least one
    # approved trade name in `npk_fertigation_products` survive.
    fertigation_pool: Optional[set[str]] = None
    if fertigation:
        fertigation_pool = await load_fertigation_approved_common_name_ids(db)

    candidates: list[Candidate] = []
    skipped_common_names = 0
    for cn_id in common_name_seen:
        vals = acc.get(cn_id)
        if not vals or (vals["N"] == 0 and vals["P"] == 0 and vals["K"] == 0):
            skipped_common_names += 1
            continue
        cn_core = cores_by_id.get(cn_id)
        if cn_core is None:
            skipped_common_names += 1
            continue
        if fertigation_pool is not None and cn_id not in fertigation_pool:
            # Not a Cosh gap — intentional water-soluble filter. Don't
            # count toward skipped (the diagnostic is about missing data,
            # not about the spec's filter).
            continue
        name = (cn_core.translations or {}).get("en") or cn_id
        candidates.append(Candidate(
            cosh_id=cn_id, name=name,
            concentration=Concentration(
                n=vals["N"], p=vals["P"], k=vals["K"],
            ),
            # water_soluble is now a derived attribute on Candidate that
            # mirrors the filter we just applied. Kept for downstream
            # surfaces (e.g. PWA chip) until the broader brand-side
            # water-soluble flag arrives.
            water_soluble=fertigation_pool is None or cn_id in fertigation_pool,
        ))

    return candidates, skipped + skipped_common_names
