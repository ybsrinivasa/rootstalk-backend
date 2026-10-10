"""Claude-driven auto-translation for Custom Parameter / Variable names
authored at CA scope (`/client/{client_id}/parameters` and children).

Separate from `translation_service.translate_and_persist` because
Parameter / Variable names live in the legacy per-domain tables
(`parameter_translations` / `variable_translations`) by design — see
`translations/models.py:7-9`. Everything above the storage layer
(Claude call, prompt building, fallback chain) is shared.

Behaviour (Option-2 rename policy agreed with user 2026-10-10):
  * Create → fire task with force=False → Claude writes APPROVED rows
    for every TARGET_LOCALE. Farmer PWA sees machine translations
    immediately.
  * Rename → downgrade existing rows to PENDING (keep text) in the
    rename handler itself. Task is NOT auto-fired on rename; SE
    reviews the stale rows in the CA portal and clicks Regenerate
    per row (force=True) when they want fresh Claude output.
  * Regenerate button → force=True → overwrite existing rows with
    fresh APPROVED Claude output.

Target locales = `translation_service.TARGET_LOCALES` (hi, ta, kn)
for consistency with every other content-translation entity type.
When product widens the coverage, one constant change here widens
everything simultaneously.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import (
    Parameter, ParameterTranslation,
    Variable, VariableTranslation,
    TranslationStatus,
)
from app.modules.translations.models import EntityType
from app.services.translation_ancestry import _resolve_crop_name
from app.services.translation_service import (
    AncestryContext, translate_content,
)

logger = logging.getLogger(__name__)


async def _upsert_parameter_translations(
    db: AsyncSession, parameter_id: str, translations: dict[str, str],
) -> int:
    """Upsert `parameter_translations` rows with status=APPROVED.
    Returns the number of rows written. Called from the Celery task
    after Claude returns; also used by the regenerate endpoint path."""
    now = datetime.now(timezone.utc)
    written = 0
    for lang, text in translations.items():
        existing = (await db.execute(
            select(ParameterTranslation).where(
                ParameterTranslation.parameter_id == parameter_id,
                ParameterTranslation.language_code == lang,
            )
        )).scalar_one_or_none()
        if existing is None:
            db.add(ParameterTranslation(
                parameter_id=parameter_id,
                language_code=lang,
                name=text,
                translation_status=TranslationStatus.APPROVED,
                approved_at=now,
            ))
        else:
            existing.name = text
            existing.translation_status = TranslationStatus.APPROVED
            existing.approved_at = now
        written += 1
    await db.commit()
    return written


async def _upsert_variable_translations(
    db: AsyncSession, variable_id: str, translations: dict[str, str],
) -> int:
    """Symmetric to `_upsert_parameter_translations` for Variables."""
    now = datetime.now(timezone.utc)
    written = 0
    for lang, text in translations.items():
        existing = (await db.execute(
            select(VariableTranslation).where(
                VariableTranslation.variable_id == variable_id,
                VariableTranslation.language_code == lang,
            )
        )).scalar_one_or_none()
        if existing is None:
            db.add(VariableTranslation(
                variable_id=variable_id,
                language_code=lang,
                name=text,
                translation_status=TranslationStatus.APPROVED,
                approved_at=now,
            ))
        else:
            existing.name = text
            existing.translation_status = TranslationStatus.APPROVED
            existing.approved_at = now
        written += 1
    await db.commit()
    return written


async def translate_parameter(
    db: AsyncSession, parameter_id: str,
) -> Optional[int]:
    """Fetch the Parameter's English name + crop ancestry, call Claude,
    upsert translation rows. Returns rows written, or None on skip."""
    param = (await db.execute(
        select(Parameter).where(Parameter.id == parameter_id)
    )).scalar_one_or_none()
    if param is None:
        logger.warning("translate_parameter: %s not found", parameter_id)
        return None
    if not param.name or not param.name.strip():
        return None
    crop_name = await _resolve_crop_name(db, param.crop_cosh_id)
    ancestry = AncestryContext(crop_name=crop_name)
    translations = await translate_content(
        param.name, EntityType.PARAMETER_NAME, ancestry,
    )
    return await _upsert_parameter_translations(db, parameter_id, translations)


async def translate_variable(
    db: AsyncSession, variable_id: str,
) -> Optional[int]:
    """Fetch the Variable's English name + parent Parameter name + crop
    ancestry, call Claude, upsert translation rows."""
    var = (await db.execute(
        select(Variable).where(Variable.id == variable_id)
    )).scalar_one_or_none()
    if var is None:
        logger.warning("translate_variable: %s not found", variable_id)
        return None
    if not var.name or not var.name.strip():
        return None
    parent = (await db.execute(
        select(Parameter).where(Parameter.id == var.parameter_id)
    )).scalar_one_or_none()
    crop_name = (
        await _resolve_crop_name(db, parent.crop_cosh_id) if parent else None
    )
    field_notes = (
        f"Parent parameter: {parent.name}" if parent and parent.name else None
    )
    ancestry = AncestryContext(crop_name=crop_name, field_notes=field_notes)
    translations = await translate_content(
        var.name, EntityType.VARIABLE_NAME, ancestry,
    )
    return await _upsert_variable_translations(db, variable_id, translations)
