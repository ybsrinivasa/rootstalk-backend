"""Hardcoded Problem-Group list — V1 stopgap until Cosh ships the
`problem_group` Connect (CHA hub Round 1, 2026-05-10).

The user expects ~70 PGs to land via Cosh later. Until then, the SE
needs *something* to pick from when authoring a CHA recommendation.
This module provides a curated subset that covers the common CHA
authoring scenarios in pilot data — the SE can still author
content, and when Cosh's PG sync arrives the list source swaps to
`cosh_core_items.core_type='problem_group'` without touching any
caller (just rewire `list_problem_groups`).

Each entry mirrors the shape Cosh will eventually emit:
  cosh_id (stable UUID-like slug) + name_en (display) + status.

Status of `active` for all entries — at this surface there's no need
for inactive distinction yet.
"""
from __future__ import annotations


_PROBLEM_GROUPS_V1: list[dict] = [
    {"cosh_id": "pg:fungal_diseases",       "name_en": "Fungal Diseases"},
    {"cosh_id": "pg:bacterial_diseases",    "name_en": "Bacterial Diseases"},
    {"cosh_id": "pg:viral_diseases",        "name_en": "Viral Diseases"},
    {"cosh_id": "pg:nematode_infestations", "name_en": "Nematode Infestations"},
    {"cosh_id": "pg:sucking_pests",         "name_en": "Sucking Pests"},
    {"cosh_id": "pg:chewing_pests",         "name_en": "Chewing Pests"},
    {"cosh_id": "pg:boring_pests",          "name_en": "Boring Pests"},
    {"cosh_id": "pg:weed_competition",      "name_en": "Weed Competition"},
    {"cosh_id": "pg:nutrient_deficiency",   "name_en": "Nutrient Deficiency"},
    {"cosh_id": "pg:water_stress",          "name_en": "Water Stress"},
    {"cosh_id": "pg:heat_stress",           "name_en": "Heat Stress"},
    {"cosh_id": "pg:cold_stress",           "name_en": "Cold Stress"},
]


def list_problem_groups() -> list[dict]:
    """Return the V1 PG list, sorted alphabetically by display name.
    Identical shape to what `cosh_crop_view.list_crops` returns for
    crops — keeps the four-screen-hub frontend code symmetrical."""
    return [
        {**pg, "status": "active"}
        for pg in sorted(_PROBLEM_GROUPS_V1, key=lambda p: p["name_en"].lower())
    ]


def is_known_problem_group(cosh_id: str) -> bool:
    """Membership check used by validation paths that need to refuse
    a PG cosh_id the V1 list doesn't carry. Once Cosh ships the
    Connect, this becomes a DB lookup against `cosh_core_items`."""
    return any(pg["cosh_id"] == cosh_id for pg in _PROBLEM_GROUPS_V1)
