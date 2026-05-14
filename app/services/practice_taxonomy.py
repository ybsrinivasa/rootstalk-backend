"""Practice L0 / L1 / L2 hierarchy.

The L2 element rule book (`app/services/l2_element_rules.py`)
keys each Practice subtype's element-spec by L2 name. The L0
enum (`PracticeL0`) is on the model. But the L1 grouping that
sits between them — "PESTICIDE", "FERTILIZER", "SOWING_METHOD"
etc — has only lived in section comments inside the rule book
and in piecemeal references (`relation_validation.L1_PESTICIDE_GROUP`).

This module surfaces the canonical L0 → L1 → L2 taxonomy as
pure data so the SA / CA portals can render cascading dropdowns
instead of free-text inputs. No DB access. Reconciled with the
Agriculture-Team-Document v5 §2.3–§2.10 sections.

When Cosh ships a `practice_taxonomy` Core + Connect, we can
swap this in-process tree for a DB-backed one — the shape of
`get_practice_taxonomy()` and `list_l2_elements()` are the
stable contract.
"""
from __future__ import annotations

from app.services.l2_element_rules import (
    L2_ELEMENT_RULES, applies_plant_wise_extras, get_l2_spec,
)


# Display labels keep the same ALL_CAPS storage form but show
# something friendlier in dropdowns ("Chemical Pesticide" rather
# than "CHEMICAL_PESTICIDES"). Generated on demand below.
def _label_from_id(value: str) -> str:
    return " ".join(part.capitalize() for part in value.split("_"))


# L0 → L1 → [L2, ...] canonical hierarchy. Derived from
# Agriculture-Team-Document v5 + verified against the rule-book
# section comments in l2_element_rules.py.

TAXONOMY: dict[str, dict[str, list[str]]] = {
    "INPUT": {
        "PESTICIDE": [
            "CHEMICAL_PESTICIDES",
            "MICROBIAL_PESTICIDES",
            "BOTANICAL_PESTICIDES",
            "INSECT_BIOCONTROL_AGENTS",
            "INSECT_TRAPS",
            "CHEMICAL_HERBICIDES",
            "OTHER_PESTICIDES",
        ],
        "SPECIAL_INPUT": [
            "ADJUVANTS",
        ],
        "FERTILIZER": [
            "MANURES",
            "CHEMICAL_FERTILIZER_PRODUCTS",
            "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS",
            "BIOFERTILIZERS",
            "PGR_TONICS",
            "SOIL_AMENDMENTS",
            "CHEMICAL_FERTILIZERS_NPK_DOSAGES",
            "FERTIGATION_NPK_DOSAGES",
        ],
    },
    "NON_INPUT": {
        "SPACING": [
            "SPACING_PLANT_TO_PLANT",
            "SPACING_ROW_TO_ROW",
        ],
        "SEED_TREATMENT": [
            "SEED_TREATMENT_HOT_WATER",
            "SEED_TREATMENT_HOT_AIR",
            "SEED_TREATMENT_COLD",
            "SEED_TREATMENT_BOILING",
            "SEED_TREATMENT_SOAKING",
            "SEED_TREATMENT_SUN_DRYING",
            "SEED_TREATMENT_SHADE_DRYING",
        ],
        "SOWING_METHOD": [
            "SOWING_LINE",
            "SOWING_BROADCASTING",
            "SOWING_DIBBLING",
            "SOWING_DRILLING",
            "SOWING_TRANSPLANTING",
            "SOWING_HILL_DROPPING",
            "SOWING_CHECK_ROW",
            "SOWING_PRO_TRAY",
            "SOWING_RAISED_BEDS",
            "SOWING_PIT_METHOD",
        ],
        "PLANTING_MATERIAL": [
            "PLANTING_MATERIAL_QUANTITY",
        ],
        "PLANTING_SYSTEM": [
            "PLANTING_SQUARE",
            "PLANTING_RECTANGULAR",
            "PLANTING_TRIANGULAR",
            "PLANTING_HEXAGONAL",
            "PLANTING_QUINCUNX",
            "CONTOUR_SYSTEM",
        ],
        "ITKS": [
            "ITKS",
        ],
        "IRRIGATION": [
            "WATER_DRIP",
            "WATER_SPRINKLER",
            "WATER_FURROW",
            "WATER_FLOOD",
            "WATER_BASIN",
        ],
        "WEED_MANAGEMENT": [
            "WEED_MANUAL",
        ],
        "CULTURAL": [
            "CULTURAL_THINNING",
            "CULTURAL_GAP_FILLING",
            "CULTURAL_EARTHING_UP",
            "CULTURAL_TOP_DRESSING",
            "CULTURAL_STAKING",
            "CULTURAL_ARTIFICIAL_POLLINATION",
            "CULTURAL_TRAINING",
            "CULTURAL_PRUNING",
            "CULTURAL_SHADE_MANAGEMENT",
            "CULTURAL_WIND_BREAKS",
            "CULTURAL_DEFLOWERING",
            "CULTURAL_NIPPING_PINCHING",
            "CULTURAL_DESHOOTING",
            "CULTURAL_DESUCKERING",
        ],
        "HARVESTING": [
            "HARVESTING_MANUAL",
        ],
        "POST_HARVEST": [
            "POST_HARVEST_DEHULLING",
            "POST_HARVEST_DEHUSKING",
            "POST_HARVEST_SHELLING",
            "POST_HARVEST_DRYING",
            "POST_HARVEST_CURING",
            "POST_HARVEST_WINNOWING",
            "POST_HARVEST_CLEANING",
            "POST_HARVEST_TRIMMING",
            "POST_HARVEST_SORTING",
            "POST_HARVEST_GRADING",
            "POST_HARVEST_PACKING",
            "POST_HARVEST_COLD_STORAGE",
        ],
    },
    "INSTRUCTION": {
        "GENERAL": [
            "GENERAL_INSTRUCTIONS",
        ],
    },
    "MEDIA": {
        "MEDIA": [
            "MEDIA_IMAGE",
            "MEDIA_AUDIO",
            "MEDIA_VIDEO",
            "MEDIA_HYPERLINK",
        ],
    },
}


def get_practice_taxonomy() -> list[dict]:
    """Return the L0 → L1 → L2 hierarchy in a frontend-friendly
    shape:

      [
        {
          "id": "INPUT", "label": "Input",
          "l1": [
            {
              "id": "PESTICIDE", "label": "Pesticide",
              "l2": [
                {"id": "CHEMICAL_PESTICIDES",
                 "label": "Chemical Pesticides"},
                ...
              ],
            },
            ...
          ],
        },
        ...
      ]
    """
    out = []
    for l0_id, l1_map in TAXONOMY.items():
        l1_list = []
        for l1_id, l2_ids in l1_map.items():
            l1_list.append({
                "id": l1_id,
                "label": _label_from_id(l1_id),
                "l2": [
                    {"id": l2_id, "label": _label_from_id(l2_id)}
                    for l2_id in l2_ids
                ],
            })
        out.append({
            "id": l0_id,
            "label": _label_from_id(l0_id),
            "l1": l1_list,
        })
    return out


def list_l2_elements(
    l2_type: str,
    *,
    crop_measure: str | None = None,
) -> list[dict] | None:
    """Return the element-spec for a given L2 (as per the rule
    book), shaped for the frontend:

      [
        {"name": "COMMON_NAME", "label": "Common Name",
         "source": "cosh_core:common_name", "mandatory": True,
         "mandatory_if_set": [], "cascade_from": [],
         "auto_selected": False},
        ...
      ]

    Returns None when l2_type isn't in the rule book — the
    frontend renders "no elements defined" in that case.

    Plant-wise extras (VOLUME_PER_PLANT + VOLUME_PER_PLANT_UNIT)
    are appended **only when `crop_measure == "PLANT_WISE"` AND
    the L2 opts in via PLANT_WISE_EXTRAS_APPLY_TO**. User decision
    2026-05-11: AREA_WISE crops (or unclassified / no measure
    supplied) should never see the plant-wise dosage fields —
    they don't apply.
    """
    spec = get_l2_spec(l2_type)
    if spec is None:
        return None
    fields = list(spec.fields)
    if crop_measure == "PLANT_WISE" and applies_plant_wise_extras(l2_type):
        from app.services.l2_element_rules import PLANT_WISE_EXTRA_FIELDS
        fields.extend(PLANT_WISE_EXTRA_FIELDS)
    return [
        {
            "name": f.name,
            "label": _label_from_id(f.name),
            "source": f.source,
            "mandatory": f.mandatory,
            "mandatory_if_set": list(f.mandatory_if_set),
            "cascade_from": list(f.cascade_from),
            "cascade_optional_inputs": list(f.cascade_optional_inputs),
            "auto_selected": f.auto_selected,
        }
        for f in fields
    ]


def get_l2_meta(l2_type: str) -> dict | None:
    """L2-level metadata flags from the rule book — separate from
    the per-field element specs. Used by the frontend to decide
    whether to render UI affordances that only apply to certain L2s
    (e.g. the Special Input checkbox is meaningful only for L2s
    with `is_special_input=True`, which today is just ADJUVANTS).
    Returns None when l2_type isn't in the rule book."""
    spec = get_l2_spec(l2_type)
    if spec is None:
        return None
    return {
        "is_special_input": spec.is_special_input,
        "frequency_based": spec.frequency_based,
        "plant_wise_extras": spec.plant_wise_extras,
    }


# Lookup helpers used by tests + future validators.

def is_known_l2(l2_type: str) -> bool:
    return l2_type in L2_ELEMENT_RULES


def list_all_l2_ids() -> list[str]:
    out: list[str] = []
    for l1_map in TAXONOMY.values():
        for l2_ids in l1_map.values():
            out.extend(l2_ids)
    return out
