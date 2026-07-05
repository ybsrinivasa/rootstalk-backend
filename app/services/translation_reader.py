"""Read-side helpers for SE-authored content translations.

Every farmer-facing endpoint that surfaces Package.description,
Element.value, StandardResponse.question_text, or
SeedVariety.description_points calls one of these helpers to
localise the payload before returning.

Design:
- Batched lookups only. A page render can produce dozens of
  translatable entities; loading them one-by-one would defeat the
  purpose. Callers gather (entity_type, entity_id) pairs, hand them
  to `resolve_translations_batch(...)`, get back a dict keyed by
  (entity_type, entity_id) with the localised text or None.
- English fallback happens at the caller. When the resolver returns
  None (no row, or empty translation), the caller uses the source
  English value it already has.
- Locale comes from `user.language_code`. "en" callers short-circuit
  — no lookup needed, English source wins.
"""
import json
import logging
from typing import Iterable
from sqlalchemy import select, and_, or_, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.translations.models import (
    ContentTranslation, TranslationStatus,
)

logger = logging.getLogger(__name__)


async def resolve_translations_batch(
    db: AsyncSession,
    language_code: str,
    entities: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], str | None]:
    """Load the user's locale translation for each (entity_type,
    entity_id) tuple. Returns a dict of the same keys → localised
    text (or None when no translation exists).

    APPROVED rows only. PENDING / FAILED / STALE rows are ignored
    so the farmer doesn't see a half-baked machine output. STALE
    happens transiently while a re-translate is queued.

    Callers dedupe their input list — we don't dedupe here.
    """
    if language_code == "en":
        return {}

    entities_list = list(entities)
    if not entities_list:
        return {}

    # (entity_type, entity_id, field_path) tuple IN lookup would need
    # tuple_(...).in_(...) which asyncpg doesn't cleanly support with
    # composite tuples. Two-step filter instead — narrow by type +
    # id sets then post-filter by exact pair. Batched still.
    entity_types = {t for t, _ in entities_list}
    entity_ids = {i for _, i in entities_list}

    rows = (await db.execute(
        select(
            ContentTranslation.entity_type,
            ContentTranslation.entity_id,
            ContentTranslation.translated_text,
        ).where(
            ContentTranslation.entity_type.in_(entity_types),
            ContentTranslation.entity_id.in_(entity_ids),
            ContentTranslation.language_code == language_code,
            ContentTranslation.translation_status == TranslationStatus.APPROVED,
            # field_path is either empty (scalar) or something we
            # don't use yet — filter to the empty-string default to
            # avoid picking up future sub-field rows accidentally.
            ContentTranslation.field_path == "",
        )
    )).all()

    seen_pairs = set(entities_list)
    out: dict[tuple[str, str], str | None] = {p: None for p in entities_list}
    for etype, eid, text in rows:
        key = (etype, eid)
        if key in seen_pairs:
            out[key] = text
    return out


def decode_seed_description_translation(translated: str | None) -> list[str] | None:
    """SeedVariety.description_points is stored as a JSON-encoded list
    in the translation payload (matches the source shape — see
    tasks/translate_content.py). Decode back to a list; return None
    on missing or malformed input so the caller can fall back to the
    English bullets."""
    if not translated:
        return None
    try:
        decoded = json.loads(translated)
    except json.JSONDecodeError:
        logger.warning("Malformed seed_variety translation payload: %s", translated[:100])
        return None
    if not isinstance(decoded, list):
        return None
    return [str(x) for x in decoded]
