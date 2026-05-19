"""Locked Cosh-side identifiers — captured from the first production
sync, 2026-05-09 (sync_id 93e3e668-2266-4839-986f-b09898db3fc0).

Cosh consolidates all biological identifiers (crops, pests, biocontrol
agents) into a single `biological_names` Core. Each item's semantic
role is expressed by a separate Connect (`biological_names_and_roles`)
linking it to one of the items in the `roles_of_biological_names` Core.

Filter on UUIDs, never on translated strings. The string "Crop" can be
edited by Cosh curators; the UUID is the stable identifier.
"""
from __future__ import annotations

# Core entity_type slugs (the `core_type` column on cosh_core_items)
COSH_BIOLOGICAL_NAMES_CORE = "biological_names"
COSH_ROLES_CORE = "roles_of_biological_names"

# Connect entity_type slug (the `connect_type` column on cosh_connect_rows)
COSH_NAME_ROLE_CONNECT = "biological_names_and_roles"

# Stable UUIDs of the three role items. New roles can be added in Cosh
# without touching this file — they're just unused unless we add a
# constant referring to them.
COSH_ROLE_CROP_UUID = "9735f960-2d5a-498d-8fdd-429be0cfb950"
COSH_ROLE_PEST_UUID = "4138f142-d2bd-43c2-8252-d658799bcc99"
COSH_ROLE_BIO_CONTROL_AGENT_UUID = "7f2f5283-e427-484a-a5b8-40181d633fa2"

# Role names used inside Connect endpoints (the `role` key of each
# endpoint dict). Cosh emits `entity_type` of the underlying Core as
# the role string, so these match the Core slugs above.
ENDPOINT_ROLE_BIOLOGICAL_NAME = COSH_BIOLOGICAL_NAMES_CORE
ENDPOINT_ROLE_OF_NAME = COSH_ROLES_CORE

# ── Area/Plant-wise classification (Round 3, 2026-05-09) ───────────────────

# Core hosting the two area-plant-wise items (Area-wise / Plant-wise).
COSH_AREA_PLANT_WISE_CORE = "area_plant_wise"

# Connect linking biological_names (classified as Crop) to one of the
# area_plant_wise items.
COSH_CROP_AREA_PLANT_CONNECT = "crop_area_plant_wise"

# Stable UUIDs of the two typing items.
COSH_AREA_WISE_UUID = "89e4d7d5-ac70-460f-9aa4-9c00ca5808a2"
COSH_PLANT_WISE_UUID = "1bf6f539-89bb-4d45-ae88-68606761deae"

# RootsTalk-side measure tokens that downstream code (BL-06 volume calc,
# plant-wise additional elements, etc.) compares against. Maps
# Cosh-side UUIDs to the stable string tokens RootsTalk uses.
COSH_UUID_TO_MEASURE = {
    COSH_AREA_WISE_UUID: "AREA_WISE",
    COSH_PLANT_WISE_UUID: "PLANT_WISE",
}

# ── Package Parameters + Variables (synced 2026-05-12) ────────────────────
#
# Cosh ships a three-endpoint Connect that ties each crop to a
# (parameter, variable) pair. The PoP signature picker on the
# SA / CA portals reads through this Connect to surface, per crop,
# the set of parameters and their applicable variables.

COSH_PACKAGE_PARAMETERS_CORE = "package_parameters"
COSH_PACKAGE_VARIABLES_CORE = "package_variables"
COSH_CROPS_PARAMS_VARS_CONNECT = "crops_parameters_variables"

# ── Input options + cascades (synced 2026-05-14) ──────────────────────────
#
# Seven Connects + their backing Cores drive the per-L2 element
# dropdowns + the brand cascade on the Add Practice modal:
#
#   commonnames_l2          (common_names_of_inputs ↔ l2_data)
#   application_methods_l2  (application_methods ↔ l2_data)
#   l2_units_unittypes      (l2_data ↔ units_data ↔ unit_types) 3-endpoint
#   tradename_commonname    (trade_names ↔ common_names_of_inputs)
#   tradename_manufacturer  (trade_names ↔ input_manufacturers)
#   tradename_formulation   (trade_names ↔ formulations)
#   tradename_ai            (trade_names ↔ a_i)

# Cores
COSH_L2_DATA_CORE = "l2_data"
COSH_COMMON_NAMES_CORE = "common_names_of_inputs"
COSH_APPLICATION_METHODS_CORE = "application_methods"
COSH_UNITS_DATA_CORE = "units_data"
COSH_UNIT_TYPES_CORE = "unit_types"
COSH_TRADE_NAMES_CORE = "trade_names"
COSH_INPUT_MANUFACTURERS_CORE = "input_manufacturers"
COSH_FORMULATIONS_CORE = "formulations"
COSH_AI_CORE = "a_i"

# Connects
COSH_COMMONNAMES_L2_CONNECT = "commonnames_l2"
COSH_APPLICATION_METHODS_L2_CONNECT = "application_methods_l2"
COSH_L2_UNITS_UNITTYPES_CONNECT = "l2_units_unittypes"
COSH_TRADENAME_COMMONNAME_CONNECT = "tradename_commonname"
COSH_TRADENAME_MANUFACTURER_CONNECT = "tradename_manufacturer"
COSH_TRADENAME_FORMULATION_CONNECT = "tradename_formulation"
COSH_TRADENAME_AI_CONNECT = "tradename_ai"

# Non-input Cores (synced 2026-05-16). Flat Cosh Cores driving the three
# Non-input element dropdowns: PLANTING_MATERIAL_QUANTITY → planting_material,
# ITKS → itk_data, HARVESTING_MANUAL → maturity_index. Maturity indices are
# additionally crop-filtered via the `maturity_index_crops` Connect.
COSH_PLANTING_MATERIAL_CORE = "planting_material"
COSH_ITK_DATA_CORE = "itk_data"
COSH_MATURITY_INDEX_CORE = "maturity_index"
COSH_MATURITY_INDEX_CROPS_CONNECT = "maturity_index_crops"

# ── SP × PG × Crop applicability (Cosh shipped 2026-05-14) ───────────────
#
# Cosh's `sp_pg_crops` Connect ties each Specific Problem (SP) to one
# Problem Group (PG) and one crop. 3-endpoint shape per row:
#
#   pos 1: biological_names   ← the **SP** (the specific organism /
#                                 pest / pathogen). Same Core as crop,
#                                 disambiguated by position.
#   pos 2: problem_groups     ← the PG (e.g. Fungal Diseases).
#   pos 3: biological_names   ← the **crop**.
#
# 1,633 rows in the first prod sync. Bridge between Cosh's pest-side
# taxonomy and RootsTalk's PG / SP authoring surfaces: a SP is only
# applicable to a given crop if a row exists in this Connect. Drives:
#
#   • "applicable crops" chips on each PG card (SA portal CHA list).
#   • crop-filter on SE's Add Specific-Problem picker.
#   • PG-filter on SE's Add Crop picker (reverse direction).

COSH_PROBLEM_GROUPS_CORE = "problem_groups"
COSH_SP_PG_CROPS_CONNECT = "sp_pg_crops"

SPPC_POS_SP = 1
SPPC_POS_PG = 2
SPPC_POS_CROP = 3


# ── DUS Characters (synced 2026-05-19) ────────────────────────────────────
#
# Cosh's `dus_characters_descriptors` Connect wires a crop to its DUS
# (Distinctness, Uniformity, Stability) characterisation taxonomy.
# Each row encodes one valid (crop, part, sub-part, character,
# descriptor-value) combination.
#
#   pos 1: biological_names   ← the crop
#   pos 2: plant_parts        ← LEAF / SHOOT / SPIKE / FRUIT / …
#   pos 3: plant_subparts     ← LAMINA / SPINES / CLOVES / …
#   pos 4: dus_characters     ← "Lobing" / "Vigor" / "Shape in cross
#                                section" — the trait being scored.
#   pos 5: dus_descriptors    ← "Weak" / "Thick" / "Green" — the
#                                discrete value the trait can take.
#
# 1,562 active rows on first prod sync. Drives the cascading
# Part → Sub-Part → Character → Descriptor pickers on the SE's
# /seed/varieties DUS section.

COSH_PLANT_PARTS_CORE = "plant_parts"
COSH_PLANT_SUBPARTS_CORE = "plant_subparts"
COSH_DUS_CHARACTERS_CORE = "dus_characters"
COSH_DUS_DESCRIPTORS_CORE = "dus_descriptors"
COSH_DUS_CHARACTERS_DESCRIPTORS_CONNECT = "dus_characters_descriptors"

DCD_POS_CROP = 1
DCD_POS_PART = 2
DCD_POS_SUBPART = 3
DCD_POS_CHARACTER = 4
DCD_POS_DESCRIPTOR = 5


# ── BLANK BOX sentinel UUIDs (canonical) ──────────────────────────────────
#
# Cosh's `BLANK BOX` Core item is a sentinel meaning "no relevant
# data for this field." User-confirmed 2026-05-14 and re-stated
# 2026-05-19. Treat as null / wildcard at filter time; never
# surface to users as a pickable option. See
# `feedback_cosh_blank_box_sentinel.md` for the rule.
#
# This is the canonical dict — services walking Cosh Connects
# should normalise matching endpoints to None before building any
# picker tree or filter index. Mirror of the older
# `PD_BLANK_BOX_BY_CORE` in pest_diagnosis_view (which predates
# this file; the dict there is the authoritative seed and is kept
# in sync manually).
COSH_BLANK_BOX_BY_CORE: dict[str, str] = {
    "plant_parts":        "15d12e37-9309-4899-ad7b-801bc8bb0a65",
    "plant_subparts":     "9cba3894-6022-4f9d-9aa7-b8685f567b74",
    "damage_subsymptoms": "818bb871-ff52-4fb0-a95d-3d80ff40bb43",
    "crop_stages":        "c15d78e4-7f46-421b-9a9f-10ce080c45e3",
}


def is_blank_box(core_type: str, cosh_id: str | None) -> bool:
    """True iff `cosh_id` is the BLANK BOX sentinel for `core_type`.
    Returns False for unknown Cores, missing sentinels, or None ids."""
    if not cosh_id:
        return False
    return COSH_BLANK_BOX_BY_CORE.get(core_type) == cosh_id


# ── Pest Diagnosis (synced 2026-05-14) ────────────────────────────────────
#
# Cosh ships one Connect — `pest_diagnosis` — that's 9 endpoints wide
# (the widest we've seen). Each row encodes:
#
#   pos 1: damage_symptoms             (top-level visible symptom)
#   pos 2: damage_subsymptoms          (finer-grain symptom)
#   pos 3: biological_names            ← the **pest**
#   pos 4: pest_stages                 (pest's life stage)
#   pos 5: plant_parts                 (affected plant part)
#   pos 6: plant_subparts              (finer-grain plant part)
#   pos 7: biological_names            ← the **crop**
#   pos 8: crop_stages                 (crop's growth stage)
#   pos 9: priority_rank_pests         (severity ranking, "0"..."5")
#
# The same `biological_names` Core appears at positions 3 and 7;
# position number disambiguates the semantic role (pest vs crop).
# Cosh's "one Core for all organisms" decision pays off here — the
# structure stays clean without per-role Cores.
#
# First sync 2026-05-14: 6,266 active rows. Drives the farmer-facing
# Diagnosis pipe end-to-end (after symptom-report UX).

COSH_PEST_DIAGNOSIS_CONNECT = "pest_diagnosis"

# Supporting Cores referenced by the Connect.
COSH_DAMAGE_SYMPTOMS_CORE = "damage_symptoms"
COSH_DAMAGE_SUBSYMPTOMS_CORE = "damage_subsymptoms"
COSH_PEST_STAGES_CORE = "pest_stages"
COSH_PLANT_PARTS_CORE = "plant_parts"
COSH_PLANT_SUBPARTS_CORE = "plant_subparts"
COSH_CROP_STAGES_CORE = "crop_stages"
COSH_PRIORITY_RANK_PESTS_CORE = "priority_rank_pests"

# Endpoint position constants (1-indexed, mirroring Cosh's wire shape).
PD_POS_SYMPTOM = 1
PD_POS_SUBSYMPTOM = 2
PD_POS_PEST = 3
PD_POS_PEST_STAGE = 4
PD_POS_PLANT_PART = 5
PD_POS_PLANT_SUBPART = 6
PD_POS_CROP = 7
PD_POS_CROP_STAGE = 8
PD_POS_PRIORITY_RANK = 9

# BLANK BOX sentinel UUIDs (pinned 2026-05-14 from the first prod sync).
#
# Semantics confirmed by user: BLANK BOX = "no data exists for this
# dimension" — i.e., the row's constraint on that dimension is
# vacuously satisfied. Behaves like a wildcard when filtering: when
# the farmer specifies a value, rows with that endpoint = BLANK BOX
# still match. NOT shown as a farmer-pickable option.
#
# Only the four dimensions below carry a BLANK BOX item in Cosh today.
# The other three (damage_symptoms, pest_stages, priority_rank_pests)
# match strictly. If Cosh later adds BLANK BOX to those Cores, extend
# this mapping; the filter helper reads from it.

PD_BLANK_BOX_BY_CORE: dict[str, str] = {
    COSH_DAMAGE_SUBSYMPTOMS_CORE: "818bb871-ff52-4fb0-a95d-3d80ff40bb43",
    COSH_PLANT_PARTS_CORE:        "15d12e37-9309-4899-ad7b-801bc8bb0a65",
    COSH_PLANT_SUBPARTS_CORE:     "9cba3894-6022-4f9d-9aa7-b8685f567b74",
    COSH_CROP_STAGES_CORE:        "c15d78e4-7f46-421b-9a9f-10ce080c45e3",
}

# ── Image Index + per-crop symptom images (synced 2026-05-14) ─────────────
#
# Cosh introduced a Connect-references-Connect pattern here. Two-step
# lookup chain for any crop:
#
#   1. image_index Connect (2-endpoint, one row per crop with images):
#        pos 1 — biological_names (the crop)
#        pos 2 — symptom_image_slug Core item
#      The pos-2 Core item's `translations.en` carries the *string*
#      name of the per-crop image Connect (e.g. "tomato_symptom_images").
#
#   2. {crop}_symptom_images Connect (2-endpoint, one row per image
#      attached to one pest_diagnosis row):
#        pos 1 — pest_diagnosis (references a row of the
#                pest_diagnosis Connect)
#        pos 2 — images_{crop}_symptoms_core (image Core item).
#                The image Core item's `metadata_.s3_path` carries the
#                full HTTPS URL on Cosh's public-read S3 bucket.
#
# The image Core's slug is *not* stored in the index — it falls out
# from the image Connect's own endpoint role on first read.

COSH_IMAGE_INDEX_CONNECT = "image_index"
COSH_SYMPTOM_IMAGE_SLUG_CORE = "symptom_image_slug"

II_POS_CROP = 1
II_POS_SLUG = 2

SI_POS_DIAGNOSIS_ROW = 1
SI_POS_IMAGE_CORE = 2


# Python rule-book L2 name → Cosh `l2_data` cosh_id. Built from
# the first Cosh sync of `l2_data` (2026-05-14). L2 names that exist
# in the rule book but Cosh hasn't shipped (e.g. GENERAL_INSTRUCTIONS,
# MEDIA_*) are omitted — cascade lookups never fire on those because
# their element specs have no cosh_core/cosh_cascade sources.
PYTHON_L2_TO_COSH_UUID: dict[str, str] = {
    # ── INPUT / PESTICIDE ──────────────────────────────────────────────
    "CHEMICAL_PESTICIDES":                      "375b7aaf-34ab-46a9-9cb7-d0c1da30f489",
    "MICROBIAL_PESTICIDES":                     "d795e573-cd6e-4580-93a3-4960438e6a00",
    "BOTANICAL_PESTICIDES":                     "1b7d54da-081d-4123-be44-51dbe61df376",
    "INSECT_BIOCONTROL_AGENTS":                 "9c2ea425-6fa1-41cf-8ffd-f3ebb65d77d9",
    "INSECT_TRAPS":                             "f2d9e6d6-de53-4ab0-b1c0-05e0ec048096",
    "CHEMICAL_HERBICIDES":                      "bae16db2-4bc1-4aef-9d49-b5db5825afec",
    "OTHER_PESTICIDES":                         "6b4034bb-6177-47ca-90dc-d2ecddc0f066",
    # ── INPUT / SPECIAL ────────────────────────────────────────────────
    "ADJUVANTS":                                "da7d62de-6b52-4fd2-8f71-386dc4b0a9a0",
    # ── INPUT / FERTILIZER ─────────────────────────────────────────────
    "MANURES":                                  "9bd6fb52-08ec-4673-b7ed-d1c8266223c3",
    "CHEMICAL_FERTILIZER_PRODUCTS":             "7d18296f-ad5a-4ff8-a084-57798f84dbdc",
    "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS": "e9b00598-d9cf-4232-8148-4e403e060159",
    "BIOFERTILIZERS":                           "0637b0c4-157a-4a6e-b6cc-2eb423536fb4",
    "PGR_TONICS":                               "2c3f0d40-9081-4416-b4fc-b128b06e8e7d",
    "SOIL_AMENDMENTS":                          "bed6a3e5-3844-48ad-b25e-f56792966fd7",
    "CHEMICAL_FERTILIZERS_NPK_DOSAGES":         "249c5760-341f-406e-9105-a570b166506c",
    "FERTIGATION_NPK_DOSAGES":                  "b7f34666-9bcd-4e9d-bfa7-99ea6446d8f2",

    # ── §2.6 NON-INPUT / Spacing + Seed Treatment Physical (Batch AA, 2026-05-19)
    # User-flagged 2026-05-19: Units-related info wasn't populated for
    # non-input practices. Root cause was this map missing the non-input
    # L2 → Cosh UUID rows — `list_units_for_l2` returned [] for any L2
    # not in the dict. The `l2_units_unittypes` Connect has rows for all
    # of these; resolving the slug → UUID lookup unblocks the dropdown.
    "SPACING_PLANT_TO_PLANT":     "86a1f009-3838-4dc1-a61e-be3a9fb1f68d",
    "SPACING_ROW_TO_ROW":         "f7db24aa-3a81-4f80-acb8-4265be6489b1",
    "SEED_TREATMENT_HOT_WATER":   "b78525ea-6561-4e79-a4f7-0422484a377b",
    "SEED_TREATMENT_HOT_AIR":     "44e6852a-1b6c-4f5a-ba45-75342c7b9358",
    "SEED_TREATMENT_COLD":        "5e5dba09-5b51-450a-b8a2-b980c59c9d58",
    "SEED_TREATMENT_BOILING":     "1b217746-2022-4119-aefe-5f5efe733175",
    "SEED_TREATMENT_SOAKING":     "740df3a5-2a5d-4d64-bc42-6039dd773a37",
    "SEED_TREATMENT_SUN_DRYING":  "f22f39df-8b35-49b4-84a5-106fb10d76f9",
    "SEED_TREATMENT_SHADE_DRYING":"e4eb2248-fb61-4329-873c-94a440fd73e6",

    # ── §2.6 NON-INPUT / Sowing/Planting Methods
    "SOWING_LINE":          "c424dd0f-0c15-4d23-8d6e-76682cb7bfd4",
    "SOWING_BROADCASTING":  "690a0c51-4343-419c-8dc3-fb692e40a245",
    "SOWING_DIBBLING":      "1a92deaa-056f-4f48-a9e4-482d9defeed6",
    "SOWING_DRILLING":      "301e5854-bd44-45da-9f18-e496d36fa96a",
    "SOWING_TRANSPLANTING": "bc897c1d-f47b-422f-bd4f-2829c79cf874",
    "SOWING_HILL_DROPPING": "341ead61-0da1-483f-8069-f513631c82bc",
    "SOWING_CHECK_ROW":     "059aaa20-448f-4e61-96d5-98267ce029b3",
    "SOWING_PRO_TRAY":      "c6bb8707-590f-4794-9a2a-71f4c01968ce",
    "SOWING_RAISED_BEDS":   "86d46efc-2b86-4648-b414-95737320ebb0",
    "SOWING_PIT_METHOD":    "e4683395-b941-4e00-b243-7c5d373fc2f7",

    # ── §2.6 NON-INPUT / Other Non-Inputs
    "PLANTING_MATERIAL_QUANTITY": "41658881-30bf-4baf-9cf0-c410f5d01d98",
    "PLANTING_SQUARE":            "4c2b3b2b-459b-4acd-85da-9d46d95abf08",
    "PLANTING_RECTANGULAR":       "e525ec7f-3ca7-4359-8ffa-f864717d4ffb",
    "PLANTING_TRIANGULAR":        "5c035605-6e2f-4982-81aa-155ff3a72e83",
    "PLANTING_HEXAGONAL":         "5f0351e6-fe5a-4c98-99c1-10683e4f31bd",
    "PLANTING_QUINCUNX":          "37c44217-7167-40f6-abd8-52f9925ea0ac",
    "CONTOUR_SYSTEM":             "cddaf342-583a-41e2-92e0-acf350698bcf",
    "ITKS":                       "22a3a5e1-cab9-4e36-8a26-715d90c8e086",

    # Irrigation L2s (frequency-based). Furrow / Flood / Basin don't
    # currently appear in the l2_units_unittypes Connect — their rule
    # book entries don't have unit-typed fields. Mapped anyway so future
    # Cosh additions surface without another code change.
    "WATER_DRIP":      "192eb9fb-ada5-4773-b2c9-c2b413ba1174",
    "WATER_SPRINKLER": "53decf29-04e3-4640-8cb2-bd87d9ffbad6",
    "WATER_FURROW":    "c6a7f6ba-8352-4fdd-8600-0187ee13a247",
    "WATER_FLOOD":     "295f1ba9-06ed-404d-ba0e-1cac4de0321e",
    "WATER_BASIN":     "8f4ee59e-5970-487b-bf57-fd9f3bd6a6a0",

    "WEED_MANUAL":     "dc23865d-fb2d-482e-94fb-084752b1945b",

    "CULTURAL_THINNING":              "c83733b3-b8a4-40c1-9a5b-8ddb935d9436",
    "CULTURAL_GAP_FILLING":           "6ec45032-106b-4339-bf38-848e68cbc069",
    # CULTURAL_EARTHING_UP — not yet in Cosh sync; will resolve to None
    # in _l2_uuid and the units dropdown will stay empty for it.
    "CULTURAL_TOP_DRESSING":          "daccb959-d2e8-4b30-a29a-cf5ee36bad8d",
    "CULTURAL_STAKING":               "a65e02df-26e6-43f2-b144-df0f53256427",
    "CULTURAL_ARTIFICIAL_POLLINATION":"c176010d-d357-43e4-b9f7-60394fcc2711",
    "CULTURAL_TRAINING":              "51d9cf66-ad1e-445a-92d9-c074f2ee0f63",
    "CULTURAL_PRUNING":               "f628bc11-7c4c-4165-9c82-720c92375fec",
    "CULTURAL_SHADE_MANAGEMENT":      "95fc1732-f067-448e-a986-1eb5b9aa2546",
    "CULTURAL_WIND_BREAKS":           "ea709d94-a81c-4976-a8d4-4e224225684c",
    "CULTURAL_DEFLOWERING":           "cfb80c0e-d38a-46fe-ae32-ad146c61d8e3",
    "CULTURAL_NIPPING_PINCHING":      "594b7703-ce76-403e-a16a-1b7e21cae50e",
    "CULTURAL_DESHOOTING":            "932b6cfa-409b-44cb-8ac5-5bca33281eab",
    "CULTURAL_DESUCKERING":           "169091fe-14c3-4b90-86c7-775ed536ef76",

    "HARVESTING_MANUAL":          "3b519992-52fb-4406-9182-f53dc3061ca0",

    "POST_HARVEST_DEHULLING":     "53b06365-400e-4c92-892f-ff32b284141c",
    "POST_HARVEST_DEHUSKING":     "543ce5dc-034a-45bc-9107-4fa9597c7cbd",
    "POST_HARVEST_SHELLING":      "dae94293-e82e-45ac-807f-dc162a8333ad",
    "POST_HARVEST_DRYING":        "7c4e05e2-0e33-4a25-8864-2b27968b2cbf",
    "POST_HARVEST_CURING":        "dc6b4ff7-7b97-4bd6-b2b6-950ab9e7a71e",
    "POST_HARVEST_WINNOWING":     "5321d916-2c94-4d4f-86ff-814b1c9a023b",
    "POST_HARVEST_CLEANING":      "a7d55c82-4475-4ce6-8a92-85e08c3cb28c",
    "POST_HARVEST_TRIMMING":      "2b82460c-9d0c-4a4f-aca2-e9cf9282adc7",
    "POST_HARVEST_SORTING":       "b863efb3-237e-4d8f-a463-45f49e866d74",
    "POST_HARVEST_GRADING":       "a2d69622-9b74-4064-9bda-4810a03893c7",
    "POST_HARVEST_PACKING":       "21adf2f2-e409-42c1-8a06-961c2bff8b31",
    "POST_HARVEST_COLD_STORAGE":  "96222fd5-d8e1-40c9-8cb6-95cd49c7c601",
}


# Unit-type slug → Cosh `unit_types` cosh_id list. The slug is what the
# rule book uses in source strings like `cosh_core:dosage_unit`.
# Cosh has 12 unit_types; we collapse the 3 dosage variants into
# one `dosage_unit` slug for V1 — SE picks from a unified list.
UNIT_TYPE_SLUG_TO_COSH_UUIDS: dict[str, list[str]] = {
    "dosage_unit": [
        "1c644e28-a81e-4f30-bbb3-d1f1a7b5e013",  # Dosage Unit
        "7762d0bb-9fba-48ae-9587-1af180f238b7",  # Dosage Unit (without dilution)
        "e6387449-da6a-464e-a769-dea3e8a32600",  # Dosage Unit (with dilution)
        # Cosh classifies CHEMICAL_FERTILIZERS_NPK_DOSAGES units (g/plant,
        # kg/acre, kg/plant) under the generic "Unit" type instead of one
        # of the three "Dosage Unit" variants above. Including it here
        # lets the l2_units_unittypes walk surface those rows for the
        # NPK Dosage L2's UNIT dropdown (Batch 38, 2026-05-15). Only one
        # L2 (CHEMICAL_FERTILIZERS_NPK_DOSAGES) uses this type today, so
        # widening the slug doesn't cross-contaminate other L2s.
        "11a14b5b-1bc9-4d15-8c9a-af1f7310578c",  # Unit
    ],
    "volume_unit":      ["0462580b-0c2f-4607-ace5-76a00055c3db"],
    "temperature_unit": ["0d433397-7998-4c52-b469-d5ac6645570d"],
    # The rule book uses the single `distance_unit` slug for every
    # distance-y field (DISTANCE_UNIT, DEPTH_UNIT, SIZE_UNIT,
    # SPACING_BETWEEN_BEDS_UNIT etc.) because the physical units are
    # the same (cm, inch, m, …). Cosh's `unit_types` Core splits them
    # into Distance / Depth / Size as separate categories, so the
    # slug must include all three Cosh UUIDs — otherwise Sowing-depth
    # rows tagged with "Depth Unit" and Raised-bed rows tagged with
    # "Size Unit" never surface in the dropdown (Batch AA-1,
    # 2026-05-19 — user-flagged: Line Sowing rows showed empty).
    "distance_unit": [
        "bd97312b-c5f1-42f3-b529-c1a81b33e45c",  # Distance Unit
        "75d2d141-b711-4174-bc6d-50e95c007f2e",  # Depth Unit
        "621b95a4-08a9-456d-9ba5-e158e16052db",  # Size Unit
    ],
    "time_unit":        ["7fd6e2aa-e5be-4517-8c09-ad6a28f8ad72"],
    "number_unit":      ["5abe34c0-2dec-46ec-89cb-d8f1a683525f"],
    "irrigation_unit":  ["e1458bd8-4be7-43b5-9bb0-6c24447e6a0d"],
    # `size_unit` and `depth_unit` slugs kept as alternates in case
    # a future rule-book entry wants a narrower scope. Today they're
    # unused — every distance-y field maps to `distance_unit` above.
    "size_unit":        ["621b95a4-08a9-456d-9ba5-e158e16052db"],
    "depth_unit":       ["75d2d141-b711-4174-bc6d-50e95c007f2e"],
}
