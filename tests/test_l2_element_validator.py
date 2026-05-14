"""
L2 Element Validator — unit tests.

Mocks the cascade service's `list_core_options` / `list_cascade_options`
functions so the validator's logic can be exercised without a live DB.
The cascade service itself is covered by integration tests in 4C-i.D.
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.services.l2_element_validator import (
    validate_l2_elements, ValidationResult,
)
from app.services.cosh_cascade import CascadeOption


def el(element_type, *, cosh_ref=None, value=None):
    """Build an element dict (validator accepts dicts or SQLAlchemy rows)."""
    return {"element_type": element_type, "cosh_ref": cosh_ref, "value": value}


def codes(result: ValidationResult) -> list[str]:
    return [e.code for e in result.errors]


def patches(*, core_returns=None, cascade_returns=None):
    """Patch the cascade service functions used by the validator.

    `core_returns`: dict[entity_type, list[CascadeOption]]
    `cascade_returns`: dict[(name, frozenset(inputs.items())), list[CascadeOption]]

    For unspecified inputs, returns []."""

    async def _core(_db, entity_type):
        return (core_returns or {}).get(entity_type, [])

    async def _cascade(_db, name, inputs):
        key = (name, frozenset(inputs.items()))
        return (cascade_returns or {}).get(key, [])

    return patch.multiple(
        "app.services.l2_element_validator",
        list_core_options=AsyncMock(side_effect=_core),
        list_cascade_options=AsyncMock(side_effect=_cascade),
    )


# ── UNKNOWN_L2 ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_l2_returns_single_error():
    with patches():
        r = await validate_l2_elements(db=None, l2_type="NOT_A_REAL_L2", elements=[])
    assert not r.is_valid
    assert codes(r) == ["UNKNOWN_L2"]


# ── Mandatory fields ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_mandatory_fields_flagged():
    """CHEMICAL_PESTICIDES with no elements → mandatory ones all flagged."""
    with patches():
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES", elements=[],
        )
    expected = {"COMMON_NAME", "APPLICATION_METHOD", "DOSAGE", "DOSAGE_UNIT"}
    missing = {e.field_name for e in r.errors if e.code == "MISSING_MANDATORY"}
    assert expected.issubset(missing), f"missing: {missing}, expected superset of {expected}"


# ── mandatory_if_set ────────────────────────────────────────────────────────
#
# The legacy "BRAND_NAME mandatory_if_set=(MANUFACTURER,)" rule was
# dropped in Batch 24 (2026-05-14, per user). MFR and BRAND_NAME are
# now independent optional peers — bidirectional cascade in the UI,
# but no validator constraint between them.

@pytest.mark.asyncio
async def test_brand_optional_even_when_manufacturer_set():
    """MFR set without BRAND_NAME → still valid; no MISSING_CONDITIONAL."""
    elements = [
        el("COMMON_NAME", cosh_ref="cn:imida"),
        el("MANUFACTURER", cosh_ref="Bayer"),
        # BRAND_NAME omitted on purpose — expert remembers the maker
        # but not the brand; this used to error, now it doesn't.
        el("APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        el("DOSAGE", value="0.5"),
        el("DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
    ]
    core = {
        "common_name":        [CascadeOption("cn:imida", "Imidacloprid")],
        "application_method": [CascadeOption("am:foliar_spray", "Foliar spray")],
        "dosage_unit":        [CascadeOption("du:ml_per_l", "ml/L")],
    }
    cascade = {
        ("manufacturers_for_common_name", frozenset({("COMMON_NAME", "cn:imida")})):
            [CascadeOption("Bayer", "Bayer")],
    }
    with patches(core_returns=core, cascade_returns=cascade):
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES", elements=elements,
        )
    assert not any(e.code == "MISSING_CONDITIONAL" for e in r.errors), r.errors


@pytest.mark.asyncio
async def test_brand_optional_when_manufacturer_blank():
    """No MANUFACTURER → no BRAND_NAME required (no conditional error)."""
    elements = [
        el("COMMON_NAME", cosh_ref="cn:imida"),
        el("APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        el("DOSAGE", value="0.5"),
        el("DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
    ]
    core = {
        "common_name":        [CascadeOption("cn:imida", "Imidacloprid")],
        "application_method": [CascadeOption("am:foliar_spray", "Foliar spray")],
        "dosage_unit":        [CascadeOption("du:ml_per_l", "ml/L")],
    }
    with patches(core_returns=core):
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES", elements=elements,
        )
    assert not any(e.code == "MISSING_CONDITIONAL" for e in r.errors), r.errors


# ── Cascade integrity ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_manufacturer_caught_via_cascade():
    """SE picks a manufacturer NOT in Cosh's cascade output → CASCADE_VIOLATION."""
    elements = [
        el("COMMON_NAME", cosh_ref="cn:imida"),
        el("MANUFACTURER", cosh_ref="UnknownVendor"),  # not in cascade
        el("APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        el("DOSAGE", value="0.5"),
        el("DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
    ]
    core = {
        "common_name":        [CascadeOption("cn:imida", "Imidacloprid")],
        "application_method": [CascadeOption("am:foliar_spray", "Foliar spray")],
        "dosage_unit":        [CascadeOption("du:ml_per_l", "ml/L")],
    }
    cascade = {
        ("manufacturers_for_common_name", frozenset({("COMMON_NAME", "cn:imida")})):
            [CascadeOption("Bayer", "Bayer")],
    }
    with patches(core_returns=core, cascade_returns=cascade):
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES", elements=elements,
        )
    violations = [e for e in r.errors if e.code == "CASCADE_VIOLATION"]
    assert len(violations) == 1
    assert violations[0].field_name == "MANUFACTURER"


# ── Auto-selected fields ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_selected_must_match_cascade_output():
    """FORMULATION value must equal the cascade's deterministic output."""
    elements = [
        el("COMMON_NAME", cosh_ref="cn:imida"),
        el("MANUFACTURER", cosh_ref="Bayer"),
        el("BRAND_NAME", cosh_ref="brand:confidor"),
        el("FORMULATION", cosh_ref="form:WRONG"),    # cascade says form:SC
        el("AI_CONCENTRATION", cosh_ref="17.8% SL"),
        el("APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        el("DOSAGE", value="0.5"),
        el("DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
    ]
    core = {
        "common_name":        [CascadeOption("cn:imida", "Imidacloprid")],
        "application_method": [CascadeOption("am:foliar_spray", "Foliar spray")],
        "dosage_unit":        [CascadeOption("du:ml_per_l", "ml/L")],
    }
    cascade = {
        ("manufacturers_for_common_name", frozenset({("COMMON_NAME", "cn:imida")})):
            [CascadeOption("Bayer", "Bayer")],
        ("brands_for_common_name_and_manufacturer",
         frozenset({("COMMON_NAME", "cn:imida")})):
            [CascadeOption("brand:confidor", "Confidor")],
        ("formulation_for_brand", frozenset({("BRAND_NAME", "brand:confidor")})):
            [CascadeOption("form:SC", "SC")],
        ("ai_concentration_for_brand", frozenset({("BRAND_NAME", "brand:confidor")})):
            [CascadeOption("17.8% SL", "17.8% SL")],
    }
    with patches(core_returns=core, cascade_returns=cascade):
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES", elements=elements,
        )
    # Auto-selected check fires via CASCADE_VIOLATION first (value not in
    # cascade output of one option). Either CASCADE_VIOLATION or
    # AUTO_SELECTED_OVERRIDE is acceptable — both signal the same problem.
    flagged = [e for e in r.errors if e.field_name == "FORMULATION"]
    assert flagged, f"FORMULATION not flagged; errors={r.errors}"
    assert flagged[0].code in ("CASCADE_VIOLATION", "AUTO_SELECTED_OVERRIDE")


@pytest.mark.asyncio
async def test_auto_selected_missing_when_upstream_complete():
    """BRAND_NAME set → FORMULATION must be present."""
    elements = [
        el("COMMON_NAME", cosh_ref="cn:imida"),
        el("MANUFACTURER", cosh_ref="Bayer"),
        el("BRAND_NAME", cosh_ref="brand:confidor"),
        # FORMULATION / AI_CONCENTRATION omitted
        el("APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        el("DOSAGE", value="0.5"),
        el("DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
    ]
    core = {
        "common_name":        [CascadeOption("cn:imida", "Imidacloprid")],
        "application_method": [CascadeOption("am:foliar_spray", "Foliar spray")],
        "dosage_unit":        [CascadeOption("du:ml_per_l", "ml/L")],
    }
    cascade = {
        ("manufacturers_for_common_name", frozenset({("COMMON_NAME", "cn:imida")})):
            [CascadeOption("Bayer", "Bayer")],
        ("brands_for_common_name_and_manufacturer",
         frozenset({("COMMON_NAME", "cn:imida")})):
            [CascadeOption("brand:confidor", "Confidor")],
        ("formulation_for_brand", frozenset({("BRAND_NAME", "brand:confidor")})):
            [CascadeOption("form:SC", "SC")],
        ("ai_concentration_for_brand", frozenset({("BRAND_NAME", "brand:confidor")})):
            [CascadeOption("17.8% SL", "17.8% SL")],
    }
    with patches(core_returns=core, cascade_returns=cascade):
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES", elements=elements,
        )
    missing_auto = [e for e in r.errors if e.code == "AUTO_SELECTED_MISSING"]
    flagged_fields = {e.field_name for e in missing_auto}
    assert flagged_fields == {"FORMULATION", "AI_CONCENTRATION"}


# ── is_special_input invariant ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_adjuvants_requires_special_input_flag():
    elements = [
        el("COMMON_NAME", cosh_ref="cn:silwet"),
        el("APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        el("DOSAGE", value="0.05"),
        el("DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
    ]
    core = {
        "common_name":        [CascadeOption("cn:silwet", "Silwet")],
        "application_method": [CascadeOption("am:foliar_spray", "Foliar spray")],
        "dosage_unit":        [CascadeOption("du:ml_per_l", "ml/L")],
    }
    with patches(core_returns=core):
        r = await validate_l2_elements(
            db=None, l2_type="ADJUVANTS",
            elements=elements,
            practice_is_special_input=False,
        )
    assert any(e.code == "SPECIAL_INPUT_REQUIRED" for e in r.errors), r.errors


@pytest.mark.asyncio
async def test_non_adjuvant_with_special_flag_rejected():
    elements = [
        el("COMMON_NAME", cosh_ref="cn:imida"),
        el("APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        el("DOSAGE", value="0.5"),
        el("DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
    ]
    core = {
        "common_name":        [CascadeOption("cn:imida", "Imidacloprid")],
        "application_method": [CascadeOption("am:foliar_spray", "Foliar spray")],
        "dosage_unit":        [CascadeOption("du:ml_per_l", "ml/L")],
    }
    with patches(core_returns=core):
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES",
            elements=elements,
            practice_is_special_input=True,
        )
    assert any(e.code == "SPECIAL_INPUT_NOT_ALLOWED" for e in r.errors), r.errors


# ── frequency_based invariant ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_frequency_mismatch_flagged():
    elements = [
        el("N_DOSAGE", value="100"),
        el("P_DOSAGE", value="50"),
        el("K_DOSAGE", value="50"),
        el("UNIT", cosh_ref="du:kg_per_acre"),
        el("FORMULATION", cosh_ref="form:water_soluble"),
        el("APPLICATION_METHOD", cosh_ref="am:fertigation"),
        el("FERTIGATION_INTERVAL", value="7"),  # Practice claims 5
    ]
    core = {
        "dosage_unit":        [CascadeOption("du:kg_per_acre", "kg/acre")],
        "formulation":        [CascadeOption("form:water_soluble", "Water-soluble")],
        "application_method": [CascadeOption("am:fertigation", "Fertigation")],
    }
    with patches(core_returns=core):
        r = await validate_l2_elements(
            db=None, l2_type="FERTIGATION_NPK_DOSAGES",
            elements=elements,
            practice_frequency_days=5,  # mismatch with FERTIGATION_INTERVAL=7
        )
    mismatches = [e for e in r.errors if e.code == "FREQUENCY_MISMATCH"]
    assert len(mismatches) == 1
    assert mismatches[0].details["practice_frequency_days"] == 5
    assert mismatches[0].details["fertigation_interval"] == 7


@pytest.mark.asyncio
async def test_frequency_match_passes():
    elements = [
        el("N_DOSAGE", value="100"),
        el("P_DOSAGE", value="50"),
        el("K_DOSAGE", value="50"),
        el("UNIT", cosh_ref="du:kg_per_acre"),
        el("FORMULATION", cosh_ref="form:water_soluble"),
        el("APPLICATION_METHOD", cosh_ref="am:fertigation"),
        el("FERTIGATION_INTERVAL", value="7"),
    ]
    core = {
        "dosage_unit":        [CascadeOption("du:kg_per_acre", "kg/acre")],
        "formulation":        [CascadeOption("form:water_soluble", "Water-soluble")],
        "application_method": [CascadeOption("am:fertigation", "Fertigation")],
    }
    with patches(core_returns=core):
        r = await validate_l2_elements(
            db=None, l2_type="FERTIGATION_NPK_DOSAGES",
            elements=elements,
            practice_frequency_days=7,
        )
    assert not any(e.code == "FREQUENCY_MISMATCH" for e in r.errors), r.errors


# ── Plant-wise extras ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_volume_per_plant_unit_required_when_volume_set():
    """VOLUME_PER_PLANT set → VOLUME_PER_PLANT_UNIT becomes mandatory."""
    elements = [
        el("COMMON_NAME", cosh_ref="cn:imida"),
        el("APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        el("DOSAGE", value="0.5"),
        el("DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
        el("VOLUME_PER_PLANT", value="0.025"),
        # VOLUME_PER_PLANT_UNIT omitted
    ]
    core = {
        "common_name":        [CascadeOption("cn:imida", "Imidacloprid")],
        "application_method": [CascadeOption("am:foliar_spray", "Foliar spray")],
        "dosage_unit":        [CascadeOption("du:ml_per_l", "ml/L")],
    }
    with patches(core_returns=core):
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES", elements=elements,
        )
    cond = [e for e in r.errors
            if e.code == "MISSING_CONDITIONAL" and e.field_name == "VOLUME_PER_PLANT_UNIT"]
    assert len(cond) == 1


@pytest.mark.asyncio
async def test_plant_wise_extras_rejected_on_non_applicable_l2():
    """INSECT_TRAPS is NOT in PLANT_WISE_EXTRAS_APPLY_TO → those fields
    are unknown for it."""
    elements = [
        el("COMMON_NAME", cosh_ref="cn:trap"),
        el("DOSAGE", value="10"),
        el("DOSAGE_UNIT", cosh_ref="du:per_acre"),
        el("VOLUME_PER_PLANT", value="0.5"),  # not allowed for INSECT_TRAPS
    ]
    core = {
        "common_name": [CascadeOption("cn:trap", "Pheromone trap")],
        "dosage_unit": [CascadeOption("du:per_acre", "per acre")],
    }
    with patches(core_returns=core):
        r = await validate_l2_elements(
            db=None, l2_type="INSECT_TRAPS", elements=elements,
        )
    unknowns = [e for e in r.errors
                if e.code == "UNKNOWN_FIELD" and e.field_name == "VOLUME_PER_PLANT"]
    assert len(unknowns) == 1


# ── Numeric format ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_numeric_caught():
    elements = [
        el("DISTANCE", value="not-a-number"),
        el("DISTANCE_UNIT", cosh_ref="du:cm"),
    ]
    core = {"distance_unit": [CascadeOption("du:cm", "cm")]}
    with patches(core_returns=core):
        r = await validate_l2_elements(
            db=None, l2_type="SPACING_PLANT_TO_PLANT", elements=elements,
        )
    bad = [e for e in r.errors if e.code == "INVALID_NUMERIC"]
    assert len(bad) == 1
    assert bad[0].field_name == "DISTANCE"


# ── Happy path ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_minimal_valid_post_harvest_practice():
    """Instructions-only L2: just one mandatory text field."""
    with patches():
        r = await validate_l2_elements(
            db=None, l2_type="POST_HARVEST_DRYING",
            elements=[el("INSTRUCTIONS", value="Sun-dry for 3 days, turn twice daily")],
        )
    assert r.is_valid, r.errors


@pytest.mark.asyncio
async def test_full_chemical_pesticide_happy_path():
    elements = [
        el("COMMON_NAME", cosh_ref="cn:imida"),
        el("MANUFACTURER", cosh_ref="Bayer"),
        el("BRAND_NAME", cosh_ref="brand:confidor"),
        el("FORMULATION", cosh_ref="form:SC"),
        el("AI_CONCENTRATION", cosh_ref="17.8% SL"),
        el("APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        el("DOSAGE", value="0.5"),
        el("DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
        el("INSTRUCTIONS", value="Apply early morning."),
    ]
    core = {
        "common_name":        [CascadeOption("cn:imida", "Imidacloprid")],
        "application_method": [CascadeOption("am:foliar_spray", "Foliar spray")],
        "dosage_unit":        [CascadeOption("du:ml_per_l", "ml/L")],
    }
    cascade = {
        ("manufacturers_for_common_name", frozenset({("COMMON_NAME", "cn:imida")})):
            [CascadeOption("Bayer", "Bayer")],
        ("brands_for_common_name_and_manufacturer",
         frozenset({("COMMON_NAME", "cn:imida")})):
            [CascadeOption("brand:confidor", "Confidor")],
        ("formulation_for_brand", frozenset({("BRAND_NAME", "brand:confidor")})):
            [CascadeOption("form:SC", "SC")],
        ("ai_concentration_for_brand", frozenset({("BRAND_NAME", "brand:confidor")})):
            [CascadeOption("17.8% SL", "17.8% SL")],
    }
    with patches(core_returns=core, cascade_returns=cascade):
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES", elements=elements,
        )
    assert r.is_valid, r.errors
