"""Read-through cascading filter over Cosh's `pest_diagnosis`
Connect (synced 2026-05-14, 9-endpoint shape).

Seeds minimal Cosh-side Core + Connect rows and asserts:
  • crop scoping (position 7) is strict.
  • BLANK BOX is treated as wildcard on the four dimensions that
    ship it; strict elsewhere.
  • Dropdown options never include BLANK BOX itself.
  • Cascade narrowing works: picking a parent shrinks downstream
    option lists.
  • Candidates are deduped by (pest, stage), keep lowest int rank,
    sorted by rank then name.
"""
from __future__ import annotations

import pytest

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
)
from app.services.pest_diagnosis_view import (
    list_candidates,
    list_crop_stages,
    list_plant_parts,
    list_plant_subparts,
    list_subsymptoms,
    list_symptoms,
)
from tests.conftest import requires_docker


BB_CROP_STAGE = PD_BLANK_BOX_BY_CORE[COSH_CROP_STAGES_CORE]
BB_PLANT_PART = PD_BLANK_BOX_BY_CORE[COSH_PLANT_PARTS_CORE]
BB_PLANT_SUBPART = PD_BLANK_BOX_BY_CORE[COSH_PLANT_SUBPARTS_CORE]
BB_SUBSYMPTOM = PD_BLANK_BOX_BY_CORE[COSH_DAMAGE_SUBSYMPTOMS_CORE]


def _core(cosh_id: str, core_type: str, name: str, status: str = "active") -> CoshCoreItem:
    return CoshCoreItem(
        cosh_id=cosh_id, core_type=core_type,
        translations={"en": name}, status=status,
    )


def _pd_row(
    cid: str, *, crop: str, symptom: str, subsymptom: str,
    pest: str, pest_stage: str, plant_part: str, plant_subpart: str,
    crop_stage: str, rank: str, status: str = "active",
) -> CoshConnectRow:
    return CoshConnectRow(
        connect_id=cid, connect_type=COSH_PEST_DIAGNOSIS_CONNECT,
        status=status,
        endpoints=[
            {"role": COSH_DAMAGE_SYMPTOMS_CORE,     "cosh_id": symptom,        "position": 1},
            {"role": COSH_DAMAGE_SUBSYMPTOMS_CORE,  "cosh_id": subsymptom,     "position": 2},
            {"role": COSH_BIOLOGICAL_NAMES_CORE,    "cosh_id": pest,           "position": 3},
            {"role": COSH_PEST_STAGES_CORE,         "cosh_id": pest_stage,     "position": 4},
            {"role": COSH_PLANT_PARTS_CORE,         "cosh_id": plant_part,     "position": 5},
            {"role": COSH_PLANT_SUBPARTS_CORE,      "cosh_id": plant_subpart,  "position": 6},
            {"role": COSH_BIOLOGICAL_NAMES_CORE,    "cosh_id": crop,           "position": 7},
            {"role": COSH_CROP_STAGES_CORE,         "cosh_id": crop_stage,     "position": 8},
            {"role": COSH_PRIORITY_RANK_PESTS_CORE, "cosh_id": rank,           "position": 9},
        ],
    )


async def _seed_minimal_world(db) -> None:
    """A small Cosh world: two crops (tomato, mango), three pests,
    three symptoms, a couple of stages, two ranks. Connect rows
    target tomato by default; mango added so we can prove crop
    scoping isolates them."""
    db.add(_core("crop:tomato", COSH_BIOLOGICAL_NAMES_CORE, "Tomato"))
    db.add(_core("crop:mango", COSH_BIOLOGICAL_NAMES_CORE, "Mango"))

    db.add(_core("pest:aphid", COSH_BIOLOGICAL_NAMES_CORE, "Aphid"))
    db.add(_core("pest:whitefly", COSH_BIOLOGICAL_NAMES_CORE, "Whitefly"))
    db.add(_core("pest:mite", COSH_BIOLOGICAL_NAMES_CORE, "Spider Mite"))

    db.add(_core("sym:curl", COSH_DAMAGE_SYMPTOMS_CORE, "Leaf Curl"))
    db.add(_core("sym:spots", COSH_DAMAGE_SYMPTOMS_CORE, "Spots"))
    db.add(_core("sym:webbing", COSH_DAMAGE_SYMPTOMS_CORE, "Webbing"))

    db.add(_core("sub:upward", COSH_DAMAGE_SUBSYMPTOMS_CORE, "Upward Curl"))
    db.add(_core("sub:downward", COSH_DAMAGE_SUBSYMPTOMS_CORE, "Downward Curl"))
    db.add(_core(BB_SUBSYMPTOM, COSH_DAMAGE_SUBSYMPTOMS_CORE, "BLANK BOX"))

    db.add(_core("ps:all", COSH_PEST_STAGES_CORE, "All Stage"))
    db.add(_core("ps:adult", COSH_PEST_STAGES_CORE, "Adult"))

    db.add(_core("pp:leaf", COSH_PLANT_PARTS_CORE, "LEAF"))
    db.add(_core("pp:fruit", COSH_PLANT_PARTS_CORE, "FRUIT"))
    db.add(_core(BB_PLANT_PART, COSH_PLANT_PARTS_CORE, "BLANK BOX"))

    db.add(_core("psp:lamina", COSH_PLANT_SUBPARTS_CORE, "Lamina"))
    db.add(_core(BB_PLANT_SUBPART, COSH_PLANT_SUBPARTS_CORE, "BLANK BOX"))

    db.add(_core("cs:flowering", COSH_CROP_STAGES_CORE, "Flowering"))
    db.add(_core("cs:fruiting", COSH_CROP_STAGES_CORE, "Fruiting"))
    db.add(_core(BB_CROP_STAGE, COSH_CROP_STAGES_CORE, "BLANK BOX"))

    db.add(_core("rank:0", COSH_PRIORITY_RANK_PESTS_CORE, "0"))
    db.add(_core("rank:1", COSH_PRIORITY_RANK_PESTS_CORE, "1"))
    db.add(_core("rank:3", COSH_PRIORITY_RANK_PESTS_CORE, "3"))

    # 4 Connect rows for Tomato:
    #   r1 — Leaf Curl / Upward Curl / Aphid (adult) on Leaf/Lamina
    #        at Flowering stage, rank 1.
    db.add(_pd_row(
        "r1", crop="crop:tomato",
        symptom="sym:curl", subsymptom="sub:upward",
        pest="pest:aphid", pest_stage="ps:adult",
        plant_part="pp:leaf", plant_subpart="psp:lamina",
        crop_stage="cs:flowering", rank="rank:1",
    ))
    #   r2 — Leaf Curl / BLANK BOX subsymptom / Whitefly (all stage)
    #        on Leaf/BLANK BOX subpart at Flowering, rank 0.
    db.add(_pd_row(
        "r2", crop="crop:tomato",
        symptom="sym:curl", subsymptom=BB_SUBSYMPTOM,
        pest="pest:whitefly", pest_stage="ps:all",
        plant_part="pp:leaf", plant_subpart=BB_PLANT_SUBPART,
        crop_stage="cs:flowering", rank="rank:0",
    ))
    #   r3 — Spots / Downward Curl / Mite (all stage) on
    #        Fruit/BLANK BOX subpart at Fruiting, rank 3.
    db.add(_pd_row(
        "r3", crop="crop:tomato",
        symptom="sym:spots", subsymptom="sub:downward",
        pest="pest:mite", pest_stage="ps:all",
        plant_part="pp:fruit", plant_subpart=BB_PLANT_SUBPART,
        crop_stage="cs:fruiting", rank="rank:3",
    ))
    #   r4 — Webbing / BLANK BOX subsymptom / Mite (all stage) on
    #        BLANK BOX plant_part at BLANK BOX crop_stage, rank 0.
    #        (Tests BLANK BOX as wildcard on plant_part + crop_stage.)
    db.add(_pd_row(
        "r4", crop="crop:tomato",
        symptom="sym:webbing", subsymptom=BB_SUBSYMPTOM,
        pest="pest:mite", pest_stage="ps:all",
        plant_part=BB_PLANT_PART, plant_subpart=BB_PLANT_SUBPART,
        crop_stage=BB_CROP_STAGE, rank="rank:0",
    ))

    # Mango row — should never leak into tomato queries.
    db.add(_pd_row(
        "rm1", crop="crop:mango",
        symptom="sym:curl", subsymptom="sub:upward",
        pest="pest:aphid", pest_stage="ps:adult",
        plant_part="pp:leaf", plant_subpart="psp:lamina",
        crop_stage="cs:flowering", rank="rank:1",
    ))
    await db.commit()


# ── Crop scoping ──────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_crop_scoping_isolates_crops(db):
    await _seed_minimal_world(db)
    # tomato has 4 rows touching Flowering + Fruiting + BLANK BOX
    # crop_stage; only Flowering and Fruiting surface (BLANK BOX
    # dropped from option list).
    stages = await list_crop_stages(db, crop_cosh_id="crop:tomato")
    names = sorted(s["name"] for s in stages)
    assert names == ["Flowering", "Fruiting"]
    # mango has only one row → only Flowering.
    mango_stages = await list_crop_stages(db, crop_cosh_id="crop:mango")
    assert [s["name"] for s in mango_stages] == ["Flowering"]


# ── BLANK BOX option-list filtering ───────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_blank_box_never_appears_in_options(db):
    await _seed_minimal_world(db)
    # The Cores include BLANK BOX items, and Connect rows reference
    # them — but the option lists must scrub them out.
    parts = await list_plant_parts(db, crop_cosh_id="crop:tomato")
    assert all(p["name"] != "BLANK BOX" for p in parts)
    subparts = await list_plant_subparts(db, crop_cosh_id="crop:tomato")
    assert all(p["name"] != "BLANK BOX" for p in subparts)
    subs = await list_subsymptoms(db, crop_cosh_id="crop:tomato")
    assert all(s["name"] != "BLANK BOX" for s in subs)


# ── BLANK BOX wildcard match ──────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_blank_box_matches_any_filter_value(db):
    """r4 has plant_part=BLANK BOX. When the farmer pins plant_part
    to LEAF, r4 should still match (BLANK BOX = no data exists for
    this dim on this row → constraint vacuously satisfied)."""
    await _seed_minimal_world(db)
    # Without plant_part filter: r4's Mite/Webbing route surfaces.
    candidates_all = await list_candidates(
        db, crop_cosh_id="crop:tomato", symptom="sym:webbing",
    )
    assert any(c["pest_name"] == "Spider Mite" for c in candidates_all)
    # Pinning plant_part=LEAF still surfaces Mite (r4 matches via
    # BLANK BOX wildcard).
    candidates_leaf = await list_candidates(
        db, crop_cosh_id="crop:tomato", symptom="sym:webbing",
        plant_part="pp:leaf",
    )
    assert any(c["pest_name"] == "Spider Mite" for c in candidates_leaf)


@requires_docker
@pytest.mark.asyncio
async def test_strict_match_on_dimensions_without_blank_box(db):
    """damage_symptoms has no BLANK BOX → filtering by symptom is
    strict. Row r1 has symptom=Leaf Curl; querying for Spots must
    not match it."""
    await _seed_minimal_world(db)
    cands = await list_candidates(
        db, crop_cosh_id="crop:tomato", symptom="sym:spots",
    )
    # Only r3 (Mite, Spots, rank 3) survives.
    assert [c["pest_name"] for c in cands] == ["Spider Mite"]
    assert cands[0]["priority_rank"] == 3


# ── Cascade narrowing ────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_plant_parts_narrow_when_crop_stage_set(db):
    await _seed_minimal_world(db)
    # All parts (less BLANK BOX) for tomato: LEAF and FRUIT
    # (r4 has BLANK BOX, omitted).
    all_parts = await list_plant_parts(db, crop_cosh_id="crop:tomato")
    assert sorted(p["name"] for p in all_parts) == ["FRUIT", "LEAF"]
    # Filter to Flowering: r1 + r2 → both target LEAF. r4 (BLANK
    # BOX crop_stage) still matches Flowering as wildcard, but its
    # plant_part is BLANK BOX → omitted from list.
    flowering_parts = await list_plant_parts(
        db, crop_cosh_id="crop:tomato", crop_stage="cs:flowering",
    )
    assert [p["name"] for p in flowering_parts] == ["LEAF"]


@requires_docker
@pytest.mark.asyncio
async def test_symptoms_narrow_through_full_cascade(db):
    await _seed_minimal_world(db)
    # Pin crop_stage=Flowering, plant_part=Leaf, plant_subpart=Lamina.
    # Rows that survive:
    #   r1 (Curl, Upward, Aphid, Leaf/Lamina, Flowering) — exact
    #   r2 (Curl, BLANK BOX, Whitefly, Leaf/BLANK BOX, Flowering) —
    #       subpart wildcards Lamina via BLANK BOX
    #   r4 (Webbing, BLANK BOX, Mite, BLANK BOX/BLANK BOX, BLANK BOX) —
    #       part + subpart + crop_stage all BLANK BOX wildcards
    #   r3 (Spots, Spots-on-Fruit) — plant_part=fruit, strict
    #       mismatch with Leaf → dropped
    # Distinct symptoms surviving: Leaf Curl, Webbing.
    syms = await list_symptoms(
        db, crop_cosh_id="crop:tomato",
        crop_stage="cs:flowering",
        plant_part="pp:leaf", plant_subpart="psp:lamina",
    )
    assert sorted(s["name"] for s in syms) == ["Leaf Curl", "Webbing"]
    # When we then narrow to symptom=Webbing, only r4 survives.
    cands = await list_candidates(
        db, crop_cosh_id="crop:tomato",
        crop_stage="cs:flowering",
        plant_part="pp:leaf", plant_subpart="psp:lamina",
        symptom="sym:webbing",
    )
    assert [c["pest_name"] for c in cands] == ["Spider Mite"]


# ── Candidates: dedup + ranking ──────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_candidates_sort_by_priority_rank_then_name(db):
    await _seed_minimal_world(db)
    # All four tomato rows surface; sort by rank int then name:
    #   r2 (Whitefly, all, rank 0)
    #   r4 (Spider Mite, all, rank 0)
    #   r1 (Aphid, adult, rank 1)
    #   r3 (Spider Mite, all, rank 3) — dedupe vs r4: same
    #      (Mite, all stage) key, keep rank 0 not 3.
    cands = await list_candidates(db, crop_cosh_id="crop:tomato")
    names_in_order = [c["pest_name"] for c in cands]
    # Two rank-0 first (alphabetical), then Aphid rank 1.
    # No duplicate Spider Mite (deduped via key).
    assert names_in_order == ["Spider Mite", "Whitefly", "Aphid"]
    assert cands[0]["priority_rank"] == 0
    assert cands[1]["priority_rank"] == 0
    assert cands[2]["priority_rank"] == 1


@requires_docker
@pytest.mark.asyncio
async def test_candidates_drops_inactive_pest(db):
    await _seed_minimal_world(db)
    # Mark Whitefly inactive on the Cosh side and re-fetch.
    from sqlalchemy import select
    whitefly = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "pest:whitefly")
    )).scalar_one()
    whitefly.status = "inactive"
    await db.commit()

    cands = await list_candidates(db, crop_cosh_id="crop:tomato")
    assert all(c["pest_name"] != "Whitefly" for c in cands)


@requires_docker
@pytest.mark.asyncio
async def test_inactive_connect_row_ignored(db):
    await _seed_minimal_world(db)
    from sqlalchemy import select
    r4 = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.connect_id == "r4")
    )).scalar_one()
    r4.status = "inactive"
    await db.commit()
    cands = await list_candidates(
        db, crop_cosh_id="crop:tomato", symptom="sym:webbing",
    )
    # r4 was the only Webbing row → no candidates now.
    assert cands == []


@requires_docker
@pytest.mark.asyncio
async def test_no_rows_for_crop_returns_empty(db):
    await _seed_minimal_world(db)
    stages = await list_crop_stages(db, crop_cosh_id="crop:nonexistent")
    assert stages == []
    cands = await list_candidates(db, crop_cosh_id="crop:nonexistent")
    assert cands == []
