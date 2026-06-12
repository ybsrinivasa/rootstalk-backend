"""
Reference Image Lookup for Diagnosis Questions

Given the current diagnosis question's filter (crop + part + symptom +
optional sub_part / sub_symptom), walk Cosh's typed graph to surface
the reference images Cosh has curated for matching scenarios. Used by
the PWA to show "this is what the symptom looks like" carousel during
self-diagnosis.

Cosh wire shape (locked 2026-05-14):

  • `pest_diagnosis` Connect rows carry 9 typed endpoints by position
    (see `app/services/cosh_constants.py::PD_POS_*` and the project
    memory `project_rootstalk_pest_diagnosis.md`).

  • `image_index` Connect maps a crop → a per-crop image Connect slug
    (e.g. `tomato_symptom_images`).

  • `{crop}_symptom_images` Connect rows carry (pos 1 = the
    pest_diagnosis row id, pos 2 = the image Core cosh_id).

  • image Core items carry the asset URL in `metadata_.s3_path`.

Rewired from `pest_diagnosis_chain` (slug Cosh never shipped) on
2026-05-25 to consume real Cosh data. The filter step now reuses
`pest_diagnosis_view._filter_rows`; the image-resolution step
reuses `pest_diagnosis_images_view.images_for_pest_diagnosis_rows`.
Both already power the `/diagnosis/candidates` `image_urls` field,
so this function inherits the same correctness.

Fallback when zero images are found is the caller's job — this service
just returns `[]`. The endpoint layer adds the Google-Images URL.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshCoreItem
from app.services.i18n_cosh import pick_translation


@dataclass(frozen=True)
class ReferenceImage:
    cosh_id: str
    url: Optional[str]
    media_type: str
    caption: Optional[str]


async def find_reference_images(
    db: AsyncSession,
    *,
    crop_cosh_id: str,
    part_cosh_id: str,
    symptom_cosh_id: str,
    crop_stage_cosh_id: Optional[str] = None,
    sub_part_cosh_id: Optional[str] = None,
    sub_symptom_cosh_id: Optional[str] = None,
    language_code: str = "en",
) -> list[ReferenceImage]:
    """Return reference images for a diagnosis question. Empty list
    when no curated images exist for the matching scenario."""
    from app.services.pest_diagnosis_view import (
        DiagnosisFilters, _filter_rows,
    )
    from app.services.pest_diagnosis_images_view import (
        images_for_pest_diagnosis_rows,
    )

    # 1. Filter pest_diagnosis rows that match this question.
    matching_rows = await _filter_rows(
        db,
        DiagnosisFilters(
            crop_cosh_id=crop_cosh_id,
            crop_stage=crop_stage_cosh_id,
            plant_part=part_cosh_id,
            plant_subpart=sub_part_cosh_id,
            symptom=symptom_cosh_id,
            subsymptom=sub_symptom_cosh_id,
        ),
    )
    if not matching_rows:
        return []

    # 2. Walk image_index → {crop}_symptom_images → image Core to
    # collect curated images for these rows. Returns a dict keyed by
    # diagnosis row id; we flatten + dedupe order-preserving.
    diag_row_ids = [r.connect_id for r in matching_rows]
    urls_by_diag = await images_for_pest_diagnosis_rows(
        db,
        crop_cosh_id=crop_cosh_id,
        diag_row_ids=diag_row_ids,
    )

    seen_urls: set[str] = set()
    ordered_urls: list[str] = []
    for diag_id in diag_row_ids:
        for u in urls_by_diag.get(diag_id, []):
            if u in seen_urls:
                continue
            seen_urls.add(u)
            ordered_urls.append(u)

    if not ordered_urls:
        return []

    # 3. Fetch the image Core items for their cosh_id + caption.
    # `urls_by_diag` returns S3 URLs, not Core ids — to surface
    # captions in the farmer's language we re-query the image Cores
    # whose metadata_.s3_path matches one of the urls we collected.
    candidates = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == "image",
            CoshCoreItem.status == "active",
        )
    )).scalars().all()

    by_url: dict[str, CoshCoreItem] = {}
    for c in candidates:
        meta = c.metadata_ or {}
        url = meta.get("s3_path") or meta.get("url")
        if url:
            by_url[url] = c

    out: list[ReferenceImage] = []
    for url in ordered_urls:
        c = by_url.get(url)
        translations = (c.translations or {}) if c else {}
        meta = (c.metadata_ or {}) if c else {}
        out.append(ReferenceImage(
            cosh_id=c.cosh_id if c else url,
            url=url,
            media_type=meta.get("media_type", "image"),
            caption=pick_translation(translations, language_code, "") or None,
        ))
    return out


# ── Google Images fallback URL ──────────────────────────────────────────────

async def build_google_images_query(
    db: AsyncSession,
    *,
    crop_cosh_id: str,
    part_cosh_id: str,
    symptom_cosh_id: str,
    sub_part_cosh_id: Optional[str] = None,
    sub_symptom_cosh_id: Optional[str] = None,
    language_code: str = "en",
) -> str:
    """Build the human-readable query string for Google Images search,
    using Cosh-stored translations of the question terms in the
    farmer's language. Falls back to English when the requested
    language has no translation.

    Order: symptom + sub_symptom + part + sub_part + crop. Format chosen
    so the Google result page surfaces images of the symptom on the
    plant part, on the crop."""
    lookups = [symptom_cosh_id, sub_symptom_cosh_id, part_cosh_id,
               sub_part_cosh_id, crop_cosh_id]
    lookups = [c for c in lookups if c]

    rows = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id.in_(lookups))
    )).scalars().all()
    by_id = {r.cosh_id: r for r in rows}

    parts: list[str] = []
    for cid in lookups:
        row = by_id.get(cid)
        if not row:
            continue
        name = pick_translation(row.translations, language_code, "")
        if name:
            parts.append(name)
    return " ".join(parts).strip()


def google_images_url(query: str) -> str:
    """Build the Google Images search URL for a free-text query.
    Identical pattern the PWA uses today (diagnose/page.tsx)."""
    import urllib.parse
    return f"https://www.google.com/search?tbm=isch&q={urllib.parse.quote(query)}"
