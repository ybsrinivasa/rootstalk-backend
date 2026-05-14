"""Image resolution for the Diagnosis pipe (Batch 22, 2026-05-14).

Walks Cosh's two-step image chain to surface curated symptom images
per pest_diagnosis row, plus builds the context-aware Google Images
search query used at any Symptom Node where curated images are
absent (or the farmer wants to see more).

Resolution chain (see cosh_constants for slug pins):

  crop_cosh_id
    → image_index Connect (pos 1 = crop, pos 2 = symptom_image_slug Core)
    → symptom_image_slug Core item's translations.en (the slug string)
    → {crop}_symptom_images Connect rows (pos 1 = pest_diagnosis row)
    → image Core item (pos 2)'s metadata_.s3_path  (full HTTPS URL)

Read-through; no local mirror. Inactive Cosh items are filtered out
of all returned data.

The Google search query at the symptom node intentionally omits the
pest name — one Symptom Node typically maps to multiple candidate
pests, and biasing the search to one pest would skew the farmer's
visual matching. Per user 2026-05-14: format is `<Crop> <Part>
<Symptom>` (no subsymptom V1; we'll observe usage before promoting
that to subsymptom-level specificity).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_IMAGE_INDEX_CONNECT,
    COSH_SYMPTOM_IMAGE_SLUG_CORE,
    II_POS_CROP,
    II_POS_SLUG,
    SI_POS_DIAGNOSIS_ROW,
    SI_POS_IMAGE_CORE,
)


def _endpoint_at_position(row: CoshConnectRow, position: int) -> Optional[str]:
    for e in row.endpoints or []:
        if e.get("position") == position:
            return e.get("cosh_id")
    return None


# ── Step 1 of the chain: crop → image Connect slug ────────────────────────

async def image_connect_slug_for_crop(
    db: AsyncSession, crop_cosh_id: str,
) -> Optional[str]:
    """Walk image_index → resolve the symptom_image_slug Core item →
    return its English translation (the slug string of the per-crop
    image Connect). None when:
      - the crop has no image_index row yet (Cosh curators haven't
        wired images for this crop), or
      - the index row's slug Core is inactive / missing / has no
        usable English translation.
    """
    index_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_IMAGE_INDEX_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()
    slug_cosh_id: Optional[str] = None
    for r in index_rows:
        if _endpoint_at_position(r, II_POS_CROP) == crop_cosh_id:
            slug_cosh_id = _endpoint_at_position(r, II_POS_SLUG)
            break
    if slug_cosh_id is None:
        return None
    core = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == COSH_SYMPTOM_IMAGE_SLUG_CORE,
            CoshCoreItem.cosh_id == slug_cosh_id,
            CoshCoreItem.status == "active",
        )
    )).scalar_one_or_none()
    if core is None:
        return None
    t = core.translations or {}
    slug = t.get("en") or t.get("English")
    return slug if slug else None


# ── Step 2 of the chain: pest_diagnosis rows → image URLs ─────────────────

async def images_for_pest_diagnosis_rows(
    db: AsyncSession, *,
    crop_cosh_id: str,
    diag_row_ids: set[str],
) -> dict[str, list[str]]:
    """Return `{pest_diagnosis_row_id: [s3_url, ...]}` for each input
    id that has at least one curated image. Rows without images simply
    don't appear in the dict (caller treats absence as empty).

    Walks the per-crop image Connect (slug from
    `image_connect_slug_for_crop`), filters to rows whose pos-1
    diagnosis_row reference is in `diag_row_ids`, then batch-resolves
    each row's pos-2 image Core item to its `metadata_.s3_path`.

    Inactive Connect rows and inactive image Core items are silently
    skipped. Image Cores missing `metadata_.s3_path` are also skipped
    (degenerate Cosh data; logged at debug-level if we want later).

    URLs are deduped per-diagnosis-row, preserving first-seen order
    (stable thumbnail across calls).
    """
    if not diag_row_ids:
        return {}
    slug = await image_connect_slug_for_crop(db, crop_cosh_id)
    if slug is None:
        return {}

    image_rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == slug,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()

    # First pass: collect (diag_row_id, image_core_cosh_id) tuples for
    # the diag_row_ids we care about; gather image Core ids for batch
    # resolution.
    pairs: list[tuple[str, str]] = []
    image_core_ids: set[str] = set()
    for r in image_rows:
        diag = _endpoint_at_position(r, SI_POS_DIAGNOSIS_ROW)
        if diag not in diag_row_ids:
            continue
        img = _endpoint_at_position(r, SI_POS_IMAGE_CORE)
        if img is None:
            continue
        pairs.append((diag, img))
        image_core_ids.add(img)

    if not image_core_ids:
        return {}

    # Batch-resolve image Core items. We don't pin the core_type here
    # (it's per-crop, e.g. `images_tomato_symptoms_core`); the cosh_id
    # is globally unique enough in practice, and we filter by
    # status=active to drop retired images.
    cores = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id.in_(image_core_ids),
            CoshCoreItem.status == "active",
        )
    )).scalars().all()
    s3_by_core: dict[str, str] = {}
    for c in cores:
        meta = c.metadata_ or {}
        url = meta.get("s3_path")
        if isinstance(url, str) and url:
            s3_by_core[c.cosh_id] = url

    # Second pass: bucket URLs per diagnosis row, deduped, preserve order.
    out: dict[str, list[str]] = {}
    for diag, img in pairs:
        url = s3_by_core.get(img)
        if url is None:
            continue
        bucket = out.setdefault(diag, [])
        if url not in bucket:
            bucket.append(url)
    return out


# ── Google Images search query builder ────────────────────────────────────

def build_google_search_query(
    *, crop_name: Optional[str],
    plant_part_name: Optional[str],
    symptom_name: Optional[str],
) -> str:
    """Compose the context-aware search terms the farmer-PWA wraps
    into a Google Images URL.

    Format: `<Crop> <Part> <Symptom>`, with each token included only
    when the corresponding filter has been pinned AND its English
    name resolved. Pest name is intentionally absent (one Symptom
    Node fans out to many pests; pest-specific search would bias the
    farmer's visual matching).

    Returns an empty string when nothing resolved — frontend hides
    the button in that case rather than firing a useless search.
    """
    tokens: list[str] = []
    for name in (crop_name, plant_part_name, symptom_name):
        if name and name.strip():
            tokens.append(name.strip())
    return " ".join(tokens)
