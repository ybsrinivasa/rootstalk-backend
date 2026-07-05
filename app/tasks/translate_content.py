"""Celery task: translate an SE-authored field into the 12 target locales.

Enqueued on SE save (Phase T-3 hooks it up per surface). For Phase T-2
the task is invocable via `.delay(entity_type, entity_id)` or via the
backfill script; wiring into save endpoints comes next.

Failure handling: caught exceptions are logged; the task doesn't
retry itself yet — background sweep task (T-2b, follow-up) will
find translations with status FAILED / STALE and re-enqueue. Keeping
the immediate path simple.
"""
import asyncio
import json
import logging

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.celery_app import celery_app
from app.services.translation_service import (
    AncestryContext, translate_and_persist,
)
from app.services.translation_ancestry import (
    build_ancestry_for_package_description,
    build_ancestry_for_seed_variety_description,
)
from app.modules.translations.models import EntityType

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.translate_content.translate_field")
def translate_field(entity_type: str, entity_id: str, field_path: str = "") -> dict:
    """Translate one (entity_type, entity_id, field_path) into all 12
    target locales. Fetches the current English source from the row,
    builds ancestry, calls Claude, persists.

    Returns {"written": N} on success, {"skipped": true} when hash
    unchanged, {"error": "..."} on failure.
    """
    return asyncio.run(_run(entity_type, entity_id, field_path))


async def _run(entity_type: str, entity_id: str, field_path: str) -> dict:
    async with AsyncSessionLocal() as db:
        source_text, ancestry = await _fetch_source_and_ancestry(
            db, entity_type, entity_id, field_path,
        )
        if source_text is None:
            return {"error": "source_not_found"}
        if not source_text.strip():
            return {"skipped": True, "reason": "empty_source"}
        try:
            written = await translate_and_persist(
                db, entity_type, entity_id, field_path, source_text, ancestry,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "Translation failed for %s/%s/%s: %s",
                entity_type, entity_id, field_path, e,
            )
            return {"error": str(e)}
        if written is None:
            return {"skipped": True, "reason": "source_unchanged"}
        return {"written": written}


async def _fetch_source_and_ancestry(
    db, entity_type: str, entity_id: str, field_path: str,
) -> tuple[str | None, AncestryContext]:
    """Look up the English source string + build ancestry per entity type.
    Add a branch here for each new entity we onboard.
    """
    if entity_type == EntityType.PACKAGE_DESCRIPTION:
        from app.modules.advisory.models import Package
        pkg = (await db.execute(
            select(Package.description).where(Package.id == entity_id)
        )).scalar_one_or_none()
        ancestry = await build_ancestry_for_package_description(db, entity_id)
        return pkg, ancestry

    if entity_type == EntityType.SEED_VARIETY_DESCRIPTION_POINTS:
        # JSONB list. We translate the whole list as one payload —
        # serialise to JSON, Claude preserves structure, we deserialise
        # on the read path. Cheaper than one call per bullet.
        from app.modules.seed_mgmt.models import SeedVariety
        row = (await db.execute(
            select(SeedVariety.description_points).where(SeedVariety.id == entity_id)
        )).scalar_one_or_none()
        if row is None:
            return None, AncestryContext()
        items = row or []
        if not items:
            return "", await build_ancestry_for_seed_variety_description(db, entity_id)
        source_json = json.dumps(items, ensure_ascii=False)
        ancestry = await build_ancestry_for_seed_variety_description(db, entity_id)
        return source_json, ancestry

    # T-3 additions land here (element.value, standard_response.question_text)
    return None, AncestryContext()
