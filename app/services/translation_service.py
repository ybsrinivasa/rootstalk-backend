"""Claude-backed contextual translator for SE-authored content.

One Claude call translates a source string into all 12 target Indic
locales in a single batch — cheaper and more consistent than 12
separate calls (the model sees its own translations as reference).

Uses claude-sonnet-4-6 (same model as claude_service.py). Prompt
carries ancestry context so the register + terminology match:
crop name, field kind (title vs paragraph vs bullet), and any
additional notes the caller supplies (L0/L1/L2, product type, etc).

Persistence: writes rows to `content_translations`. Idempotent on
(entity_type, entity_id, field_path, language_code) — re-runs with
the same source hash are no-ops; source drift triggers a refresh.
"""
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.modules.translations.models import (
    ContentTranslation, TranslationStatus, hash_source,
)

logger = logging.getLogger(__name__)


# 2026-07-07 — Narrowed from 12 → 3 (hi, ta, kn) for the v1 prod
# push. Rationale: the PWA UI (app/scripts/translate-messages.mjs)
# still covers all 12 static-string locales, but SE-authored dynamic
# content is only exercised by the three farmer-language cohorts in
# active field use. Narrowing cuts Claude cost + backfill wall-time
# by ~4x.
#
# To add a locale later:
#   1. Append the language code here.
#   2. Push, rebuild the api/celery_worker image, restart the worker.
#   3. Re-run scripts/backfill_content_translations.py — the source-
#      hash check will skip already-covered entities; the new locale
#      will fan out as an additive translate.
TARGET_LOCALES = ("hi", "ta", "kn")


# Human-friendly labels used in prompt construction. Not the same as
# the internal `entity_type` string constants — those are storage keys;
# these are how the model should think about each field.
FIELD_KIND_LABELS = {
    "package.description": "the overall description of a crop advisory package",
    "element.value": "a piece of instructional content inside a farming practice",
    "standard_response.question_text": "the question a farmer would ask (used in a curated Q&A library)",
    "seed_variety.description_points": "bullet points describing a seed variety's qualities",
    "conditional_question.question_text": "a short yes/no question the farmer answers to steer the advisory (e.g. 'Has it rained in the last 2 days?')",
}


@dataclass
class AncestryContext:
    """Contextual signal for the translator. All fields optional —
    fill what you have; the prompt omits missing pieces cleanly.

    - `crop_name`: canonical English crop name ("Cotton", "Rice"). Sets
      terminology register; the model picks region-appropriate crop
      names in each target locale.
    - `product_type`: for QR-adjacent flows — SEED / PESTICIDE /
      FERTILISER. Not used yet in Phase T-2 (Package.description
      only); wired here so element-level translations in T-3 can
      pass it without a signature change.
    - `field_notes`: freeform extra hints from the caller. Kept
      short — "Element type: TITLE" or "Practice L1: SEED_TREATMENT".
      Rendered in the prompt as an extra "Additional context" line.
    """
    crop_name: Optional[str] = None
    product_type: Optional[str] = None
    field_notes: Optional[str] = None


def _google_translate_fallback(
    source_text: str,
    entity_type: str,
    locales: tuple[str, ...],
) -> dict[str, str]:
    """Google Translate v2 API — ultimate fallback when both Sonnet 4.6
    and Opus 4.7 refuse.

    Called for the ~4% of prod entities where Claude's pesticide-
    application guardrail false-positives ("Apply, Beauveria bassiana
    mix with jaggery 0.5%" and similar biological-control instructions).
    Google Translate has no such guardrail; it translates benign content
    without refusing.

    Quality is lower than Claude — no village-extension-worker register,
    no ancestry-aware terminology — but "clumsy Hindi" beats "raw
    English in the middle of a Hindi UI" as an outcome for the farmer.

    Returns {locale: translated_text} for every locale that resolved.
    Missing locales caller-side become the English source (existing
    fallback). Empty dict when the API key isn't configured OR every
    per-locale call failed.

    List-source entities (seed_variety.description_points) are handled
    by unpacking the JSON array, translating each bullet in one v2
    call per locale (v2 accepts multiple `q` values), then re-encoding
    as a JSON string for storage — the read-path decoder already
    handles that shape.
    """
    if not settings.google_translate_api_key:
        logger.warning(
            "Both Sonnet + Opus refused and GOOGLE_TRANSLATE_API_KEY "
            "is not configured — English fallback. Source: %s",
            source_text[:200],
        )
        return {}

    is_list_source = _is_list_source_entity(entity_type)
    if is_list_source:
        try:
            items = json.loads(source_text)
            if not isinstance(items, list):
                items = [source_text]
        except json.JSONDecodeError:
            items = [source_text]
    else:
        items = [source_text]

    import httpx
    out: dict[str, str] = {}
    with httpx.Client(timeout=15.0) as client:
        for locale in locales:
            try:
                resp = client.post(
                    "https://translation.googleapis.com/language/translate/v2",
                    params={"key": settings.google_translate_api_key},
                    json={
                        "q": items,
                        "source": "en",
                        "target": locale,
                        "format": "text",
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
                translated_items = [
                    t.get("translatedText", "")
                    for t in payload.get("data", {}).get("translations", [])
                ]
                if not translated_items or any(not t for t in translated_items):
                    logger.error(
                        "Google Translate returned empty for %s: %s",
                        locale, payload,
                    )
                    continue
                if is_list_source:
                    out[locale] = json.dumps(translated_items, ensure_ascii=False)
                else:
                    out[locale] = translated_items[0]
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "Google Translate failed for locale=%s: %s. Source: %s",
                    locale, e, source_text[:120],
                )
    if out:
        logger.info(
            "Google Translate fallback covered %d/%d locales for %s",
            len(out), len(locales), entity_type,
        )
    return out


def _is_list_source_entity(entity_type: str) -> bool:
    """Return True when the source for this entity type is a JSON
    array of strings (each element a bullet / sentence). Currently
    only `seed_variety.description_points`. Callers include the
    prompt builder (different response shape) and the response
    parser (list values → JSON-encoded string in storage)."""
    from app.modules.translations.models import EntityType
    return entity_type == EntityType.SEED_VARIETY_DESCRIPTION_POINTS


def _build_prompt(
    source_text: str,
    entity_type: str,
    ancestry: AncestryContext,
) -> str:
    field_kind = FIELD_KIND_LABELS.get(
        entity_type, "a piece of agricultural advisory content",
    )
    ancestry_lines = []
    if ancestry.crop_name:
        ancestry_lines.append(f"- Crop: {ancestry.crop_name}")
    if ancestry.product_type:
        ancestry_lines.append(f"- Product type: {ancestry.product_type}")
    if ancestry.field_notes:
        ancestry_lines.append(f"- Additional context: {ancestry.field_notes}")
    ancestry_block = "\n".join(ancestry_lines) if ancestry_lines else "- (no additional context available)"

    locales_json = json.dumps(list(TARGET_LOCALES))

    if _is_list_source_entity(entity_type):
        # 2026-07-06 — List-source path. Backfill caught Claude
        # returning `{"hi": "["item1", "item2"]", ...}` — inner
        # double-quotes not escaped, breaks the outer JSON. Instruct
        # the model to return the array as a proper nested list
        # (values are arrays of strings), which is trivially valid
        # JSON. Read-path re-encodes to a JSON string for storage.
        return f"""You are a professional agricultural translator specialising in crop advisory content for Indian farmers.

You translate content that reaches the farmer through a mobile app. The register should match how a village-level extension worker would speak to the farmer — practical, clear, imperative when the source is imperative, respectful. Do NOT explain, expand, or add caveats.

Preserve literally (do NOT translate):
- Proper nouns: brand names, chemical names, molecule names, place names, company names, farmer names.
- Numerical values and units (kg, ml, ha, °C, days, %).
- Product codes, batch numbers, formula tokens.
- English text inside quotes if the author quoted a brand or slogan.

This item is: {field_kind}. The English source is a JSON array of short bullet strings — translate each bullet, keeping the array shape and order.

Context:
{ancestry_block}

English source (JSON array):
{source_text}

Translate the source into each of these {len(TARGET_LOCALES)} Indian locales:
{locales_json}

Return ONLY a valid JSON object. Keys are the language codes above; each VALUE IS A JSON ARRAY OF STRINGS with the same number of elements as the English source, in the same order. Do not add explanations, notes, or any other keys.

Example shape (values shown for illustration only, respond with real translations):
{{"hi": ["translated_bullet_1", "translated_bullet_2"], "ta": ["…", "…"], "te": ["…", "…"], "kn": ["…", "…"], "ml": ["…", "…"], "mr": ["…", "…"], "gu": ["…", "…"], "pa": ["…", "…"], "or": ["…", "…"], "bn": ["…", "…"], "as": ["…", "…"], "ur": ["…", "…"]}}"""

    return f"""You are a professional agricultural translator specialising in crop advisory content for Indian farmers.

You translate content that reaches the farmer through a mobile app. The register should match how a village-level extension worker would speak to the farmer — practical, clear, imperative when the source is imperative, respectful. Do NOT explain, expand, or add caveats.

Preserve literally (do NOT translate):
- Proper nouns: brand names, chemical names, molecule names, place names, company names, farmer names.
- Numerical values and units (kg, ml, ha, °C, days, %).
- Product codes, batch numbers, formula tokens.
- English text inside quotes if the author quoted a brand or slogan.

This item is: {field_kind}

Context:
{ancestry_block}

English source:
\"\"\"
{source_text}
\"\"\"

Translate the source into each of these {len(TARGET_LOCALES)} Indian locales:
{locales_json}

Return ONLY a valid JSON object. Keys are the language codes above; values are the translated strings. Preserve line breaks and punctuation from the source. Do not add explanations, notes, or any other keys.

Example shape (values shown for illustration only, respond with real translations):
{{"hi": "…", "ta": "…", "te": "…", "kn": "…", "ml": "…", "mr": "…", "gu": "…", "pa": "…", "or": "…", "bn": "…", "as": "…", "ur": "…"}}"""


async def translate_content(
    source_text: str,
    entity_type: str,
    ancestry: AncestryContext,
) -> dict[str, str]:
    """Call Claude for a single source. Returns dict of
    {locale: translated} for every locale the model returned. Missing
    locales get retried by caller (mark those FAILED). Raises on
    hard errors (network / API 5xx / malformed response) — caller
    catches and marks the row FAILED for background retry.
    """
    if not settings.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY not set — cannot translate")
        raise RuntimeError("Anthropic API key not configured")

    import anthropic

    prompt = _build_prompt(source_text, entity_type, ancestry)
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    # 2026-07-07 — added a system frame after the prod backfill
    # surfaced ~4% straggler entities that all named beneficial
    # biological pest-control agents (Beauveria bassiana,
    # Metarhizium anisopliae, Bacillus thuringiensis, etc.) with
    # dosage instructions. Sonnet 4.6 was refusing them with an
    # empty content array — a false positive on pesticide-application
    # guardrails. The system frame gives Claude the correct context:
    # curated advisory content for smallholder farmers, safe to
    # translate as-is.
    system_prompt = (
        "You are translating agricultural advisory content for a mobile app "
        "used by smallholder farmers in India. The content is curated by "
        "domain experts (agronomists, plant pathologists, entomologists) "
        "and covers crop nutrition, pest and disease management, and "
        "farming best practices — including biological control agents, "
        "chemical inputs, and organic methods. Your job is to translate "
        "faithfully and accurately into the requested Indic languages. "
        "Never refuse; the content is beneficial extension advisory that "
        "helps farmers grow crops safely."
    )
    # Sync client called from an async context — anthropic's Python
    # SDK's sync client is safe here because we're inside a Celery
    # task worker thread. If invoked from an ASGI request path we'd
    # switch to AsyncAnthropic; kept sync to match claude_service.py.
    def _call(model_name: str):
        return client.messages.create(
            model=model_name,
            system=system_prompt,
            # 2026-07-06 — bumped from 2048 to 6144. Backfill caught
            # long-ish INSTRUCTIONS elements truncating: 12 locales of
            # translation output overflow 2048 output tokens for source
            # texts around 200+ chars, producing "Unterminated string"
            # JSON parse errors. 6144 gives comfortable headroom for
            # any real-world SE-authored bullet or instruction.
            max_tokens=6144,
            messages=[{"role": "user", "content": prompt}],
        )

    response = _call("claude-sonnet-4-6")
    if not response.content:
        # 2026-07-07 — Sonnet 4.6 false-positive refuses translations
        # of beneficial biocontrol instructions ("Apply, Beauveria
        # bassiana mix with jaggery 0.5%") on a pesticide-application
        # guardrail. Retry the same prompt against Opus 4.7 which has
        # a different guardrail profile. Only ~4% of translations hit
        # this path; cost impact is negligible.
        stop_reason = getattr(response, "stop_reason", "unknown")
        logger.warning(
            "Sonnet returned empty content (stop_reason=%s) for %s — retrying with Opus. Source: %s",
            stop_reason, entity_type, source_text[:200],
        )
        response = _call("claude-opus-4-7")
        if not response.content:
            # 2026-07-07 — Ultimate fallback: Google Translate v2.
            # Prod backfill showed Opus also refuses the same 94% of
            # what Sonnet refuses (biocontrol-with-dosage pattern is
            # a Claude-family guardrail, not model-specific). Google
            # has no such guardrail and translates benign content
            # cleanly. Quality is lower than Claude — no register /
            # ancestry — but "clumsy Hindi" beats "raw English" as
            # the farmer-facing outcome.
            stop_reason = getattr(response, "stop_reason", "unknown")
            logger.warning(
                "Both Sonnet and Opus refused %s (stop_reason=%s) — falling back to Google Translate. Source: %s",
                entity_type, stop_reason, source_text[:200],
            )
            google_out = _google_translate_fallback(
                source_text, entity_type, TARGET_LOCALES,
            )
            if google_out:
                return google_out
            logger.error(
                "Google Translate fallback also empty for %s. Farmer sees English.",
                entity_type,
            )
            raise RuntimeError(f"empty_content_all_providers stop_reason={stop_reason}")
    raw = response.content[0].text.strip()

    # Model sometimes wraps JSON in a code fence despite instructions.
    # Strip both variants defensively.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Claude translation returned invalid JSON: {raw[:200]}")
        raise RuntimeError(f"Translation response malformed: {e}") from e

    if not isinstance(parsed, dict):
        raise RuntimeError(f"Translation response is not a JSON object: {type(parsed)}")

    is_list_source = _is_list_source_entity(entity_type)
    out: dict[str, str] = {}
    for loc in TARGET_LOCALES:
        val = parsed.get(loc)
        if is_list_source:
            # Expect array of strings. Re-encode as JSON string for
            # storage — the read-path decoder in translation_reader.py
            # (`decode_seed_description_translation`) already knows
            # how to unwrap this shape.
            if isinstance(val, list) and val and all(isinstance(x, str) for x in val):
                out[loc] = json.dumps(val, ensure_ascii=False)
        else:
            if isinstance(val, str) and val.strip():
                out[loc] = val
    if not out:
        raise RuntimeError("Translation returned no usable locale values")
    return out


async def apply_translations(
    db: AsyncSession,
    entity_type: str,
    entity_id: str,
    field_path: str,
    source_text: str,
    translations: dict[str, str],
) -> int:
    """Upsert translations into `content_translations`. Same
    (entity_type, entity_id, field_path, language_code) row is
    updated in-place; missing rows are inserted. Returns count of
    rows written / updated.

    source_hash is computed from source_text and stored on every row
    so drift detection works: if the row's source_hash != current
    hash_source(source_text), the translation is stale and should be
    marked STALE / re-translated.
    """
    src_hash = hash_source(source_text)
    now = datetime.now(timezone.utc)
    written = 0
    for lang, text in translations.items():
        existing = (await db.execute(
            select(ContentTranslation).where(
                ContentTranslation.entity_type == entity_type,
                ContentTranslation.entity_id == entity_id,
                ContentTranslation.field_path == field_path,
                ContentTranslation.language_code == lang,
            )
        )).scalar_one_or_none()
        if existing is None:
            row = ContentTranslation(
                entity_type=entity_type,
                entity_id=entity_id,
                field_path=field_path,
                language_code=lang,
                translated_text=text,
                source_hash=src_hash,
                translation_status=TranslationStatus.APPROVED,
                translated_at=now,
            )
            db.add(row)
            written += 1
        else:
            existing.translated_text = text
            existing.source_hash = src_hash
            existing.translation_status = TranslationStatus.APPROVED
            existing.translated_at = now
            written += 1
    await db.commit()
    return written


async def translate_and_persist(
    db: AsyncSession,
    entity_type: str,
    entity_id: str,
    field_path: str,
    source_text: str,
    ancestry: AncestryContext,
    force: bool = False,
) -> Optional[int]:
    """End-to-end: check hash → skip if unchanged → call Claude →
    persist. Returns number of rows written, or None if skipped.

    `force=True` bypasses the hash check — useful for re-translate
    button in the CA portal review UI.
    """
    src_hash = hash_source(source_text)

    if not force:
        # Sample one row for this entity/field to see if source drifted.
        sample = (await db.execute(
            select(ContentTranslation).where(
                ContentTranslation.entity_type == entity_type,
                ContentTranslation.entity_id == entity_id,
                ContentTranslation.field_path == field_path,
            ).limit(1)
        )).scalar_one_or_none()
        if sample and sample.source_hash == src_hash and sample.translation_status == TranslationStatus.APPROVED:
            logger.debug(
                "Skipping translation — source unchanged: %s/%s/%s",
                entity_type, entity_id, field_path,
            )
            return None

    translations = await translate_content(source_text, entity_type, ancestry)
    return await apply_translations(
        db, entity_type, entity_id, field_path, source_text, translations,
    )
