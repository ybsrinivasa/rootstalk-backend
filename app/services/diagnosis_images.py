"""
Reference Image Lookup for Diagnosis Questions

Given the current diagnosis question's filter (crop + part + symptom +
optional sub_part / sub_symptom), walk Cosh's typed graph to surface
the reference images Cosh has curated for matching scenarios. Used by
the PWA to show "this is what the symptom looks like" carousel during
self-diagnosis.

Cosh data shape (per docs/COSH_2_SYNC_CONTRACT.md):

  • `pest_diagnosis_chain` Connect rows carry endpoints with roles
    crop / crop_stage / pest / pest_stage / part / sub_part / symptom /
    sub_symptom.

  • `<crop>_pest_images` Connect rows (one per crop) link a single
    `pest_diagnosis_chain` row to a single `media` Core item.

  • `media` Core items carry the asset URL in `metadata.s3_path` (or
    `metadata.url`) and the type in `metadata.media_type`.

Lookup:
  1. Filter `pest_diagnosis_chain` rows by the question.
  2. For each matching row's cosh_id, find any other Connect row whose
     endpoints contain {role: 'pest_diagnosis_chain', cosh_id: <row_id>}
     AND a {role: 'media', cosh_id: <media_id>} entry.
  3. Look up each `media` Core item, pull the S3 path + caption.
  4. Return de-duplicated.

Result is intentionally connect_type-agnostic — any image Connect type
(`tomato_pest_images`, `paddy_pest_images`, future per-crop ones) works
without code change.

Fallback when zero images are found is the caller's job — this service
just returns `[]`. The endpoint layer adds the Google-Images URL.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshConnectRow, CoshCoreItem


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

    # 1. Find pest_diagnosis_chain rows that match this question.
    diagnosis_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == "pest_diagnosis_chain",
            CoshConnectRow.status == "active",
        )
    )).scalars().all()

    matching_diagnosis_ids: set[str] = set()
    for r in diagnosis_rows:
        eps = {ep["role"]: ep["cosh_id"] for ep in (r.endpoints or [])
               if ep.get("role") and ep.get("cosh_id")}
        if eps.get("crop") != crop_cosh_id:
            continue
        if crop_stage_cosh_id and eps.get("crop_stage") != crop_stage_cosh_id:
            continue
        if eps.get("part") != part_cosh_id:
            continue
        if eps.get("symptom") != symptom_cosh_id:
            continue
        if sub_part_cosh_id and eps.get("sub_part") != sub_part_cosh_id:
            continue
        if sub_symptom_cosh_id and eps.get("sub_symptom") != sub_symptom_cosh_id:
            continue
        matching_diagnosis_ids.add(r.connect_id)

    if not matching_diagnosis_ids:
        return []

    # 2. Find image-link Connect rows pointing to any matching diagnosis row.
    # We don't filter by connect_type — any Connect (tomato_pest_images,
    # paddy_pest_images, future per-crop ones) qualifies as long as its
    # endpoints carry the right role pair.
    all_active = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.status == "active")
    )).scalars().all()

    media_ids: set[str] = set()
    for r in all_active:
        if r.connect_type == "pest_diagnosis_chain":
            continue
        eps = {ep["role"]: ep["cosh_id"] for ep in (r.endpoints or [])
               if ep.get("role") and ep.get("cosh_id")}
        diag_id = eps.get("pest_diagnosis_chain")
        media_id = eps.get("media")
        if diag_id in matching_diagnosis_ids and media_id:
            media_ids.add(media_id)

    if not media_ids:
        return []

    # 3. Fetch Media Core items.
    media_items = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id.in_(media_ids),
            CoshCoreItem.core_type == "media",
            CoshCoreItem.status == "active",
        )
    )).scalars().all()

    out: list[ReferenceImage] = []
    for m in media_items:
        meta = m.metadata_ or {}
        translations = m.translations or {}
        out.append(ReferenceImage(
            cosh_id=m.cosh_id,
            url=meta.get("s3_path") or meta.get("url"),
            media_type=meta.get("media_type", "image"),
            caption=translations.get(language_code) or translations.get("en"),
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
        translations = row.translations or {}
        name = translations.get(language_code) or translations.get("en")
        if name:
            parts.append(name)
    return " ".join(parts).strip()


def google_images_url(query: str) -> str:
    """Build the Google Images search URL for a free-text query.
    Identical pattern the PWA uses today (diagnose/page.tsx)."""
    import urllib.parse
    return f"https://www.google.com/search?tbm=isch&q={urllib.parse.quote(query)}"
