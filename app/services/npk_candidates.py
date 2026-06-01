"""Build NPK candidate list from the real Cosh data shape.

Cosh contract (synced 2026-06-01 — confirmed against testing payload):

  Cores
    common_names_of_inputs            — the fertiliser identity
    fert_nutrients                    — 3 rows: Nitrogen / Phosphorus / Potassium
    straight_complex                  — 2 rows: Straight fertilizer / Complex fertilizer
    fert_nutrient_concentration_core  — concentration values. translations.en
                                        carries the numeric percentage as text
                                        ("8", "10", "16.5", "46", etc.).

  Connect (4-endpoint)
    fert_nutrient_concentration
      role=common_names_of_inputs            position 1
      role=straight_complex                  position 2
      role=fert_nutrients                    position 3
      role=fert_nutrient_concentration_core  position 4

A Straight fertiliser has ONE Connect row (one nutrient). A Complex
(Mixed) fertiliser has TWO or THREE Connect rows — one per nutrient
it carries. We group by common_name and assemble a {N, P, K} tuple
per fertiliser.

Water-soluble filtering (spec §5.1, Fertigation flow) — DEFERRED.
This dataset has no water-soluble flag; for now every candidate is
treated as water_soluble=True. The Fertigation flow runs over the
full pool until Cosh ships a flag (or we read it from formulation
via tradename_commonname / tradename_formulation chain). Tracked
in project memory.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_COMMON_NAMES_CORE,
    COSH_FERT_NUTRIENT_CONCENTRATION_CONNECT,
    COSH_FERT_NUTRIENT_CONCENTRATION_CORE,
    COSH_FERT_NUTRIENTS_CORE,
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


async def load_fertiliser_candidates(
    db: AsyncSession,
) -> tuple[list[Candidate], int]:
    """Return (candidates, skipped_count).

    `skipped_count` counts common_name rows referenced by the Connect
    that could not be fully resolved (missing concentration, unknown
    nutrient name, all-zero NPK, etc.) — surfaced by the endpoint so
    a CM can spot Cosh-side gaps.
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
        name = (cn_core.translations or {}).get("en") or cn_id
        candidates.append(Candidate(
            cosh_id=cn_id, name=name,
            concentration=Concentration(
                n=vals["N"], p=vals["P"], k=vals["K"],
            ),
            water_soluble=True,  # see module docstring — deferred filter
        ))

    return candidates, skipped + skipped_common_names
