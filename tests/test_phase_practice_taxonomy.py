"""Practice taxonomy endpoints — L0/L1/L2 hierarchy + per-L2
element spec.

Surfaces the rule book in `app/services/l2_element_rules.py` as
a pure-data API the SA and CA portals consume for cascading
Practice dropdowns + element-form rendering. No DB access; no
auth gate.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.advisory.router import (
    get_l2_element_spec, get_practice_taxonomy_endpoint,
)
from app.services.practice_taxonomy import (
    TAXONOMY, get_practice_taxonomy, is_known_l2, list_all_l2_ids,
    list_l2_elements,
)
from tests.conftest import requires_docker
from tests.factories import make_user


# ── Taxonomy structure ─────────────────────────────────────────────────────

def test_taxonomy_covers_every_l2_in_the_rule_book():
    """No L2 in `l2_element_rules.py` should be orphaned from
    the hierarchy. Every L2 must sit under some L0+L1."""
    from app.services.l2_element_rules import L2_ELEMENT_RULES
    taxonomy_l2s = set(list_all_l2_ids())
    rule_book_l2s = set(L2_ELEMENT_RULES.keys())
    missing = rule_book_l2s - taxonomy_l2s
    extra = taxonomy_l2s - rule_book_l2s
    assert not missing, f"L2 names in the rule book missing from taxonomy: {missing}"
    assert not extra, f"L2 names in taxonomy not in the rule book: {extra}"


def test_taxonomy_l0_keys_match_practice_l0_enum():
    """L0 keys map 1-1 to the PracticeL0 enum values."""
    from app.modules.advisory.models import PracticeL0
    expected = {e.value for e in PracticeL0}
    assert set(TAXONOMY.keys()) == expected


def test_get_practice_taxonomy_shape():
    out = get_practice_taxonomy()
    assert isinstance(out, list)
    by_id = {row["id"]: row for row in out}
    assert "INPUT" in by_id
    assert "NON_INPUT" in by_id
    # PESTICIDE is under INPUT.
    input_l1s = {entry["id"] for entry in by_id["INPUT"]["l1"]}
    assert "PESTICIDE" in input_l1s
    assert "FERTILIZER" in input_l1s
    # And inside PESTICIDE, CHEMICAL_PESTICIDES is one of the L2s.
    pesticide = next(e for e in by_id["INPUT"]["l1"] if e["id"] == "PESTICIDE")
    l2_ids = {l2["id"] for l2 in pesticide["l2"]}
    assert "CHEMICAL_PESTICIDES" in l2_ids
    assert "INSECT_TRAPS" in l2_ids


def test_labels_are_human_readable():
    out = get_practice_taxonomy()
    pesticide = next(
        e for e in out[0]["l1"]  # INPUT.l1
        if e["id"] == "PESTICIDE"
    )
    chem = next(l2 for l2 in pesticide["l2"] if l2["id"] == "CHEMICAL_PESTICIDES")
    assert chem["label"] == "Chemical Pesticides"


# ── Element spec ────────────────────────────────────────────────────────────

def test_list_l2_elements_returns_rule_book_fields():
    out = list_l2_elements("CHEMICAL_PESTICIDES")
    assert out is not None
    names = [e["name"] for e in out]
    # CHEMICAL_PESTICIDES has the brand triplet up top.
    assert "COMMON_NAME" in names
    assert "BRAND" in names or "BRAND_NAME" in names or any("BRAND" in n for n in names)


def test_list_l2_elements_no_crop_measure_omits_plant_wise(l2="CHEMICAL_PESTICIDES"):
    """User decision 2026-05-11: when no crop_measure is supplied,
    plant-wise dosage extras don't apply (default conservative).
    Previously the rule book always appended them for opt-in L2s."""
    out = list_l2_elements(l2)
    assert out is not None
    assert "VOLUME_PER_PLANT" not in {e["name"] for e in out}


def test_list_l2_elements_plant_wise_crop_appends_extras():
    """PLANT_WISE crop + opt-in L2 → VOLUME_PER_PLANT + UNIT
    appended."""
    out = list_l2_elements("CHEMICAL_PESTICIDES", crop_measure="PLANT_WISE")
    assert out is not None
    names = {e["name"] for e in out}
    assert "VOLUME_PER_PLANT" in names
    assert "VOLUME_PER_PLANT_UNIT" in names


def test_list_l2_elements_area_wise_crop_omits_plant_wise_extras():
    """AREA_WISE crop never sees plant-wise dosage fields, even on
    an opt-in L2."""
    out = list_l2_elements("CHEMICAL_PESTICIDES", crop_measure="AREA_WISE")
    assert out is not None
    assert "VOLUME_PER_PLANT" not in {e["name"] for e in out}


def test_list_l2_elements_excluded_l2_never_gets_extras():
    """INSECT_TRAPS isn't in PLANT_WISE_EXTRAS_APPLY_TO — extras
    don't appear even when crop is PLANT_WISE."""
    out = list_l2_elements("INSECT_TRAPS", crop_measure="PLANT_WISE")
    assert out is not None
    assert "VOLUME_PER_PLANT" not in {e["name"] for e in out}


def test_list_l2_elements_none_for_unknown():
    assert list_l2_elements("UNKNOWN_L2") is None


def test_is_known_l2():
    assert is_known_l2("CHEMICAL_PESTICIDES")
    assert is_known_l2("MEDIA_IMAGE")
    assert not is_known_l2("NOPE")


# ── Endpoints ───────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_taxonomy_endpoint(db):
    user = await make_user(db, name="x")
    out = await get_practice_taxonomy_endpoint(db=db, current_user=user)
    assert any(row["id"] == "INPUT" for row in out)


@requires_docker
@pytest.mark.asyncio
async def test_l2_elements_endpoint_happy_path(db):
    user = await make_user(db, name="x")
    out = await get_l2_element_spec(
        l2_type="CHEMICAL_PESTICIDES", db=db, current_user=user,
    )
    assert out["l2_type"] == "CHEMICAL_PESTICIDES"
    assert any(e["name"] == "COMMON_NAME" for e in out["elements"])
    assert out["crop_measure"] is None
    # No crop_cosh_id → plant-wise extras omitted (conservative
    # default).
    assert "VOLUME_PER_PLANT" not in {e["name"] for e in out["elements"]}


@requires_docker
@pytest.mark.asyncio
async def test_l2_elements_endpoint_plant_wise_crop_appends_extras(db):
    """When crop_cosh_id resolves to PLANT_WISE in the Cosh
    crop_area_plant_wise Connect, the plant-wise extras land on the
    response."""
    from tests.factories import make_crop_reference
    user = await make_user(db, name="x")
    await make_crop_reference(
        db, "crop:apple_test", name="Apple", measure="PLANT_WISE",
    )
    await db.commit()
    out = await get_l2_element_spec(
        l2_type="CHEMICAL_PESTICIDES",
        crop_cosh_id="crop:apple_test",
        db=db, current_user=user,
    )
    assert out["crop_measure"] == "PLANT_WISE"
    names = {e["name"] for e in out["elements"]}
    assert "VOLUME_PER_PLANT" in names
    assert "VOLUME_PER_PLANT_UNIT" in names


@requires_docker
@pytest.mark.asyncio
async def test_l2_elements_endpoint_area_wise_crop_omits_extras(db):
    """AREA_WISE crop never sees the plant-wise fields."""
    from tests.factories import make_crop_reference
    user = await make_user(db, name="x")
    await make_crop_reference(
        db, "crop:tomato_test", name="Tomato", measure="AREA_WISE",
    )
    await db.commit()
    out = await get_l2_element_spec(
        l2_type="CHEMICAL_PESTICIDES",
        crop_cosh_id="crop:tomato_test",
        db=db, current_user=user,
    )
    assert out["crop_measure"] == "AREA_WISE"
    assert "VOLUME_PER_PLANT" not in {e["name"] for e in out["elements"]}


@requires_docker
@pytest.mark.asyncio
async def test_l2_elements_endpoint_unclassified_crop_omits_extras(db):
    """Crop not classified in Cosh yet → measure is None → no
    plant-wise extras. Mirrors the 27/144 unclassified V1 crops."""
    user = await make_user(db, name="x")
    out = await get_l2_element_spec(
        l2_type="CHEMICAL_PESTICIDES",
        crop_cosh_id="crop:ghost",
        db=db, current_user=user,
    )
    assert out["crop_measure"] is None
    assert "VOLUME_PER_PLANT" not in {e["name"] for e in out["elements"]}


@requires_docker
@pytest.mark.asyncio
async def test_l2_elements_endpoint_area_or_plant_hint_appends_extras(db):
    """Bug fix 2026-05-30: PG / CHA-SP recommendations aren't bound
    to a crop, so callers can't supply crop_cosh_id. They pass
    `area_or_plant=PLANT_WISE` directly. The endpoint honours that
    hint and appends the plant-wise extras."""
    user = await make_user(db, name="x")
    out = await get_l2_element_spec(
        l2_type="CHEMICAL_PESTICIDES",
        area_or_plant="PLANT_WISE",
        db=db, current_user=user,
    )
    assert out["crop_measure"] == "PLANT_WISE"
    names = {e["name"] for e in out["elements"]}
    assert "VOLUME_PER_PLANT" in names
    assert "VOLUME_PER_PLANT_UNIT" in names


@requires_docker
@pytest.mark.asyncio
async def test_l2_elements_endpoint_area_or_plant_AREA_WISE_omits(db):
    """AREA_WISE hint never gets the extras (matches the AREA_WISE
    crop case)."""
    user = await make_user(db, name="x")
    out = await get_l2_element_spec(
        l2_type="CHEMICAL_PESTICIDES",
        area_or_plant="AREA_WISE",
        db=db, current_user=user,
    )
    assert out["crop_measure"] == "AREA_WISE"
    assert "VOLUME_PER_PLANT" not in {e["name"] for e in out["elements"]}


@requires_docker
@pytest.mark.asyncio
async def test_l2_elements_endpoint_404_for_unknown(db):
    user = await make_user(db, name="x")
    with pytest.raises(HTTPException) as exc:
        await get_l2_element_spec(
            l2_type="NOPE", db=db, current_user=user,
        )
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "unknown_l2_type"


# ── L2-level metadata flags (Batch 25) ─────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_endpoint_exposes_is_special_input_flag(db):
    """ADJUVANTS is the only L2 with is_special_input=True today.
    Other L2s (e.g. CHEMICAL_PESTICIDES) should report False so the
    frontend doesn't render the Special Input checkbox there."""
    user = await make_user(db, name="x")
    chem = await get_l2_element_spec(
        l2_type="CHEMICAL_PESTICIDES", db=db, current_user=user,
    )
    assert chem["is_special_input"] is False
    adj = await get_l2_element_spec(
        l2_type="ADJUVANTS", db=db, current_user=user,
    )
    assert adj["is_special_input"] is True


@requires_docker
@pytest.mark.asyncio
async def test_endpoint_exposes_frequency_based_flag(db):
    """FERTIGATION_NPK_DOSAGES is frequency-based (carries
    FERTIGATION_INTERVAL); CHEMICAL_PESTICIDES is not. Frontend uses
    the flag to decide whether to require frequency_days."""
    user = await make_user(db, name="x")
    chem = await get_l2_element_spec(
        l2_type="CHEMICAL_PESTICIDES", db=db, current_user=user,
    )
    assert chem["frequency_based"] is False
    fert = await get_l2_element_spec(
        l2_type="FERTIGATION_NPK_DOSAGES", db=db, current_user=user,
    )
    assert fert["frequency_based"] is True
