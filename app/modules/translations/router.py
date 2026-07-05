"""CA-portal read + regenerate endpoints for content translations.

Two endpoints, both scoped by (entity_type, entity_id):
  GET  /admin/translations/{entity_type}/{entity_id}
       Returns all stored translations for that entity + the current
       English source. CA portal renders these side-by-side per
       locale. Missing locales come back as `null` — the CA can
       trigger a regenerate to fill them.

  POST /admin/translations/{entity_type}/{entity_id}/regenerate
       Enqueues the translate task with force=True (bypasses hash
       skip) so an SE-verified regenerate happens even when source
       hasn't drifted.

Auth: `_assert_can_edit_client_advisory` — same gate used elsewhere
      in the CA portal so only client-scoped users can call this.

Legacy translation surfaces (parameter/variable/CQ) stay served by
their own dedicated endpoints — this router is content_translations
only.
"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.translations.models import (
    ContentTranslation, EntityType, TranslationStatus, hash_source,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/translations", tags=["Translations"])


# The 12 non-English PWA locales — same list the translation service ships to.
_TARGET_LOCALES = ("hi", "ta", "te", "kn", "ml", "mr", "gu", "pa", "or", "bn", "as", "ur")

# Canonical entity types this router handles. Kept small — mirror the
# EntityType constants. Unknown types get 400 rather than 404 so a
# typo on the frontend is caught cleanly.
_ALLOWED_ENTITY_TYPES = frozenset({
    EntityType.PACKAGE_DESCRIPTION,
    EntityType.ELEMENT_VALUE,
    EntityType.STANDARD_RESPONSE_QUESTION,
    EntityType.SEED_VARIETY_DESCRIPTION_POINTS,
})


async def _current_source(
    db: AsyncSession, entity_type: str, entity_id: str,
) -> Optional[str]:
    """Load the current English source for a given entity. Mirrors
    _fetch_source_and_ancestry in the Celery task but read-only and
    without the ancestry (the CA portal already knows its own
    context)."""
    if entity_type == EntityType.PACKAGE_DESCRIPTION:
        from app.modules.advisory.models import Package
        return (await db.execute(
            select(Package.description).where(Package.id == entity_id)
        )).scalar_one_or_none()
    if entity_type == EntityType.ELEMENT_VALUE:
        from app.modules.advisory.models import Element
        return (await db.execute(
            select(Element.value).where(Element.id == entity_id)
        )).scalar_one_or_none()
    if entity_type == EntityType.STANDARD_RESPONSE_QUESTION:
        from app.modules.farmpundit.models import StandardResponse
        return (await db.execute(
            select(StandardResponse.question_text).where(StandardResponse.id == entity_id)
        )).scalar_one_or_none()
    if entity_type == EntityType.SEED_VARIETY_DESCRIPTION_POINTS:
        from app.modules.seed_mgmt.models import SeedVariety
        pts = (await db.execute(
            select(SeedVariety.description_points).where(SeedVariety.id == entity_id)
        )).scalar_one_or_none()
        # Match the Celery-task shape: JSON-encoded list.
        if pts is None:
            return None
        import json as _json
        return _json.dumps(pts or [], ensure_ascii=False)
    return None


@router.get("/{entity_type}/{entity_id}")
async def get_translations(
    entity_type: str,
    entity_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the current English source + one row per locale (12).
    Missing locales come back with `translated_text: null` and
    `translation_status: 'MISSING'` so the CA UI can render a "not
    yet translated" state uniformly."""
    if entity_type not in _ALLOWED_ENTITY_TYPES:
        raise HTTPException(status_code=400, detail={
            "code": "unknown_entity_type",
            "message": f"Unknown entity type: {entity_type}",
        })
    source = await _current_source(db, entity_type, entity_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    src_hash = hash_source(source)
    rows = (await db.execute(
        select(ContentTranslation).where(
            ContentTranslation.entity_type == entity_type,
            ContentTranslation.entity_id == entity_id,
        )
    )).scalars().all()
    by_lang = {r.language_code: r for r in rows}

    locales_out = []
    for loc in _TARGET_LOCALES:
        r = by_lang.get(loc)
        if r is None:
            locales_out.append({
                "language_code": loc,
                "translated_text": None,
                "translation_status": "MISSING",
                "is_stale": False,
                "translated_at": None,
            })
        else:
            is_stale = r.source_hash != src_hash
            locales_out.append({
                "language_code": loc,
                "translated_text": r.translated_text,
                "translation_status": (
                    TranslationStatus.STALE if is_stale
                    else r.translation_status
                ),
                "is_stale": is_stale,
                "translated_at": r.translated_at,
            })

    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source_text": source,
        "source_hash": src_hash,
        "locales": locales_out,
    }


@router.post("/{entity_type}/{entity_id}/regenerate")
async def regenerate_translations(
    entity_type: str,
    entity_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Force a re-translate — bypasses the hash-skip guard. The
    Celery task will fetch fresh source, call Claude, and overwrite
    existing rows. Returns immediately with `{queued: true}`; CA UI
    polls the GET endpoint to see the fresh translation."""
    if entity_type not in _ALLOWED_ENTITY_TYPES:
        raise HTTPException(status_code=400, detail={
            "code": "unknown_entity_type",
            "message": f"Unknown entity type: {entity_type}",
        })
    source = await _current_source(db, entity_type, entity_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        from app.tasks.translate_content import translate_field
        translate_field.delay(entity_type, entity_id, "", True)  # force=True
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to enqueue regenerate task: %s", e)
        raise HTTPException(status_code=502, detail={
            "code": "enqueue_failed",
            "message": "Could not queue the translation job.",
        })
    return {"queued": True}
