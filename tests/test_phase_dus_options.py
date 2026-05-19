"""Batch W (2026-05-19) — Cosh `dus_characters_descriptors` Connect
read-through for the Seed Varieties DUS picker.

Tests the `list_dus_options_for_crop` service: seeds a few rows
across two parts / characters / descriptors and verifies the
nested tree shape.
"""
from __future__ import annotations

import pytest

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_BIOLOGICAL_NAMES_CORE,
    COSH_BLANK_BOX_BY_CORE,
    COSH_DUS_CHARACTERS_CORE,
    COSH_DUS_CHARACTERS_DESCRIPTORS_CONNECT,
    COSH_DUS_DESCRIPTORS_CORE,
    COSH_PLANT_PARTS_CORE,
    COSH_PLANT_SUBPARTS_CORE,
    DCD_POS_CHARACTER,
    DCD_POS_CROP,
    DCD_POS_DESCRIPTOR,
    DCD_POS_PART,
    DCD_POS_SUBPART,
)
from app.services.cosh_dus_view import list_dus_options_for_crop
from tests.conftest import requires_docker


async def _seed_core(db, *, cosh_id, core_type, name, status="active"):
    db.add(CoshCoreItem(
        cosh_id=cosh_id, core_type=core_type,
        status=status, translations={"en": name},
    ))


async def _seed_row(db, *, connect_id, crop, part, subpart, character, descriptor):
    db.add(CoshConnectRow(
        connect_id=connect_id,
        connect_type=COSH_DUS_CHARACTERS_DESCRIPTORS_CONNECT,
        status="active",
        endpoints=[
            {"role": COSH_BIOLOGICAL_NAMES_CORE, "cosh_id": crop,       "position": DCD_POS_CROP},
            {"role": COSH_PLANT_PARTS_CORE,      "cosh_id": part,       "position": DCD_POS_PART},
            {"role": COSH_PLANT_SUBPARTS_CORE,   "cosh_id": subpart,    "position": DCD_POS_SUBPART},
            {"role": COSH_DUS_CHARACTERS_CORE,   "cosh_id": character,  "position": DCD_POS_CHARACTER},
            {"role": COSH_DUS_DESCRIPTORS_CORE,  "cosh_id": descriptor, "position": DCD_POS_DESCRIPTOR},
        ],
    ))


@requires_docker
@pytest.mark.asyncio
async def test_returns_empty_when_crop_has_no_rows(db):
    out = await list_dus_options_for_crop(db, crop_cosh_id="bn:does_not_exist")
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_nests_part_subpart_character_descriptors(db):
    crop = "bn:tomato"
    await _seed_core(db, cosh_id=crop,    core_type=COSH_BIOLOGICAL_NAMES_CORE, name="Tomato")
    await _seed_core(db, cosh_id="part:leaf",     core_type=COSH_PLANT_PARTS_CORE,    name="Leaf")
    await _seed_core(db, cosh_id="part:fruit",    core_type=COSH_PLANT_PARTS_CORE,    name="Fruit")
    await _seed_core(db, cosh_id="sub:lamina",    core_type=COSH_PLANT_SUBPARTS_CORE, name="Lamina")
    await _seed_core(db, cosh_id="sub:pericarp",  core_type=COSH_PLANT_SUBPARTS_CORE, name="Pericarp")
    await _seed_core(db, cosh_id="ch:color",      core_type=COSH_DUS_CHARACTERS_CORE, name="Color")
    await _seed_core(db, cosh_id="ch:shape",      core_type=COSH_DUS_CHARACTERS_CORE, name="Shape")
    await _seed_core(db, cosh_id="desc:green",    core_type=COSH_DUS_DESCRIPTORS_CORE, name="Green")
    await _seed_core(db, cosh_id="desc:red",      core_type=COSH_DUS_DESCRIPTORS_CORE, name="Red")
    await _seed_core(db, cosh_id="desc:round",    core_type=COSH_DUS_DESCRIPTORS_CORE, name="Round")
    await _seed_row(db, connect_id="dus:1", crop=crop, part="part:leaf",  subpart="sub:lamina",   character="ch:color", descriptor="desc:green")
    await _seed_row(db, connect_id="dus:2", crop=crop, part="part:fruit", subpart="sub:pericarp", character="ch:color", descriptor="desc:red")
    await _seed_row(db, connect_id="dus:3", crop=crop, part="part:fruit", subpart="sub:pericarp", character="ch:shape", descriptor="desc:round")
    await db.commit()

    out = await list_dus_options_for_crop(db, crop_cosh_id=crop)

    # Two parts. Sorted alphabetically.
    assert [p["part_name_en"] for p in out] == ["Fruit", "Leaf"]

    fruit = next(p for p in out if p["part_cosh_id"] == "part:fruit")
    assert [s["subpart_name_en"] for s in fruit["subparts"]] == ["Pericarp"]
    pericarp = fruit["subparts"][0]
    # Two characters on Fruit / Pericarp; Color appears before Shape.
    char_names = [c["character_name_en"] for c in pericarp["characters"]]
    assert char_names == ["Color", "Shape"]
    color = next(c for c in pericarp["characters"] if c["character_cosh_id"] == "ch:color")
    assert [d["descriptor_name_en"] for d in color["descriptors"]] == ["Red"]


@requires_docker
@pytest.mark.asyncio
async def test_blank_box_subpart_collapses_to_null(db):
    """Batch W-1 (2026-05-19): when the Connect row's subpart
    endpoint is the BLANK BOX sentinel, the tree emits
    subpart_cosh_id=None / subpart_name_en=None so the frontend can
    skip the subpart dropdown for that branch."""
    crop = "bn:tomato"
    bb_subpart = COSH_BLANK_BOX_BY_CORE[COSH_PLANT_SUBPARTS_CORE]
    await _seed_core(db, cosh_id=crop,        core_type=COSH_BIOLOGICAL_NAMES_CORE, name="Tomato")
    await _seed_core(db, cosh_id="part:leaf", core_type=COSH_PLANT_PARTS_CORE,    name="Leaf")
    await _seed_core(db, cosh_id=bb_subpart,  core_type=COSH_PLANT_SUBPARTS_CORE, name="BLANK BOX")
    await _seed_core(db, cosh_id="ch:color",  core_type=COSH_DUS_CHARACTERS_CORE, name="Color")
    await _seed_core(db, cosh_id="desc:green", core_type=COSH_DUS_DESCRIPTORS_CORE, name="Green")
    await _seed_row(db, connect_id="dus:bb", crop=crop, part="part:leaf",
                    subpart=bb_subpart, character="ch:color", descriptor="desc:green")
    await db.commit()

    out = await list_dus_options_for_crop(db, crop_cosh_id=crop)
    leaf = next(p for p in out if p["part_cosh_id"] == "part:leaf")
    assert len(leaf["subparts"]) == 1
    only = leaf["subparts"][0]
    assert only["subpart_cosh_id"] is None
    assert only["subpart_name_en"] is None
    # The character + descriptor still surface under the null subpart.
    assert only["characters"][0]["character_name_en"] == "Color"
    assert only["characters"][0]["descriptors"][0]["descriptor_name_en"] == "Green"


@requires_docker
@pytest.mark.asyncio
async def test_blank_box_part_drops_row(db):
    """BLANK BOX at the part level removes the row entirely — no
    meaningful part means nothing for the SE to score."""
    crop = "bn:tomato"
    bb_part = COSH_BLANK_BOX_BY_CORE[COSH_PLANT_PARTS_CORE]
    await _seed_core(db, cosh_id=crop,         core_type=COSH_BIOLOGICAL_NAMES_CORE, name="Tomato")
    await _seed_core(db, cosh_id=bb_part,      core_type=COSH_PLANT_PARTS_CORE,    name="BLANK BOX")
    await _seed_core(db, cosh_id="sub:lamina", core_type=COSH_PLANT_SUBPARTS_CORE, name="Lamina")
    await _seed_core(db, cosh_id="ch:color",   core_type=COSH_DUS_CHARACTERS_CORE, name="Color")
    await _seed_core(db, cosh_id="desc:green", core_type=COSH_DUS_DESCRIPTORS_CORE, name="Green")
    await _seed_row(db, connect_id="dus:bb_part", crop=crop, part=bb_part,
                    subpart="sub:lamina", character="ch:color", descriptor="desc:green")
    await db.commit()

    out = await list_dus_options_for_crop(db, crop_cosh_id=crop)
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_blank_box_subpart_coexists_with_real(db):
    """A part can have a mix: some rows with real subparts, some with
    BLANK BOX. The tree carries both — null subpart sorted first,
    then real subparts alphabetised. The frontend renders an
    additional "— not applicable —" option in that case."""
    crop = "bn:tomato"
    bb_subpart = COSH_BLANK_BOX_BY_CORE[COSH_PLANT_SUBPARTS_CORE]
    await _seed_core(db, cosh_id=crop,        core_type=COSH_BIOLOGICAL_NAMES_CORE, name="Tomato")
    await _seed_core(db, cosh_id="part:leaf", core_type=COSH_PLANT_PARTS_CORE,    name="Leaf")
    await _seed_core(db, cosh_id="sub:lamina", core_type=COSH_PLANT_SUBPARTS_CORE, name="Lamina")
    await _seed_core(db, cosh_id=bb_subpart,  core_type=COSH_PLANT_SUBPARTS_CORE, name="BLANK BOX")
    await _seed_core(db, cosh_id="ch:overall", core_type=COSH_DUS_CHARACTERS_CORE, name="Overall")
    await _seed_core(db, cosh_id="ch:color",   core_type=COSH_DUS_CHARACTERS_CORE, name="Color")
    await _seed_core(db, cosh_id="desc:green", core_type=COSH_DUS_DESCRIPTORS_CORE, name="Green")
    await _seed_row(db, connect_id="dus:bb1", crop=crop, part="part:leaf",
                    subpart=bb_subpart, character="ch:overall", descriptor="desc:green")
    await _seed_row(db, connect_id="dus:real1", crop=crop, part="part:leaf",
                    subpart="sub:lamina", character="ch:color", descriptor="desc:green")
    await db.commit()

    out = await list_dus_options_for_crop(db, crop_cosh_id=crop)
    leaf = next(p for p in out if p["part_cosh_id"] == "part:leaf")
    assert len(leaf["subparts"]) == 2
    # Null subpart sorts first.
    assert leaf["subparts"][0]["subpart_cosh_id"] is None
    assert leaf["subparts"][1]["subpart_cosh_id"] == "sub:lamina"


@requires_docker
@pytest.mark.asyncio
async def test_drops_rows_referencing_inactive_cores(db):
    """If the descriptor (or any other endpoint Core item) is marked
    INACTIVE in Cosh, the row is silently dropped — a retired
    descriptor never surfaces in the picker."""
    crop = "bn:tomato"
    await _seed_core(db, cosh_id=crop,           core_type=COSH_BIOLOGICAL_NAMES_CORE, name="Tomato")
    await _seed_core(db, cosh_id="part:leaf",    core_type=COSH_PLANT_PARTS_CORE,    name="Leaf")
    await _seed_core(db, cosh_id="sub:lamina",   core_type=COSH_PLANT_SUBPARTS_CORE, name="Lamina")
    await _seed_core(db, cosh_id="ch:color",     core_type=COSH_DUS_CHARACTERS_CORE, name="Color")
    await _seed_core(db, cosh_id="desc:green",   core_type=COSH_DUS_DESCRIPTORS_CORE, name="Green")
    await _seed_core(db, cosh_id="desc:old",     core_type=COSH_DUS_DESCRIPTORS_CORE, name="Retired", status="inactive")
    await _seed_row(db, connect_id="dus:keep", crop=crop, part="part:leaf", subpart="sub:lamina", character="ch:color", descriptor="desc:green")
    await _seed_row(db, connect_id="dus:drop", crop=crop, part="part:leaf", subpart="sub:lamina", character="ch:color", descriptor="desc:old")
    await db.commit()

    out = await list_dus_options_for_crop(db, crop_cosh_id=crop)
    leaf = next(p for p in out if p["part_cosh_id"] == "part:leaf")
    color = leaf["subparts"][0]["characters"][0]
    descs = [d["descriptor_name_en"] for d in color["descriptors"]]
    # "Retired" descriptor is gone; only "Green" survives.
    assert descs == ["Green"]
