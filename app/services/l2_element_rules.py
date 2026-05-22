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
    # Optional cross-filter inputs the cascade can narrow on when set,
    # but which don't gate UI enable-state the way cascade_from does.
    # Example: BRAND_NAME's UI enables on COMMON_NAME alone (cascade_from),
    # but if MANUFACTURER is also set, the cascade narrows further
    # (cascade_optional_inputs). Validator merges both into the lookup
    # inputs; frontend ignores `cascade_optional_inputs` for the
    # disabled-until-parent-set check.
    cascade_optional_inputs: tuple[str, ...] = ()
    # Batch 34: marker for the "interval days" field on a
    # frequency_based L2 (Fertigation, Irrigation, Instructions). The
    # validator + handler use this to find the field instead of
    # hardcoding the element name — each frequency-based L2 family
    # has its own name (FERTIGATION_INTERVAL / IRRIGATION_INTERVAL /
    # REPEAT_INTERVAL) so the SE sees a label appropriate to the
    # practice family.
    is_interval: bool = False


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

# Per Batch 29 (2026-05-14): map rule-book slugs to the **real** Cosh
# `core_type` slugs as shipped by the production sync (see
# cosh_constants.py). The validator's list_core_options query is
# `select(CoshCoreItem).where(core_type=<target>)`, so the right-hand
# side must match what Cosh actually ships.
#
# All unit-flavoured slugs (dosage_unit, volume_unit, …) point at the
# unified `units_data` Core; finer L2/unit_type filtering happens in
# the UX layer via /cosh/options/units, but for validation purposes
# "is this a real units_data cosh_id?" is the right check.
#
# Non-input cores `planting_material`, `itk_data`, `maturity_index`
# shipped by Cosh 2026-05-16. The rule-book slug `itk_name` (used by
# ITKS L2's element) maps to the real Cosh `core_type` `itk_data`; the
# other two land under their literal slugs.
COSH_CORE_SLUG_MAP: Mapping[str, str] = {
    "common_name":        "common_names_of_inputs",
    "application_method": "application_methods",
    "formulation":        "formulations",
    "dosage_unit":        "units_data",
    "volume_unit":        "units_data",
    "distance_unit":      "units_data",
    "temperature_unit":   "units_data",
    "time_unit":          "units_data",
    "irrigation_unit":    "units_data",
    "number_unit":        "units_data",
    "planting_material":  "planting_material",
    "itk_name":           "itk_data",
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
        cascade_optional_inputs=("MANUFACTURER",),
    ),
)

# Per user 2026-05-14: FORMULATION and AI_CONCENTRATION are selectable
# as soon as COMMON_NAME is picked (no longer gated on BRAND_NAME being
# set first). The cascade is CN-driven: pick CN → see all formulations
# / a.i. values that span the CN's trade names. Pick BRAND_NAME → list
# narrows to that brand's specific formulation + a.i. Neither field is
# auto-determined any more — SE picks freely.
#
# Variable name kept (it's module-internal); the AUTOCASCADE suffix is
# historical from when the fields were auto_selected.
_FORMULATION_AI_AUTOCASCADE: tuple[FieldRule, ...] = (
    FieldRule(
        "FORMULATION",
        source="cosh_cascade:formulation_for_brand",
        cascade_from=("COMMON_NAME",),
        cascade_optional_inputs=("BRAND_NAME",),
    ),
    FieldRule(
        "AI_CONCENTRATION",
        source="cosh_cascade:ai_concentration_for_brand",
        cascade_from=("COMMON_NAME",),
        cascade_optional_inputs=("BRAND_NAME",),
    ),
)

# 2026-05-22 — variant of _FORMULATION_AI_AUTOCASCADE with both
# fields mandatory. Used by CHEMICAL_PESTICIDES per user, where
# the SE must commit to a specific formulation + a.i. concentration
# (the safety / dosage math depends on it). CHEMICAL_HERBICIDES
# keeps the optional variant — user can extend the mandatory
# treatment there if they want the same behaviour.
_FORMULATION_AI_AUTOCASCADE_MANDATORY: tuple[FieldRule, ...] = (
    FieldRule(
        "FORMULATION",
        source="cosh_cascade:formulation_for_brand",
        mandatory=True,
        cascade_from=("COMMON_NAME",),
        cascade_optional_inputs=("BRAND_NAME",),
    ),
    FieldRule(
        "AI_CONCENTRATION",
        source="cosh_cascade:ai_concentration_for_brand",
        mandatory=True,
        cascade_from=("COMMON_NAME",),
        cascade_optional_inputs=("BRAND_NAME",),
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
            *_FORMULATION_AI_AUTOCASCADE_MANDATORY,
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

# 2026-05-22 — interval fields are non-mandatory. Per user: when the
# SE wants a single application during the timeline (not a recurring
# cadence), they leave the interval blank and the farmer sees a
# "apply once at any time during this period" instruction. The
# FREQUENCY_MISMATCH check in the validator only fires when interval
# IS provided (intentional). `_assert_interval_fits_timeline` in the
# router also short-circuits on blank.
_FERTIGATION_FREQUENCY_TAIL: tuple[FieldRule, ...] = (
    FieldRule("FERTIGATION_INTERVAL", source="number_2dec", mandatory=False, is_interval=True),
    FieldRule("NUMBER_OF_APPLICATIONS", source="auto_calculated"),
)


# Batch 34: frequency tails for the other practice families that get
# a repeated-application cadence. The element name differs per family
# only so the rule-book → label rendering surfaces a contextual label
# ("Fertigation Interval" / "Irrigation Interval" / "Repeat Interval").
# All three share the same semantics: is_interval=True drives both the
# validator's FREQUENCY_MISMATCH check and the handler's "interval
# must produce ≥ 2 applications across the timeline" gate.
_IRRIGATION_FREQUENCY_TAIL: tuple[FieldRule, ...] = (
    FieldRule("IRRIGATION_INTERVAL", source="number_2dec", mandatory=False, is_interval=True),
    FieldRule("NUMBER_OF_APPLICATIONS", source="auto_calculated"),
)
_INSTRUCTION_FREQUENCY_TAIL: tuple[FieldRule, ...] = (
    FieldRule("REPEAT_INTERVAL", source="number_2dec", mandatory=False, is_interval=True),
    FieldRule("NUMBER_OF_APPLICATIONS", source="auto_calculated"),
)

_FERTILIZER_RULES: dict[str, L2Spec] = {
    "MANURES": L2Spec(
        # FORMULATION dropped 2026-05-19 per user — manures don't
        # carry a discrete formulation in the same way fertilizer
        # products do. The other Fertilizer L2s keep FORMULATION
        # because it's meaningful there.
        fields=(
            *_INPUT_BRAND_TRIPLET,
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
        # Per user 2026-05-14: don't split Unit into Area-wise vs
        # Plant-wise here. Show one "Unit" dropdown carrying all three
        # dosage-unit variants from the Cosh `dosage_unit` slug.
        fields=(
            FieldRule("N_DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("P_DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("K_DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("UNIT", source="cosh_core:dosage_unit", mandatory=True),
            FieldRule("FORMULATION", source="cosh_core:formulation", mandatory=True),
            FieldRule("APPLICATION_METHOD", source="cosh_core:application_method", mandatory=True),
            FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
        ),
    ),
    "FERTIGATION_NPK_DOSAGES": L2Spec(
        # Same Unit unification as CHEMICAL_FERTILIZERS_NPK_DOSAGES
        # (Batch 26, 2026-05-14).
        fields=(
            FieldRule("N_DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("P_DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("K_DOSAGE", source="number_2dec", mandatory=True),
            FieldRule("UNIT", source="cosh_core:dosage_unit", mandatory=True),
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

    # Batch 34: Irrigation practices repeat on an interval — every
    # day, every 2 days, etc. SE sets IRRIGATION_INTERVAL; the modal
    # surfaces NUMBER_OF_APPLICATIONS as read-only.
    "WATER_DRIP":      L2Spec(
        fields=(*_WATER_DURATION_FIELDS, *_IRRIGATION_FREQUENCY_TAIL),
        frequency_based=True,
    ),
    "WATER_SPRINKLER": L2Spec(
        fields=(*_WATER_DURATION_FIELDS, *_IRRIGATION_FREQUENCY_TAIL),
        frequency_based=True,
    ),
    "WATER_FURROW":    L2Spec(
        fields=(*_INSTRUCTIONS_ONLY_MANDATORY, *_IRRIGATION_FREQUENCY_TAIL),
        frequency_based=True,
    ),
    "WATER_FLOOD":     L2Spec(
        fields=(*_INSTRUCTIONS_ONLY_MANDATORY, *_IRRIGATION_FREQUENCY_TAIL),
        frequency_based=True,
    ),
    "WATER_BASIN":     L2Spec(
        fields=(*_INSTRUCTIONS_ONLY_MANDATORY, *_IRRIGATION_FREQUENCY_TAIL),
        frequency_based=True,
    ),

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
    # Batch 34: General Instructions can repeat at a fixed cadence
    # (REPEAT_INTERVAL) the same way Fertigation and Irrigation do.
    # SE picks "every day" / "every 2 days" / …; modal shows the
    # computed NUMBER_OF_APPLICATIONS over the timeline window.
    "GENERAL_INSTRUCTIONS": L2Spec(
        fields=(
            FieldRule("TITLE", source="text_area", mandatory=True),
            FieldRule("INSTRUCTIONS", source="text_area", mandatory=False),
            *_INSTRUCTION_FREQUENCY_TAIL,
        ),
        frequency_based=True,
    ),
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
    # MEDIA_VIDEO removed Batch 36 — direct video upload deferred;
    # SEs use MEDIA_HYPERLINK to point at YouTube/Drive instead.
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
