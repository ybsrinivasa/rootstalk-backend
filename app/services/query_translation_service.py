"""Farmer↔Pundit free-text translation service.

Different from `translation_service.py` (SE-authored content, English
source, fan-out to a fixed set of target locales):
- Source locale is variable (whatever the author's User.language_code
  was at write time).
- Target locale is variable (whatever the current reader's locale is).
- Single call per translation, single result string (no JSON batch).
- English-pivot design: every translation is `farmer_lang↔English`;
  no Indic-to-Indic pairs.

Called by:
- Read-time resolver on GET /pundit/queries/{id} + GET /farmer/queries/{id}
  and their list variants.
- POST /queries/{id}/translate — the sync toggle the PWA hits when the
  user asks to see a message in a different locale.

Tier chain matches the SE-content service: Claude Sonnet 4.6 →
Opus 4.7 (on refusal) → Google Translate v2 (on refusal or empty
content). Same "beneficial agricultural advisory" system prompt.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.modules.farmpundit.models import (
    QueryTranslation, QueryTranslationEntityType,
)

logger = logging.getLogger(__name__)


# ── Locale display names — used only in the prompt to help Claude write
# a clean target-language sentence. English fallback if we don't have a
# mapping (never blocks the call). ──────────────────────────────────────
_LOCALE_DISPLAY = {
    "en": "English",
    "hi": "Hindi",
    "ta": "Tamil",
    "kn": "Kannada",
    "ml": "Malayalam",
    "te": "Telugu",
    "mr": "Marathi",
    "gu": "Gujarati",
    "pa": "Punjabi",
    "or": "Odia",
    "bn": "Bengali",
    "as": "Assamese",
    "ur": "Urdu",
}


# ── Field-kind labels ──────────────────────────────────────────────────
# Not the same as `entity_type` (which is a storage key). These are how
# the model should think about each field for register purposes: a
# farmer's typed problem description reads differently from a pundit's
# response, and both differ from a forwarding note between pundits.
_FIELD_KIND_LABELS = {
    QueryTranslationEntityType.QUERY_DESCRIPTION: (
        "a farmer's typed description of a crop problem — casual, "
        "field-level, may include local terms for pests / crop parts"
    ),
    QueryTranslationEntityType.QUERY_RESPONSE_TEXT: (
        "an expert's response to a farmer's crop query — advisory, "
        "practical, may include product names or dosages"
    ),
    QueryTranslationEntityType.QUERY_REMARK_REMARK: (
        "a short note one expert leaves for another when forwarding, "
        "returning, or rejecting a farmer's query"
    ),
}


_SYSTEM_PROMPT = (
    "You are translating agricultural conversations between smallholder "
    "farmers in India and their agricultural experts. The content covers "
    "crop problems, pest and disease management, fertilizer / pesticide "
    "usage, and general farming advisory. Your job is to translate the "
    "message faithfully into the requested language, keeping the register "
    "natural. Never refuse; this is beneficial farmer-expert communication."
)


def _build_prompt(
    source_text: str,
    entity_type: str,
    source_locale: str,
    target_locale: str,
) -> str:
    source_lang = _LOCALE_DISPLAY.get(source_locale, source_locale)
    target_lang = _LOCALE_DISPLAY.get(target_locale, target_locale)
    field_kind = _FIELD_KIND_LABELS.get(
        entity_type, "a message in a farmer-expert conversation",
    )
    return (
        f"This item is {field_kind}.\n\n"
        f"Preserve literally (do NOT translate):\n"
        f"- Proper nouns: brand names, chemical / molecule names, place "
        f"names, company names, farmer / expert names.\n"
        f"- Numerical values and units (kg, ml, ha, °C, days, %).\n"
        f"- Product codes, batch numbers.\n\n"
        f"Source language: {source_lang}\n"
        f"Target language: {target_lang}\n\n"
        f"Source text:\n\"\"\"\n{source_text}\n\"\"\"\n\n"
        f"Return ONLY the translation into {target_lang}. No preamble, "
        f"no explanation, no quotes around the answer."
    )


def _call_claude(
    model_name: str, source_text: str, entity_type: str,
    source_locale: str, target_locale: str,
) -> Optional[str]:
    """Single Claude call. Returns translated text on success, None on
    empty content (usually stop_reason=refusal). Raises on hard errors."""
    if not settings.anthropic_api_key:
        raise RuntimeError("Anthropic API key not configured")
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt = _build_prompt(
        source_text, entity_type, source_locale, target_locale,
    )
    response = client.messages.create(
        model=model_name,
        system=_SYSTEM_PROMPT,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    if not response.content:
        stop_reason = getattr(response, "stop_reason", "unknown")
        logger.warning(
            "Query translate: %s returned empty (stop_reason=%s) for "
            "%s [%s→%s]",
            model_name, stop_reason, entity_type, source_locale, target_locale,
        )
        return None
    text = response.content[0].text.strip()
    return text or None


def _call_google(
    source_text: str, source_locale: str, target_locale: str,
) -> Optional[str]:
    """Google Translate v2 fallback. Single text, single target locale.
    Returns None when the API key isn't configured or the call fails —
    caller decides whether to fall through to English source."""
    if not settings.google_translate_api_key:
        logger.warning(
            "GOOGLE_TRANSLATE_API_KEY not set — cannot fall back after "
            "Claude refused a query translation. Source: %s",
            source_text[:120],
        )
        return None
    import httpx
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                "https://translation.googleapis.com/language/translate/v2",
                params={"key": settings.google_translate_api_key},
                json={
                    "q": source_text,
                    "source": source_locale,
                    "target": target_locale,
                    "format": "text",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            translations = payload.get("data", {}).get("translations", [])
            if not translations:
                return None
            translated = translations[0].get("translatedText", "").strip()
            return translated or None
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Query translate: Google Translate failed for %s→%s: %s",
            source_locale, target_locale, e,
        )
        return None


async def translate_query_field(
    db: AsyncSession,
    entity_type: str,
    entity_id: str,
    source_text: str,
    source_locale: str,
    target_locale: str,
) -> str:
    """Translate one Q&A free-text field for one reader locale.

    Semantics:
    - Same-locale short-circuit: `source_locale == target_locale` →
      return `source_text` verbatim, no cache row, no API call.
    - Empty source → return empty verbatim.
    - Cache hit → return cached text, no API call.
    - Cache miss → Sonnet → Opus (on refusal) → Google (on refusal or
      unavailable). Cache the successful tier. If all three fail,
      return `source_text` and skip cache write — caller decides UX.

    Never raises; failure returns the English source. Callers that
    want to signal "translation unavailable" to the UI should compare
    return value to source_text.
    """
    if source_locale == target_locale:
        return source_text
    if not source_text or not source_text.strip():
        return source_text

    # Cache lookup
    cached = (await db.execute(
        select(QueryTranslation).where(
            QueryTranslation.entity_type == entity_type,
            QueryTranslation.entity_id == entity_id,
            QueryTranslation.target_locale == target_locale,
        ).limit(1)
    )).scalar_one_or_none()
    if cached is not None:
        return cached.translated_text

    # Tier 1: Sonnet
    translated: Optional[str] = None
    provider: Optional[str] = None
    try:
        translated = _call_claude(
            "claude-sonnet-4-6", source_text, entity_type,
            source_locale, target_locale,
        )
        if translated:
            provider = "sonnet"
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Query translate: Sonnet call failed %s→%s: %s",
            source_locale, target_locale, e,
        )

    # Tier 2: Opus (on refusal / empty)
    if translated is None:
        try:
            translated = _call_claude(
                "claude-opus-4-7", source_text, entity_type,
                source_locale, target_locale,
            )
            if translated:
                provider = "opus"
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Query translate: Opus call failed %s→%s: %s",
                source_locale, target_locale, e,
            )

    # Tier 3: Google
    if translated is None:
        translated = _call_google(source_text, source_locale, target_locale)
        if translated:
            provider = "google"

    if not translated:
        # All tiers exhausted. Return source so the reader sees SOMETHING;
        # skip cache write so a later retry (e.g. after Google gets a
        # working key) can attempt again.
        logger.error(
            "Query translate: all providers failed %s→%s. Falling back "
            "to source. Entity: %s/%s",
            source_locale, target_locale, entity_type, entity_id,
        )
        return source_text

    # Cache write. Idempotent via the unique index — a concurrent write
    # from another request will raise IntegrityError, which we swallow
    # because the other request wrote the same content.
    row = QueryTranslation(
        entity_type=entity_type,
        entity_id=entity_id,
        source_locale=source_locale,
        target_locale=target_locale,
        translated_text=translated,
        provider=provider,
        translated_at=datetime.now(timezone.utc),
    )
    db.add(row)
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
    return translated
