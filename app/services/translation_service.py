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


# 12 non-English PWA locales. Matches app/scripts/translate-messages.mjs.
TARGET_LOCALES = ("hi", "ta", "te", "kn", "ml", "mr", "gu", "pa", "or", "bn", "as", "ur")


# Human-friendly labels used in prompt construction. Not the same as
# the internal `entity_type` string constants — those are storage keys;
# these are how the model should think about each field.
FIELD_KIND_LABELS = {
    "package.description": "the overall description of a crop advisory package",
    "element.value": "a piece of instructional content inside a farming practice",
    "standard_response.question_text": "the question a farmer would ask (used in a curated Q&A library)",
    "seed_variety.description_points": "bullet points describing a seed variety's qualities",
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
    # Sync client called from an async context — anthropic's Python
    # SDK's sync client is safe here because we're inside a Celery
    # task worker thread. If invoked from an ASGI request path we'd
    # switch to AsyncAnthropic; kept sync to match claude_service.py.
    response = client.messages.create(
        model="claude-sonnet-4-6",
        # 2026-07-06 — bumped from 2048 to 6144. Backfill caught
        # long-ish INSTRUCTIONS elements truncating: 12 locales of
        # translation output overflow 2048 output tokens for source
        # texts around 200+ chars, producing "Unterminated string"
        # JSON parse errors. 6144 gives comfortable headroom for
        # any real-world SE-authored bullet or instruction.
        max_tokens=6144,
        messages=[{"role": "user", "content": prompt}],
    )
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
