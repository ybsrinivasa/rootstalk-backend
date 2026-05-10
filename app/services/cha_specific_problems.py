"""Hardcoded Specific-Problem list keyed by crop — V1 stopgap until
Cosh ships the `specific_problem` Connect (CHA-SP hub Round 1,
2026-05-10).

User locked the model 2026-05-10:
  • SE picks a crop first (from the SP-eligible-crops set =
    ClientCrop ∩ CropHealthCrop).
  • Then sees the list of specific problems associated with that
    crop.
  • Picks one; an SP recommendation row is created for that
    (client, crop, specific_problem).

When Cosh ships the `specific_problem` Connect (linking each
specific_problem to its crop), swap the source in
`list_specific_problems_for_crop()`. Every caller stays the same.

The V1 set is intentionally narrow — about 5 problems per crop for
the most common Karnataka pilot crops. Easy to extend by editing
this file; full population will come from Cosh.
"""
from __future__ import annotations


_SPECIFIC_PROBLEMS_V1: dict[str, list[dict]] = {
    "crop:tomato": [
        {"cosh_id": "sp:tomato_late_blight",         "name_en": "Tomato Late Blight"},
        {"cosh_id": "sp:tomato_early_blight",        "name_en": "Tomato Early Blight"},
        {"cosh_id": "sp:tomato_fusarium_wilt",       "name_en": "Tomato Fusarium Wilt"},
        {"cosh_id": "sp:tomato_mosaic_virus",        "name_en": "Tomato Mosaic Virus"},
        {"cosh_id": "sp:tomato_fruit_borer",         "name_en": "Tomato Fruit Borer"},
    ],
    "crop:paddy": [
        {"cosh_id": "sp:paddy_blast",                "name_en": "Paddy Blast"},
        {"cosh_id": "sp:paddy_bacterial_leaf_blight", "name_en": "Paddy Bacterial Leaf Blight"},
        {"cosh_id": "sp:paddy_brown_planthopper",    "name_en": "Paddy Brown Planthopper"},
        {"cosh_id": "sp:paddy_stem_borer",           "name_en": "Paddy Stem Borer"},
        {"cosh_id": "sp:paddy_sheath_blight",        "name_en": "Paddy Sheath Blight"},
    ],
    "crop:onion": [
        {"cosh_id": "sp:onion_purple_blotch",        "name_en": "Onion Purple Blotch"},
        {"cosh_id": "sp:onion_thrips",               "name_en": "Onion Thrips"},
        {"cosh_id": "sp:onion_basal_rot",            "name_en": "Onion Basal Rot"},
    ],
    "crop:chilli": [
        {"cosh_id": "sp:chilli_anthracnose",         "name_en": "Chilli Anthracnose"},
        {"cosh_id": "sp:chilli_thrips",              "name_en": "Chilli Thrips"},
        {"cosh_id": "sp:chilli_leaf_curl_virus",     "name_en": "Chilli Leaf Curl Virus"},
    ],
    "crop:cotton": [
        {"cosh_id": "sp:cotton_bollworm",            "name_en": "Cotton Bollworm"},
        {"cosh_id": "sp:cotton_pink_bollworm",       "name_en": "Cotton Pink Bollworm"},
        {"cosh_id": "sp:cotton_leafhopper",          "name_en": "Cotton Leafhopper"},
    ],
}


def list_specific_problems_for_crop(crop_cosh_id: str) -> list[dict]:
    """Return the V1 specific-problem list for the given crop, sorted
    by display name. Crops not in the V1 set return []. Same shape
    Cosh will eventually emit (cosh_id + name_en + status)."""
    items = _SPECIFIC_PROBLEMS_V1.get(crop_cosh_id, [])
    return [
        {**sp, "status": "active"}
        for sp in sorted(items, key=lambda s: s["name_en"].lower())
    ]


def is_known_specific_problem(crop_cosh_id: str, sp_cosh_id: str) -> bool:
    """Membership check used by validation paths that need to refuse
    an SP cosh_id outside the V1 list-for-this-crop. Once Cosh ships
    the Connect, this becomes a DB lookup."""
    return any(
        sp["cosh_id"] == sp_cosh_id
        for sp in _SPECIFIC_PROBLEMS_V1.get(crop_cosh_id, [])
    )
