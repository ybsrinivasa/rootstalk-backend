"""Read-through service over Cosh's `dus_characters_descriptors`
Connect (synced 2026-05-19, 1,562 active rows). 5-endpoint shape:

  pos 1: biological_names   ← the crop
  pos 2: plant_parts        ← LEAF / SHOOT / SPIKE / …
  pos 3: plant_subparts     ← LAMINA / SPINES / CLOVES / …
  pos 4: dus_characters     ← "Lobing" / "Vigor" / etc.
  pos 5: dus_descriptors    ← "Weak" / "Thick" / etc.

Each Connect row asserts that ONE specific descriptor is a valid
value for ONE character on a given crop / part / sub-part. The
SE's Seed Varieties DUS picker needs the inverse view: for a
chosen crop, list every (part, sub-part, character) tuple along
with the discrete descriptor values the character can take.

`list_dus_options_for_crop` returns the full crop-scoped tree in
one round-trip so the frontend can cascade dropdowns without
chatty per-level fetches.

Pure read-through. RootsTalk does not mirror these — the picker
queries through this service every time.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_DUS_CHARACTERS_CORE,
    COSH_DUS_CHARACTERS_DESCRIPTORS_CONNECT,
    COSH_DUS_DESCRIPTORS_CORE,
    COSH_PLANT_PARTS_CORE,
    COSH_PLANT_SUBPARTS_CORE,
    DCD_POS_CHARACTER,
    DCD_POS_CROP,
    DCD_POS_DESCRIPTOR,
    DCD_POS_PART,
    DCD_POS_SUBPART,
)


def _endpoint_at_position(row: CoshConnectRow, position: int) -> Optional[str]:
    for e in row.endpoints or []:
        if e.get("position") == position:
            return e.get("cosh_id")
    return None


def _translation_en(core: Optional[CoshCoreItem], fallback: str) -> str:
    if core is None:
        return fallback
    t = core.translations or {}
    return t.get("en") or t.get("English") or fallback


async def _resolve_core_names(
    db: AsyncSession, *, core_type: str, cosh_ids: set[str],
) -> dict[str, str]:
    cosh_ids = {c for c in cosh_ids if c}
    if not cosh_ids:
        return {}
    cores = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == core_type,
            CoshCoreItem.cosh_id.in_(cosh_ids),
            CoshCoreItem.status == "active",
        )
    )).scalars().all()
    return {c.cosh_id: _translation_en(c, c.cosh_id) for c in cores}


async def _walk_rows_for_crop(
    db: AsyncSession, *, crop_cosh_id: str,
) -> list[CoshConnectRow]:
    all_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_DUS_CHARACTERS_DESCRIPTORS_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    return [
        r for r in all_rows
        if _endpoint_at_position(r, DCD_POS_CROP) == crop_cosh_id
    ]


async def list_dus_options_for_crop(
    db: AsyncSession, *, crop_cosh_id: str,
) -> list[dict]:
    """Full DUS taxonomy for a crop, nested for cascading pickers.

    Shape:
        [
          {
            part_cosh_id, part_name_en,
            subparts: [
              {
                subpart_cosh_id, subpart_name_en,
                characters: [
                  {
                    character_cosh_id, character_name_en,
                    descriptors: [
                      {descriptor_cosh_id, descriptor_name_en},
                      ...
                    ],
                  },
                  ...
                ],
              },
              ...
            ],
          },
          ...
        ]

    Empty list when the crop has no rows (Cosh hasn't characterised
    it yet). Sorted alphabetically at every level by English name;
    inactive Core items are silently dropped — a stale part /
    character / descriptor never surfaces in the dropdown.
    """
    rows = await _walk_rows_for_crop(db, crop_cosh_id=crop_cosh_id)
    if not rows:
        return []

    part_ids: set[str] = set()
    subpart_ids: set[str] = set()
    character_ids: set[str] = set()
    descriptor_ids: set[str] = set()
    for r in rows:
        part_ids.add(_endpoint_at_position(r, DCD_POS_PART) or "")
        subpart_ids.add(_endpoint_at_position(r, DCD_POS_SUBPART) or "")
        character_ids.add(_endpoint_at_position(r, DCD_POS_CHARACTER) or "")
        descriptor_ids.add(_endpoint_at_position(r, DCD_POS_DESCRIPTOR) or "")

    part_names = await _resolve_core_names(
        db, core_type=COSH_PLANT_PARTS_CORE, cosh_ids=part_ids,
    )
    subpart_names = await _resolve_core_names(
        db, core_type=COSH_PLANT_SUBPARTS_CORE, cosh_ids=subpart_ids,
    )
    character_names = await _resolve_core_names(
        db, core_type=COSH_DUS_CHARACTERS_CORE, cosh_ids=character_ids,
    )
    descriptor_names = await _resolve_core_names(
        db, core_type=COSH_DUS_DESCRIPTORS_CORE, cosh_ids=descriptor_ids,
    )

    # tree[part_id][subpart_id][character_id] = set[descriptor_id]
    tree: dict[str, dict[str, dict[str, set[str]]]] = {}
    for r in rows:
        part = _endpoint_at_position(r, DCD_POS_PART)
        subpart = _endpoint_at_position(r, DCD_POS_SUBPART)
        character = _endpoint_at_position(r, DCD_POS_CHARACTER)
        descriptor = _endpoint_at_position(r, DCD_POS_DESCRIPTOR)
        # Drop if any referenced Core item is inactive / missing —
        # those have no entry in the name maps.
        if (
            part not in part_names
            or subpart not in subpart_names
            or character not in character_names
            or descriptor not in descriptor_names
        ):
            continue
        tree.setdefault(part, {}) \
            .setdefault(subpart, {}) \
            .setdefault(character, set()) \
            .add(descriptor)

    def _name(d: dict[str, str], k: str) -> str:
        return d.get(k, k)

    out: list[dict] = []
    for part_id, subparts in tree.items():
        subpart_list = []
        for subpart_id, characters in subparts.items():
            character_list = []
            for character_id, descriptors in characters.items():
                descriptor_list = sorted(
                    [
                        {
                            "descriptor_cosh_id": d_id,
                            "descriptor_name_en": _name(descriptor_names, d_id),
                        }
                        for d_id in descriptors
                    ],
                    key=lambda x: x["descriptor_name_en"].casefold(),
                )
                character_list.append({
                    "character_cosh_id": character_id,
                    "character_name_en": _name(character_names, character_id),
                    "descriptors": descriptor_list,
                })
            character_list.sort(key=lambda x: x["character_name_en"].casefold())
            subpart_list.append({
                "subpart_cosh_id": subpart_id,
                "subpart_name_en": _name(subpart_names, subpart_id),
                "characters": character_list,
            })
        subpart_list.sort(key=lambda x: x["subpart_name_en"].casefold())
        out.append({
            "part_cosh_id": part_id,
            "part_name_en": _name(part_names, part_id),
            "subparts": subpart_list,
        })
    out.sort(key=lambda x: x["part_name_en"].casefold())
    return out
