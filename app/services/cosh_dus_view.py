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
    is_blank_box,
)


def _endpoint_at_position(row: CoshConnectRow, position: int) -> Optional[str]:
    for e in row.endpoints or []:
        if e.get("position") == position:
            return e.get("cosh_id")
    return None


async def _resolve_core_names(
    db: AsyncSession, *, core_type: str, cosh_ids: set[str], lang: str = "en",
) -> dict[str, str]:
    """Thin wrapper over the central helper. Default lang=en preserves
    legacy behaviour for any caller that doesn't yet thread the user's
    language through."""
    from app.services.i18n_cosh import resolve_names_by_cosh_id
    return await resolve_names_by_cosh_id(
        db, set(cosh_ids), lang, core_type=core_type,
    )


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
    db: AsyncSession, *, crop_cosh_id: str, lang: str = "en",
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

    **BLANK BOX handling** (Batch W-1, 2026-05-19):

    * Rows where the **part** is BLANK BOX are dropped entirely —
      no part = no DUS row the SE can meaningfully pick.
    * Rows where the **subpart** is BLANK BOX collapse the subpart
      level to a "not applicable" entry: `subpart_cosh_id=None,
      subpart_name_en=None`. The frontend skips the subpart
      dropdown for parts whose only subpart is None, otherwise
      surfaces it as "— not applicable —".

    BLANK BOX UUIDs come from `COSH_BLANK_BOX_BY_CORE` — extend
    that dict if Cosh adds the sentinel to a new Core.
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
        db, core_type=COSH_PLANT_PARTS_CORE, cosh_ids=part_ids, lang=lang,
    )
    subpart_names = await _resolve_core_names(
        db, core_type=COSH_PLANT_SUBPARTS_CORE, cosh_ids=subpart_ids, lang=lang,
    )
    character_names = await _resolve_core_names(
        db, core_type=COSH_DUS_CHARACTERS_CORE, cosh_ids=character_ids, lang=lang,
    )
    descriptor_names = await _resolve_core_names(
        db, core_type=COSH_DUS_DESCRIPTORS_CORE, cosh_ids=descriptor_ids, lang=lang,
    )

    # tree[part_id][subpart_id_or_None][character_id] = set[descriptor_id]
    # Subpart key is None when the row's subpart endpoint is BLANK BOX.
    tree: dict[str, dict[str | None, dict[str, set[str]]]] = {}
    for r in rows:
        part = _endpoint_at_position(r, DCD_POS_PART)
        subpart = _endpoint_at_position(r, DCD_POS_SUBPART)
        character = _endpoint_at_position(r, DCD_POS_CHARACTER)
        descriptor = _endpoint_at_position(r, DCD_POS_DESCRIPTOR)
        # BLANK BOX at the part level → drop row (no meaningful part
        # for the SE to pick).
        if is_blank_box(COSH_PLANT_PARTS_CORE, part):
            continue
        # BLANK BOX at the subpart level → collapse to None.
        if is_blank_box(COSH_PLANT_SUBPARTS_CORE, subpart):
            subpart = None
        # Drop if any referenced (non-BLANK-BOX) Core item is
        # inactive / missing — those have no entry in the name maps.
        if part not in part_names:
            continue
        if subpart is not None and subpart not in subpart_names:
            continue
        if character not in character_names:
            continue
        if descriptor not in descriptor_names:
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
                "subpart_name_en": (
                    None if subpart_id is None else _name(subpart_names, subpart_id)
                ),
                "characters": character_list,
            })
        # None (BLANK BOX) subpart sorts first; named subparts after,
        # alphabetised. The frontend keys on this order.
        subpart_list.sort(key=lambda x: (
            0 if x["subpart_cosh_id"] is None else 1,
            (x["subpart_name_en"] or "").casefold(),
        ))
        out.append({
            "part_cosh_id": part_id,
            "part_name_en": _name(part_names, part_id),
            "subparts": subpart_list,
        })
    out.sort(key=lambda x: x["part_name_en"].casefold())
    return out
