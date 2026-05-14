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
        "common_names_of_inputs": [CascadeOption("cn:imida", "Imidacloprid")],
        "application_methods":    [CascadeOption("am:foliar_spray", "Foliar spray")],
        "units_data":             [CascadeOption("du:ml_per_l", "ml/L")],
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
        "common_names_of_inputs": [CascadeOption("cn:imida", "Imidacloprid")],
        "application_methods":    [CascadeOption("am:foliar_spray", "Foliar spray")],
        "units_data":             [CascadeOption("du:ml_per_l", "ml/L")],
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
        "common_names_of_inputs": [CascadeOption("cn:imida", "Imidacloprid")],
        "application_methods":    [CascadeOption("am:foliar_spray", "Foliar spray")],
        "units_data":             [CascadeOption("du:ml_per_l", "ml/L")],
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


# ── F + a.i. cascade (Batch 27 — no longer auto-selected) ───────────────────
#
# The pre-Batch-27 contract had FORMULATION and AI_CONCENTRATION
# auto_selected with cascade_from=BRAND_NAME — meaning BRAND_NAME was
# required before they activated, and once BRAND_NAME was set they
# became mandatory and locked to the brand's single auto-determined
# value. Per user 2026-05-14, the contract is now: F and AI are
# CN-driven multi-option dropdowns; BRAND_NAME is just an optional
# cross-filter. Neither is mandatory.

@pytest.mark.asyncio
async def test_formulation_picked_against_cn_scope_must_be_valid():
    """SE picks F without BRAND_NAME. The cascade returns CN-scoped
    options. Value not in that set → CASCADE_VIOLATION."""
    elements = [
        el("COMMON_NAME", cosh_ref="cn:imida"),
        el("FORMULATION", cosh_ref="form:WRONG"),    # not in CN's set
        el("APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        el("DOSAGE", value="0.5"),
        el("DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
    ]
    core = {
        "common_names_of_inputs": [CascadeOption("cn:imida", "Imidacloprid")],
        "application_methods":    [CascadeOption("am:foliar_spray", "Foliar spray")],
        "units_data":             [CascadeOption("du:ml_per_l", "ml/L")],
    }
    cascade = {
        # F cascade is CN-scoped now — BRAND_NAME absent from the key.
        ("formulation_for_brand",
         frozenset({("COMMON_NAME", "cn:imida"), ("BRAND_NAME", None)})):
            [CascadeOption("form:SC", "SC"), CascadeOption("form:WP", "WP")],
    }
    with patches(core_returns=core, cascade_returns=cascade):
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES", elements=elements,
        )
    flagged = [e for e in r.errors if e.field_name == "FORMULATION"]
    assert flagged, f"FORMULATION not flagged; errors={r.errors}"
    assert flagged[0].code == "CASCADE_VIOLATION"


@pytest.mark.asyncio
async def test_formulation_optional_when_brand_set():
    """BRAND_NAME set, F omitted → no error. F is no longer
    auto-required-when-upstream-complete (Batch 27)."""
    elements = [
        el("COMMON_NAME", cosh_ref="cn:imida"),
        el("MANUFACTURER", cosh_ref="Bayer"),
        el("BRAND_NAME", cosh_ref="brand:confidor"),
        # F + AI omitted on purpose — pre-Batch-27 this fired
        # AUTO_SELECTED_MISSING; today it's allowed.
        el("APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        el("DOSAGE", value="0.5"),
        el("DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
    ]
    core = {
        "common_names_of_inputs": [CascadeOption("cn:imida", "Imidacloprid")],
        "application_methods":    [CascadeOption("am:foliar_spray", "Foliar spray")],
        "units_data":             [CascadeOption("du:ml_per_l", "ml/L")],
    }
    cascade = {
        ("manufacturers_for_common_name", frozenset({("COMMON_NAME", "cn:imida")})):
            [CascadeOption("Bayer", "Bayer")],
        ("brands_for_common_name_and_manufacturer",
         frozenset({("COMMON_NAME", "cn:imida"), ("MANUFACTURER", "Bayer")})):
            [CascadeOption("brand:confidor", "Confidor")],
    }
    with patches(core_returns=core, cascade_returns=cascade):
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES", elements=elements,
        )
    assert not any(e.code in ("AUTO_SELECTED_MISSING", "AUTO_SELECTED_UNEXPECTED")
                    for e in r.errors), r.errors


@pytest.mark.asyncio
async def test_formulation_optional_when_only_cn_set():
    """CN set, F+AI+MFR+BRAND all omitted → no error. The user's
    'CN + Formulation optional + dosage' use case (Batch 24) keeps
    working — and F is genuinely optional, not implicitly required."""
    elements = [
        el("COMMON_NAME", cosh_ref="cn:imida"),
        el("APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        el("DOSAGE", value="0.5"),
        el("DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
    ]
    core = {
        "common_names_of_inputs": [CascadeOption("cn:imida", "Imidacloprid")],
        "application_methods":    [CascadeOption("am:foliar_spray", "Foliar spray")],
        "units_data":             [CascadeOption("du:ml_per_l", "ml/L")],
    }
    with patches(core_returns=core):
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES", elements=elements,
        )
    assert r.is_valid, r.errors


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
        "common_names_of_inputs": [CascadeOption("cn:silwet", "Silwet")],
        "application_methods":    [CascadeOption("am:foliar_spray", "Foliar spray")],
        "units_data":             [CascadeOption("du:ml_per_l", "ml/L")],
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
        "common_names_of_inputs": [CascadeOption("cn:imida", "Imidacloprid")],
        "application_methods":    [CascadeOption("am:foliar_spray", "Foliar spray")],
        "units_data":             [CascadeOption("du:ml_per_l", "ml/L")],
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
        "units_data":             [CascadeOption("du:kg_per_acre", "kg/acre")],
        "formulations":           [CascadeOption("form:water_soluble", "Water-soluble")],
        "application_methods":    [CascadeOption("am:fertigation", "Fertigation")],
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
    # Batch 34: details key renamed from fertigation_interval →
    # interval_value (generic across FERTIGATION/IRRIGATION/REPEAT
    # interval fields).
    assert mismatches[0].details["interval_value"] == 7
    assert mismatches[0].details["interval_field"] == "FERTIGATION_INTERVAL"


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
        "units_data":             [CascadeOption("du:kg_per_acre", "kg/acre")],
        "formulations":           [CascadeOption("form:water_soluble", "Water-soluble")],
        "application_methods":    [CascadeOption("am:fertigation", "Fertigation")],
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
        "common_names_of_inputs": [CascadeOption("cn:imida", "Imidacloprid")],
        "application_methods":    [CascadeOption("am:foliar_spray", "Foliar spray")],
        "units_data":             [CascadeOption("du:ml_per_l", "ml/L")],
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
        "common_names_of_inputs": [CascadeOption("cn:trap", "Pheromone trap")],
        "units_data":              [CascadeOption("du:per_acre", "per acre")],
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
        "common_names_of_inputs": [CascadeOption("cn:imida", "Imidacloprid")],
        "application_methods":    [CascadeOption("am:foliar_spray", "Foliar spray")],
        "units_data":             [CascadeOption("du:ml_per_l", "ml/L")],
    }
    cascade = {
        ("manufacturers_for_common_name", frozenset({("COMMON_NAME", "cn:imida")})):
            [CascadeOption("Bayer", "Bayer")],
        # Per Batch 24: BRAND cascade key includes the MANUFACTURER
        # cross-filter when set.
        ("brands_for_common_name_and_manufacturer",
         frozenset({("COMMON_NAME", "cn:imida"), ("MANUFACTURER", "Bayer")})):
            [CascadeOption("brand:confidor", "Confidor")],
        # Per Batch 27: F + AI cascade keys are now CN-driven with
        # BRAND_NAME as an optional cross-filter.
        ("formulation_for_brand",
         frozenset({("COMMON_NAME", "cn:imida"), ("BRAND_NAME", "brand:confidor")})):
            [CascadeOption("form:SC", "SC")],
        ("ai_concentration_for_brand",
         frozenset({("COMMON_NAME", "cn:imida"), ("BRAND_NAME", "brand:confidor")})):
            [CascadeOption("17.8% SL", "17.8% SL")],
    }
    with patches(core_returns=core, cascade_returns=cascade):
        r = await validate_l2_elements(
            db=None, l2_type="CHEMICAL_PESTICIDES", elements=elements,
        )
    assert r.is_valid, r.errors
