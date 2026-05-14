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
    ],
    "volume_unit":      ["0462580b-0c2f-4607-ace5-76a00055c3db"],
    "temperature_unit": ["0d433397-7998-4c52-b469-d5ac6645570d"],
    "distance_unit":    ["bd97312b-c5f1-42f3-b529-c1a81b33e45c"],
    "time_unit":        ["7fd6e2aa-e5be-4517-8c09-ad6a28f8ad72"],
    "number_unit":      ["5abe34c0-2dec-46ec-89cb-d8f1a683525f"],
    "irrigation_unit":  ["e1458bd8-4be7-43b5-9bb0-6c24447e6a0d"],
    "size_unit":        ["621b95a4-08a9-456d-9ba5-e158e16052db"],
    "depth_unit":       ["75d2d141-b711-4174-bc6d-50e95c007f2e"],
}
