"""Cascading filter service over Cosh's `pest_diagnosis` Connect
(synced 2026-05-14, 6,266 active rows).

The farmer-facing Diagnosis pipe asks a sequence of narrowing
questions (crop stage → plant part → subpart → symptom → subsymptom)
and ends with a ranked list of candidate pests + their life stage.
This service is the read-through backend behind those steps.

Wire shape per row (9 endpoints) is pinned in `cosh_constants.py`.
Position 3 holds the pest, position 7 holds the crop — same Core,
disambiguated by position.

BLANK BOX semantics
-------------------
Four of the dimensions ship a BLANK BOX sentinel item meaning "no
data exists for this dimension on this row" — the constraint is
vacuously satisfied. When the farmer pins a value for that
dimension, rows whose endpoint is BLANK BOX still match. BLANK BOX
is never surfaced as a pickable option to the farmer.

The other three dimensions (damage_symptoms, pest_stages,
priority_rank_pests) match strictly today.

No local mirror — diagnosis is pure read-through. Nothing on the
RootsTalk side FKs to these Cosh entities.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_BIOLOGICAL_NAMES_CORE,
    COSH_CROP_STAGES_CORE,
    COSH_DAMAGE_SUBSYMPTOMS_CORE,
    COSH_DAMAGE_SYMPTOMS_CORE,
    COSH_PEST_DIAGNOSIS_CONNECT,
    COSH_PEST_STAGES_CORE,
    COSH_PLANT_PARTS_CORE,
    COSH_PLANT_SUBPARTS_CORE,
    COSH_PRIORITY_RANK_PESTS_CORE,
    PD_BLANK_BOX_BY_CORE,
    PD_POS_CROP,
    PD_POS_CROP_STAGE,
    PD_POS_PEST,
    PD_POS_PEST_STAGE,
    PD_POS_PLANT_PART,
    PD_POS_PLANT_SUBPART,
    PD_POS_PRIORITY_RANK,
    PD_POS_SUBSYMPTOM,
    PD_POS_SYMPTOM,
)


@dataclass(frozen=True)
class DiagnosisFilters:
    """All filters except `crop_cosh_id` are optional. Setting a
    filter narrows downstream queries; leaving it None means
    "don't constrain on this dimension yet."""
    crop_cosh_id: str
    crop_stage: Optional[str] = None
    plant_part: Optional[str] = None
    plant_subpart: Optional[str] = None
    symptom: Optional[str] = None
    subsymptom: Optional[str] = None


def _endpoint_at_position(row: CoshConnectRow, position: int) -> Optional[str]:
    for e in row.endpoints or []:
        if e.get("position") == position:
            return e.get("cosh_id")
    return None


def _matches(
    row_value: Optional[str],
    filter_value: Optional[str],
    blank_box_uuid: Optional[str],
) -> bool:
    """Filter predicate that respects BLANK BOX as a wildcard on the
    row side. `filter_value=None` means "no constraint" (match
    anything). `blank_box_uuid=None` means this dimension has no
    sentinel today (strict match)."""
    if filter_value is None:
        return True
    if row_value == filter_value:
        return True
    if blank_box_uuid is not None and row_value == blank_box_uuid:
        return True
    return False


async def _walk_active_rows(db: AsyncSession) -> list[CoshConnectRow]:
    rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_PEST_DIAGNOSIS_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    return list(rows)


async def _filter_rows(
    db: AsyncSession, filters: DiagnosisFilters,
) -> list[CoshConnectRow]:
    """The crop dimension (position 7) is the one mandatory filter —
    all downstream questions are crop-scoped. The other dimensions
    use BLANK BOX-aware matching where the Core ships a sentinel."""
    out: list[CoshConnectRow] = []
    for r in await _walk_active_rows(db):
        if _endpoint_at_position(r, PD_POS_CROP) != filters.crop_cosh_id:
            continue
        if not _matches(
            _endpoint_at_position(r, PD_POS_CROP_STAGE),
            filters.crop_stage,
            PD_BLANK_BOX_BY_CORE.get(COSH_CROP_STAGES_CORE),
        ):
            continue
        if not _matches(
            _endpoint_at_position(r, PD_POS_PLANT_PART),
            filters.plant_part,
            PD_BLANK_BOX_BY_CORE.get(COSH_PLANT_PARTS_CORE),
        ):
            continue
        if not _matches(
            _endpoint_at_position(r, PD_POS_PLANT_SUBPART),
            filters.plant_subpart,
            PD_BLANK_BOX_BY_CORE.get(COSH_PLANT_SUBPARTS_CORE),
        ):
            continue
        if not _matches(
            _endpoint_at_position(r, PD_POS_SYMPTOM),
            filters.symptom,
            None,  # damage_symptoms has no BLANK BOX
        ):
            continue
        if not _matches(
            _endpoint_at_position(r, PD_POS_SUBSYMPTOM),
            filters.subsymptom,
            PD_BLANK_BOX_BY_CORE.get(COSH_DAMAGE_SUBSYMPTOMS_CORE),
        ):
            continue
        out.append(r)
    return out


def _translation_en(core: Optional[CoshCoreItem], fallback: str) -> str:
    if core is None:
        return fallback
    t = core.translations or {}
    return t.get("en") or t.get("English") or fallback


async def _resolve_core_names(
    db: AsyncSession, *, core_type: str, cosh_ids: set[str],
) -> dict[str, str]:
    """Return {cosh_id: en_name} for the given Core items. Inactive
    items are dropped — keeps stale items out of farmer dropdowns."""
    cosh_ids = {c for c in cosh_ids if c is not None}
    if not cosh_ids:
        return {}
    cores = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == core_type,
            CoshCoreItem.cosh_id.in_(cosh_ids),
            CoshCoreItem.status == "active",
        )
    )).scalars().all()
    return {c.cosh_id: _translation_en(c, c.cosh_id) for c in cores}


def _options_from_rows(
    rows: list[CoshConnectRow], *, position: int, blank_box: Optional[str],
) -> set[str]:
    """Distinct endpoint UUIDs at `position`, with BLANK BOX dropped
    so it never surfaces as a farmer-pickable option."""
    out: set[str] = set()
    for r in rows:
        v = _endpoint_at_position(r, position)
        if v is None:
            continue
        if blank_box is not None and v == blank_box:
            continue
        out.add(v)
    return out


async def _list_dimension(
    db: AsyncSession, filters: DiagnosisFilters, *,
    position: int, core_type: str,
) -> list[dict]:
    rows = await _filter_rows(db, filters)
    cosh_ids = _options_from_rows(
        rows, position=position,
        blank_box=PD_BLANK_BOX_BY_CORE.get(core_type),
    )
    name_by_id = await _resolve_core_names(
        db, core_type=core_type, cosh_ids=cosh_ids,
    )
    items = [
        {"cosh_id": c, "name": name_by_id.get(c, c)}
        for c in cosh_ids if c in name_by_id
    ]
    return sorted(items, key=lambda x: x["name"].casefold())


# ── Dimension lookups ─────────────────────────────────────────────────────

async def list_crop_stages(
    db: AsyncSession, *, crop_cosh_id: str,
) -> list[dict]:
    return await _list_dimension(
        db, DiagnosisFilters(crop_cosh_id=crop_cosh_id),
        position=PD_POS_CROP_STAGE, core_type=COSH_CROP_STAGES_CORE,
    )


async def list_plant_parts(
    db: AsyncSession, *, crop_cosh_id: str,
    crop_stage: Optional[str] = None,
) -> list[dict]:
    return await _list_dimension(
        db, DiagnosisFilters(crop_cosh_id=crop_cosh_id, crop_stage=crop_stage),
        position=PD_POS_PLANT_PART, core_type=COSH_PLANT_PARTS_CORE,
    )


async def list_plant_subparts(
    db: AsyncSession, *, crop_cosh_id: str,
    crop_stage: Optional[str] = None,
    plant_part: Optional[str] = None,
) -> list[dict]:
    return await _list_dimension(
        db, DiagnosisFilters(
            crop_cosh_id=crop_cosh_id, crop_stage=crop_stage,
            plant_part=plant_part,
        ),
        position=PD_POS_PLANT_SUBPART, core_type=COSH_PLANT_SUBPARTS_CORE,
    )


async def list_symptoms(
    db: AsyncSession, *, crop_cosh_id: str,
    crop_stage: Optional[str] = None,
    plant_part: Optional[str] = None,
    plant_subpart: Optional[str] = None,
) -> list[dict]:
    return await _list_dimension(
        db, DiagnosisFilters(
            crop_cosh_id=crop_cosh_id, crop_stage=crop_stage,
            plant_part=plant_part, plant_subpart=plant_subpart,
        ),
        position=PD_POS_SYMPTOM, core_type=COSH_DAMAGE_SYMPTOMS_CORE,
    )


async def list_subsymptoms(
    db: AsyncSession, *, crop_cosh_id: str,
    crop_stage: Optional[str] = None,
    plant_part: Optional[str] = None,
    plant_subpart: Optional[str] = None,
    symptom: Optional[str] = None,
) -> list[dict]:
    return await _list_dimension(
        db, DiagnosisFilters(
            crop_cosh_id=crop_cosh_id, crop_stage=crop_stage,
            plant_part=plant_part, plant_subpart=plant_subpart,
            symptom=symptom,
        ),
        position=PD_POS_SUBSYMPTOM, core_type=COSH_DAMAGE_SUBSYMPTOMS_CORE,
    )


# ── Candidates (the diagnosis result) ─────────────────────────────────────

def _priority_rank_int(name: str) -> int:
    """`priority_rank_pests` ships ranks as English strings "0"–"5".
    Parse them for sort ordering; unparseable values sort last."""
    try:
        return int(name.strip())
    except (ValueError, AttributeError):
        return 999


async def list_candidates(
    db: AsyncSession, *, crop_cosh_id: str,
    crop_stage: Optional[str] = None,
    plant_part: Optional[str] = None,
    plant_subpart: Optional[str] = None,
    symptom: Optional[str] = None,
    subsymptom: Optional[str] = None,
) -> list[dict]:
    """Final diagnosis lookup. Returns candidate pests with their
    life stage + priority rank, sorted by rank (lowest = highest
    priority), tie-broken by pest name.

    Duplicates: multiple rows can share the same (pest, pest_stage)
    if they originate from different symptom/part routes within
    Cosh. We dedupe by (pest, pest_stage) and keep the row whose
    rank resolves to the lowest int — i.e., the most urgent one."""
    filters = DiagnosisFilters(
        crop_cosh_id=crop_cosh_id, crop_stage=crop_stage,
        plant_part=plant_part, plant_subpart=plant_subpart,
        symptom=symptom, subsymptom=subsymptom,
    )
    rows = await _filter_rows(db, filters)

    candidates: list[tuple[str, Optional[str], str]] = []
    pest_ids: set[str] = set()
    stage_ids: set[str] = set()
    rank_ids: set[str] = set()
    for r in rows:
        pest = _endpoint_at_position(r, PD_POS_PEST)
        rank = _endpoint_at_position(r, PD_POS_PRIORITY_RANK)
        if pest is None or rank is None:
            continue
        stage = _endpoint_at_position(r, PD_POS_PEST_STAGE)
        candidates.append((pest, stage, rank))
        pest_ids.add(pest)
        if stage is not None:
            stage_ids.add(stage)
        rank_ids.add(rank)

    pest_names = await _resolve_core_names(
        db, core_type=COSH_BIOLOGICAL_NAMES_CORE, cosh_ids=pest_ids,
    )
    stage_names = await _resolve_core_names(
        db, core_type=COSH_PEST_STAGES_CORE, cosh_ids=stage_ids,
    )
    rank_names = await _resolve_core_names(
        db, core_type=COSH_PRIORITY_RANK_PESTS_CORE, cosh_ids=rank_ids,
    )

    # Dedupe by (pest, stage) keeping the row whose rank int is
    # lowest (= most urgent). Inactive pests drop out via the
    # `pest in pest_names` check after name resolution.
    best: dict[tuple[str, Optional[str]], tuple[int, str]] = {}
    for pest, stage, rank_uuid in candidates:
        if pest not in pest_names:
            continue
        rank_int = _priority_rank_int(rank_names.get(rank_uuid, "999"))
        key = (pest, stage)
        if key not in best or rank_int < best[key][0]:
            best[key] = (rank_int, rank_uuid)

    out = [
        {
            "pest_cosh_id": pest,
            "pest_name": pest_names[pest],
            "pest_stage_cosh_id": stage,
            "pest_stage_name": stage_names.get(stage) if stage else None,
            "priority_rank_cosh_id": rank_uuid,
            "priority_rank": rank_int,
        }
        for (pest, stage), (rank_int, rank_uuid) in best.items()
    ]
    out.sort(key=lambda x: (x["priority_rank"], x["pest_name"].casefold()))
    return out
