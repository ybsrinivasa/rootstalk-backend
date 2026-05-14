"""
L2 Element Rule Book

Per-L2 element-shape spec for every Practice type defined in the Agriculture
Team Document v5 §2.3–§2.8. Drives both:

  • SE/CM-portal form rendering (which fields appear, which are mandatory,
    which are auto-selected via Cosh cascade).
  • Batch 4C-* L2 element validators (mandatory-element enforcement,
    cascade integrity, special-input / frequency-based / plant-wise extras
    invariants).

This file is pure data — no DB access. Validators consume L2_ELEMENT_RULES
+ the constants below. The cascade lookups named in `cosh_cascade:<name>`
sources are implemented in `app/services/cosh_cascade.py`.

Source-string vocabulary
------------------------
  cosh_core:<slug>          dropdown sourced directly from one Cosh Core
  cosh_cascade:<lookup>     dropdown filtered via a named cascade walk
  text_box                  single-line free text
  text_area                 multi-line free text
  number_2dec               numeric, 2 decimal places
  number_4dec               numeric, 4 decimal places (input dosages)
  auto_calculated           system-derived; not user-typed
  media_image               image upload (mime-checked)
  media_audio               audio upload (mime-checked)
  media_video               video upload (mime-checked)
  hyperlink                 URL string

Per-L2 invariants
-----------------
  is_special_input    ADJUVANTS only → Practice.is_special_input must be True
  frequency_based     CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS,
                       FERTIGATION_NPK_DOSAGES → Practice.frequency_days
                       must equal the FERTIGATION_INTERVAL element value
  plant_wise_extras   11 input L2s (see PLANT_WISE_EXTRAS_APPLY_TO) accept
                       optional VOLUME_PER_PLANT + VOLUME_PER_PLANT_UNIT

Numeric precision rule (locked 2026-05-07)
------------------------------------------
All numeric fields are decimal. number_4dec for fine-grained input dosages
called out as "Number (4 dec.)" in the spec. number_2dec for everything
else, including fields the spec lists as "Integer" — global "make all
decimals" decision (loses nothing if a field is whole-number in practice).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Mapping


# ── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FieldRule:
    name: str
    source: str
    mandatory: bool = False
    mandatory_if_set: tuple[str, ...] = ()
    cascade_from: tuple[str, ...] = ()
    auto_selected: bool = False


@dataclass(frozen=True)
class L2Spec:
    fields: tuple[FieldRule, ...]
    plant_wise_extras: bool = False
    is_special_input: bool = False
    frequency_based: bool = False


# ── Plant-wise extras (cross-cutting) ───────────────────────────────────────

PLANT_WISE_EXTRA_FIELDS: tuple[FieldRule, ...] = (
    FieldRule("VOLUME_PER_PLANT", source="number_4dec", mandatory=False),
    FieldRule(
        "VOLUME_PER_PLANT_UNIT",
        source="cosh_core:volume_unit",
        mandatory_if_set=("VOLUME_PER_PLANT",),
    ),
)

PLANT_WISE_EXTRAS_APPLY_TO: frozenset[str] = frozenset({
    "CHEMICAL_PESTICIDES",
    "MICROBIAL_PESTICIDES",
    "BOTANICAL_PESTICIDES",
    "INSECT_BIOCONTROL_AGENTS",
    "CHEMICAL_HERBICIDES",
    "OTHER_PESTICIDES",
    "MANURES",
    "CHEMICAL_FERTILIZER_PRODUCTS",
    "BIOFERTILIZERS",
    "PGR_TONICS",
    "SOIL_AMENDMENTS",
})


# ── Cosh slug & cascade registries ──────────────────────────────────────────
# Slugs used inside `cosh_core:<slug>` and `cosh_cascade:<name>` source
# strings. Real Cosh-side names get filled in at sync wiring; the validator
# resolves slugs to entity_types via this map.

COSH_CORE_SLUG_MAP: Mapping[str, str] = {
    "common_name":        "common_name",
    "application_method": "application_method",
    "dosage_unit":        "dosage_unit",
    "volume_unit":        "volume_unit",
    "formulation":        "formulation",
    "distance_unit":      "distance_unit",
    "temperature_unit":   "temperature_unit",
    "time_unit":          "time_unit",
    "irrigation_unit":    "irrigation_unit",
    "planting_material":  "planting_material",
    "number_unit":        "number_unit",
    "itk_name":           "itk_name",
    "maturity_index":     "maturity_index",
}

COSH_CASCADE_LOOKUPS: frozenset[str] = frozenset({
    "manufacturers_for_common_name",
    "brands_for_common_name_and_manufacturer",
    "formulation_for_brand",
    "ai_concentration_for_brand",
})


# ── Reusable field blocks ───────────────────────────────────────────────────

_INPUT_BRAND_TRIPLET: tuple[FieldRule, ...] = (
    FieldRule("COMMON_NAME", source="cosh_core:common_name", mandatory=True),
    # MANUFACTURER and BRAND_NAME are independent optional peers under
    # COMMON_NAME. Either can be set without the other (Batch 24
    # 2026-05-14): a seed-focused company may want to recommend a
    # CN + formulation without binding to a specific brand, while
    # another expert may remember the brand without the manufacturer.
    # The frontend bidirectionally filters each list by the other's
    # current value — see /cosh/options/{trade-names,manufacturers}
    # which both accept the cross-filter query param.
    FieldRule(
        "MANUFACTURER",
        source="cosh_cascade:manufacturers_for_common_name",
        cascade_from=("COMMON_NAME",),
    ),
    FieldRule(
        "BRAND_NAME",
        source="cosh_cascade:brands_for_common_name_and_manufacturer",
        cascade_from=("COMMON_NAME",),
    ),
)

_FORMULATION_AI_AUTOCASCADE: tuple[FieldRule, ...] = (
    FieldRule(
        "FORMULATION",
        source="cosh_cascade:formulation_for_brand",
        cascade_from=("BRAND_NAME",),
        auto_selected=True,
    ),
    FieldRule(
        "AI_CONCENTRATION",
        source="cosh_cascade:ai_concentration_for_brand",
        cascade_from=("BRAND_NAME",),
        auto_selected=True,
    ),
)

_FORMULATION_AI_TEXTBOX: tuple[FieldRule, ...] = (
    FieldRule("FORMULATION_AI_CONC", source="text_box", mandatory=False),
)

_FORMULATION_L2_FILTERED: tuple[FieldRule, ...] = (
    FieldRule("FORMULATION", source="cosh_core:formulation", mandatory=True),
)

_DOSAGE_TAIL_4DEC: tuple[FieldRule, ...] = (
    FieldRule("APPLICATION_METHOD", source="cosh_core:application_method", mandatory=True),
    FieldRule("DOSAGE", source="number_4dec", mandatory=True),
    FieldRule("DOSAGE_UNIT", source="cosh_core:dosage_unit", mandatory=True),
    FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
)

_DOSAGE_TAIL_2DEC: tuple[FieldRule, ...] = (
    FieldRule("APPLICATION_METHOD", source="cosh_core:application_method", mandatory=True),
    FieldRule("DOSAGE", source="number_2dec", mandatory=True),
    FieldRule("DOSAGE_UNIT", source="cosh_core:dosage_unit", mandatory=True),
    FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
)

_INSTRUCTIONS_OPTIONAL: tuple[FieldRule, ...] = (
    FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
)

_INSTRUCTIONS_ONLY_MANDATORY: tuple[FieldRule, ...] = (
    FieldRule("INSTRUCTIONS", source="text_area", mandatory=True),
)


# ── §2.3 Input Practices — Pesticides (7 L2s) ───────────────────────────────

_PESTICIDE_RULES: dict[str, L2Spec] = {
    "CHEMICAL_PESTICIDES": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_FORMULATION_AI_AUTOCASCADE,
            *_DOSAGE_TAIL_4DEC,
        ),
        plant_wise_extras=True,
    ),
    "MICROBIAL_PESTICIDES": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_FORMULATION_AI_TEXTBOX,
            *_DOSAGE_TAIL_4DEC,
        ),
        plant_wise_extras=True,
    ),
    "BOTANICAL_PESTICIDES": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_FORMULATION_AI_TEXTBOX,
            *_DOSAGE_TAIL_4DEC,
        ),
        plant_wise_extras=True,
    ),
    "INSECT_BIOCONTROL_AGENTS": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_DOSAGE_TAIL_2DEC,
        ),
        plant_wise_extras=True,
    ),
    "INSECT_TRAPS": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            FieldRule("DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("DOSAGE_UNIT", source="cosh_core:dosage_unit", mandatory=True),
            FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
        ),
    ),
    "CHEMICAL_HERBICIDES": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_FORMULATION_AI_AUTOCASCADE,
            *_DOSAGE_TAIL_4DEC,
        ),
        plant_wise_extras=True,
    ),
    "OTHER_PESTICIDES": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_FORMULATION_AI_TEXTBOX,
            *_DOSAGE_TAIL_4DEC,
        ),
        plant_wise_extras=True,
    ),
}


# ── §2.4 Input Practices — Special Inputs (1 L2) ────────────────────────────

_SPECIAL_INPUT_RULES: dict[str, L2Spec] = {
    "ADJUVANTS": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_DOSAGE_TAIL_4DEC,
        ),
        is_special_input=True,
    ),
}


# ── §2.5 Input Practices — Fertilizers (8 L2s) ──────────────────────────────
# In all Fertilizer L2s where Formulation is Cosh-linked, Formulation is
# filtered at the L2 level (cosh_core:formulation), NOT cascaded from Brand.

_FERTIGATION_FREQUENCY_TAIL: tuple[FieldRule, ...] = (
    FieldRule("FERTIGATION_INTERVAL", source="number_2dec", mandatory=True),
    FieldRule("NO_OF_APPLICATIONS", source="auto_calculated"),
)

_FERTILIZER_RULES: dict[str, L2Spec] = {
    "MANURES": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_FORMULATION_L2_FILTERED,
            *_DOSAGE_TAIL_4DEC,
        ),
        plant_wise_extras=True,
    ),
    "CHEMICAL_FERTILIZER_PRODUCTS": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_FORMULATION_L2_FILTERED,
            *_DOSAGE_TAIL_4DEC,
        ),
        plant_wise_extras=True,
    ),
    "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_FORMULATION_L2_FILTERED,
            FieldRule("APPLICATION_METHOD", source="cosh_core:application_method", mandatory=True),
            FieldRule("DOSAGE", source="number_4dec", mandatory=True),
            FieldRule("DOSAGE_UNIT", source="cosh_core:dosage_unit", mandatory=True),
            FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
            *_FERTIGATION_FREQUENCY_TAIL,
        ),
        frequency_based=True,
    ),
    "BIOFERTILIZERS": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_FORMULATION_AI_TEXTBOX,
            *_DOSAGE_TAIL_4DEC,
        ),
        plant_wise_extras=True,
    ),
    "PGR_TONICS": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_FORMULATION_AI_TEXTBOX,
            *_DOSAGE_TAIL_4DEC,
        ),
        plant_wise_extras=True,
    ),
    "SOIL_AMENDMENTS": L2Spec(
        fields=(
            *_INPUT_BRAND_TRIPLET,
            *_FORMULATION_AI_TEXTBOX,
            *_DOSAGE_TAIL_4DEC,
        ),
        plant_wise_extras=True,
    ),
    "CHEMICAL_FERTILIZERS_NPK_DOSAGES": L2Spec(
        fields=(
            FieldRule("N_DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("P_DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("K_DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("UNIT_AREA_WISE", source="cosh_core:dosage_unit", mandatory=True),
            FieldRule("FORMULATION", source="cosh_core:formulation", mandatory=True),
            FieldRule("APPLICATION_METHOD", source="cosh_core:application_method", mandatory=True),
            FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
        ),
    ),
    "FERTIGATION_NPK_DOSAGES": L2Spec(
        fields=(
            FieldRule("N_DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("P_DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("K_DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("UNIT_AREA_WISE", source="cosh_core:dosage_unit", mandatory=True),
            FieldRule("FORMULATION", source="cosh_core:formulation", mandatory=True),
            FieldRule("APPLICATION_METHOD", source="cosh_core:application_method", mandatory=True),
            *_FERTIGATION_FREQUENCY_TAIL,
            FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
        ),
        frequency_based=True,
    ),
}


# ── §2.6 Non-Input Practices — Spacing + Seed Treatment Physical (9 L2s) ───

_SPACING_FIELDS: tuple[FieldRule, ...] = (
    FieldRule("DISTANCE", source="number_2dec", mandatory=True),
    FieldRule("DISTANCE_UNIT", source="cosh_core:distance_unit", mandatory=True),
    FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
)

_TEMP_TIME_FIELDS: tuple[FieldRule, ...] = (
    FieldRule("TEMPERATURE", source="number_2dec", mandatory=True),
    FieldRule("TEMPERATURE_UNIT", source="cosh_core:temperature_unit", mandatory=True),
    FieldRule("TIME", source="number_2dec", mandatory=True),
    FieldRule("TIME_UNIT", source="cosh_core:time_unit", mandatory=True),
    FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
)

_TIME_ONLY_FIELDS: tuple[FieldRule, ...] = (
    FieldRule("TIME", source="number_2dec", mandatory=True),
    FieldRule("TIME_UNIT", source="cosh_core:time_unit", mandatory=True),
    FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
)

_SPACING_AND_SEED_TREATMENT_RULES: dict[str, L2Spec] = {
    "SPACING_PLANT_TO_PLANT": L2Spec(fields=_SPACING_FIELDS),
    "SPACING_ROW_TO_ROW":     L2Spec(fields=_SPACING_FIELDS),
    "SEED_TREATMENT_HOT_WATER":   L2Spec(fields=_TEMP_TIME_FIELDS),
    "SEED_TREATMENT_HOT_AIR":     L2Spec(fields=_TEMP_TIME_FIELDS),
    "SEED_TREATMENT_COLD":        L2Spec(fields=_TEMP_TIME_FIELDS),
    "SEED_TREATMENT_BOILING":     L2Spec(fields=_TEMP_TIME_FIELDS),
    "SEED_TREATMENT_SOAKING":     L2Spec(fields=_TIME_ONLY_FIELDS),
    "SEED_TREATMENT_SUN_DRYING":  L2Spec(fields=_TIME_ONLY_FIELDS),
    "SEED_TREATMENT_SHADE_DRYING":L2Spec(fields=_TIME_ONLY_FIELDS),
}


# ── §2.6 Non-Input Practices — Sowing/Planting Methods (10 L2s) ─────────────

_DEPTH_FIELDS: tuple[FieldRule, ...] = (
    FieldRule("DEPTH_OF_SOWING", source="number_2dec", mandatory=True),
    FieldRule("DEPTH_UNIT", source="cosh_core:distance_unit", mandatory=True),
)

_SOWING_METHOD_RULES: dict[str, L2Spec] = {
    "SOWING_LINE": L2Spec(fields=(*_DEPTH_FIELDS, *_INSTRUCTIONS_OPTIONAL)),
    "SOWING_BROADCASTING": L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "SOWING_DIBBLING": L2Spec(fields=(
        *_DEPTH_FIELDS,
        FieldRule("NO_OF_SEEDS_PER_HOLE", source="number_2dec", mandatory=True),
        *_INSTRUCTIONS_OPTIONAL,
    )),
    "SOWING_DRILLING": L2Spec(fields=(*_DEPTH_FIELDS, *_INSTRUCTIONS_OPTIONAL)),
    "SOWING_TRANSPLANTING": L2Spec(fields=(
        FieldRule("NO_OF_SEEDLINGS_PER_HILL", source="number_2dec", mandatory=True),
        *_INSTRUCTIONS_OPTIONAL,
    )),
    "SOWING_HILL_DROPPING": L2Spec(fields=(*_DEPTH_FIELDS, *_INSTRUCTIONS_OPTIONAL)),
    "SOWING_CHECK_ROW":     L2Spec(fields=(*_DEPTH_FIELDS, *_INSTRUCTIONS_OPTIONAL)),
    "SOWING_PRO_TRAY":      L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "SOWING_RAISED_BEDS": L2Spec(fields=(
        FieldRule("BED_WIDTH", source="number_2dec", mandatory=True),
        FieldRule("BED_HEIGHT", source="number_2dec", mandatory=True),
        FieldRule("SIZE_UNIT", source="cosh_core:distance_unit", mandatory=True),
        FieldRule("SPACING_BETWEEN_BEDS", source="number_2dec", mandatory=True),
        FieldRule("SPACING_BETWEEN_BEDS_UNIT", source="cosh_core:distance_unit", mandatory=True),
        FieldRule("DEPTH_OF_SOWING", source="number_2dec", mandatory=True),
        FieldRule("DEPTH_UNIT", source="cosh_core:distance_unit", mandatory=True),
        FieldRule("NO_OF_BEDS", source="number_2dec", mandatory=False),
        FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
    )),
    "SOWING_PIT_METHOD": L2Spec(fields=(
        FieldRule("PIT_LENGTH", source="number_2dec", mandatory=True),
        FieldRule("PIT_WIDTH", source="number_2dec", mandatory=True),
        FieldRule("PIT_DEPTH", source="number_2dec", mandatory=True),
        FieldRule("SIZE_UNIT", source="cosh_core:distance_unit", mandatory=True),
        FieldRule("NO_OF_PITS", source="number_2dec", mandatory=False),
        FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
    )),
}


# ── §2.6 Non-Input Practices — Other Non-Inputs (41 L2s) ───────────────────

_PLANTING_SYSTEM_FIELDS: tuple[FieldRule, ...] = (
    FieldRule("NUMBER_OF_PLANTS", source="number_2dec", mandatory=True),
    FieldRule("NUMBER_UNIT", source="cosh_core:number_unit", mandatory=True),
    FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
)

_WATER_DURATION_FIELDS: tuple[FieldRule, ...] = (
    FieldRule("IRRIGATION_DURATION", source="number_2dec", mandatory=True),
    FieldRule("IRRIGATION_UNIT", source="cosh_core:irrigation_unit", mandatory=True),
    FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
)

_OTHER_NON_INPUT_RULES: dict[str, L2Spec] = {
    "PLANTING_MATERIAL_QUANTITY": L2Spec(fields=(
        FieldRule("PLANTING_MATERIAL", source="cosh_core:planting_material", mandatory=True),
        FieldRule("QUANTITY", source="number_2dec", mandatory=True),
        FieldRule("NUMBER_UNIT", source="cosh_core:number_unit", mandatory=True),
        FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
    )),

    "PLANTING_SQUARE":      L2Spec(fields=_PLANTING_SYSTEM_FIELDS),
    "PLANTING_RECTANGULAR": L2Spec(fields=_PLANTING_SYSTEM_FIELDS),
    "PLANTING_TRIANGULAR":  L2Spec(fields=_PLANTING_SYSTEM_FIELDS),
    "PLANTING_HEXAGONAL":   L2Spec(fields=_PLANTING_SYSTEM_FIELDS),
    "PLANTING_QUINCUNX":    L2Spec(fields=_PLANTING_SYSTEM_FIELDS),

    "CONTOUR_SYSTEM": L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),

    "ITKS": L2Spec(fields=(
        FieldRule("ITK_NAME", source="cosh_core:itk_name", mandatory=True),
        FieldRule("APPLICATION_METHOD", source="cosh_core:application_method", mandatory=True),
        FieldRule("DOSAGE", source="number_2dec", mandatory=True),
        FieldRule("DOSAGE_UNIT", source="cosh_core:dosage_unit", mandatory=True),
        FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
    )),

    "WATER_DRIP":      L2Spec(fields=_WATER_DURATION_FIELDS),
    "WATER_SPRINKLER": L2Spec(fields=_WATER_DURATION_FIELDS),
    "WATER_FURROW":    L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "WATER_FLOOD":     L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "WATER_BASIN":     L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),

    "WEED_MANUAL": L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),

    "CULTURAL_THINNING":              L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_GAP_FILLING":           L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_EARTHING_UP":           L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_TOP_DRESSING":          L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_STAKING":               L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_ARTIFICIAL_POLLINATION":L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_TRAINING":              L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_PRUNING":               L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_SHADE_MANAGEMENT":      L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_WIND_BREAKS":           L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_DEFLOWERING":           L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_NIPPING_PINCHING":      L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_DESHOOTING":            L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "CULTURAL_DESUCKERING":           L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),

    "HARVESTING_MANUAL": L2Spec(fields=(
        FieldRule("MATURITY_INDEX", source="cosh_core:maturity_index", mandatory=True),
        FieldRule("INSTRUCTIONS", source="text_area", mandatory=True),
    )),

    "POST_HARVEST_DEHULLING":   L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "POST_HARVEST_DEHUSKING":   L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "POST_HARVEST_SHELLING":    L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "POST_HARVEST_DRYING":      L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "POST_HARVEST_CURING":      L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "POST_HARVEST_WINNOWING":   L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "POST_HARVEST_CLEANING":    L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "POST_HARVEST_TRIMMING":    L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "POST_HARVEST_SORTING":     L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "POST_HARVEST_GRADING":     L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "POST_HARVEST_PACKING":     L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
    "POST_HARVEST_COLD_STORAGE":L2Spec(fields=_INSTRUCTIONS_ONLY_MANDATORY),
}


# ── §2.7 General Instructions (1 L2) ────────────────────────────────────────

_GENERAL_INSTRUCTIONS_RULES: dict[str, L2Spec] = {
    "GENERAL_INSTRUCTIONS": L2Spec(fields=(
        FieldRule("TITLE", source="text_area", mandatory=True),
        FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
    )),
}


# ── §2.8 Media (4 L2s) ──────────────────────────────────────────────────────

_MEDIA_TITLE_DESCRIPTION: tuple[FieldRule, ...] = (
    FieldRule("TITLE", source="text_box", mandatory=True),
    FieldRule("DESCRIPTION", source="text_area", mandatory=False),
)

_MEDIA_RULES: dict[str, L2Spec] = {
    "MEDIA_IMAGE": L2Spec(fields=(
        *_MEDIA_TITLE_DESCRIPTION,
        FieldRule("UPLOAD_IMAGE", source="media_image", mandatory=True),
    )),
    "MEDIA_AUDIO": L2Spec(fields=(
        *_MEDIA_TITLE_DESCRIPTION,
        FieldRule("UPLOAD_AUDIO", source="media_audio", mandatory=True),
    )),
    "MEDIA_VIDEO": L2Spec(fields=(
        *_MEDIA_TITLE_DESCRIPTION,
        FieldRule("UPLOAD_VIDEO", source="media_video", mandatory=True),
    )),
    "MEDIA_HYPERLINK": L2Spec(fields=(
        *_MEDIA_TITLE_DESCRIPTION,
        FieldRule("HYPERLINK", source="hyperlink", mandatory=True),
    )),
}


# ── Master rule book ────────────────────────────────────────────────────────

L2_ELEMENT_RULES: Mapping[str, L2Spec] = {
    **_PESTICIDE_RULES,
    **_SPECIAL_INPUT_RULES,
    **_FERTILIZER_RULES,
    **_SPACING_AND_SEED_TREATMENT_RULES,
    **_SOWING_METHOD_RULES,
    **_OTHER_NON_INPUT_RULES,
    **_GENERAL_INSTRUCTIONS_RULES,
    **_MEDIA_RULES,
}


# ── Lookups for validators ──────────────────────────────────────────────────

def get_l2_spec(l2_type: str) -> L2Spec | None:
    return L2_ELEMENT_RULES.get(l2_type)


def applies_plant_wise_extras(l2_type: str) -> bool:
    return l2_type in PLANT_WISE_EXTRAS_APPLY_TO


def all_l2_types() -> tuple[str, ...]:
    return tuple(L2_ELEMENT_RULES.keys())
