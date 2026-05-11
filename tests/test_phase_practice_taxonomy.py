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
    # Plant-wise extras appended since this L2 is in
    # PLANT_WISE_EXTRAS_APPLY_TO.
    assert "VOLUME_PER_PLANT" in names
    assert "VOLUME_PER_PLANT_UNIT" in names


def test_list_l2_elements_does_not_append_plant_wise_for_excluded(l2_excluded="INSECT_TRAPS"):
    """INSECT_TRAPS isn't in PLANT_WISE_EXTRAS_APPLY_TO; its element
    spec must NOT include VOLUME_PER_PLANT."""
    out = list_l2_elements(l2_excluded)
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
