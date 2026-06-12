"""Locale-aware reads of Cosh translation maps.

Single source of truth for "given a `cosh_core_items.translations`
dict and the caller's language code, return the right display name".

Replaces the duplicate `_resolve_*` / `_translation_en` helpers that
were scattered across `modules/diagnosis/router.py`, `modules/advisory/
router.py`, `services/sp_pg_crops_view.py`, `services/cosh_dus_view.py`,
and `services/pest_diagnosis_view.py`, plus the ~130 inline
`translations.get("en")` sites in the API routers.

Precedence rule (locked 2026-06-12 after the biological_names sync):

    user's language  →  English  →  caller-supplied fallback

The fallback is typically the row's `cosh_id` so the UI never silently
drops an entry that lacks an English entry — better to show a UUID
once and have Cosh fix it than to silently disappear a row.

Cosh upstream guarantees a stable shape for `translations`:

    {"en": "...", "hi": "...", "ta": "...", "kn": "...", ...}

Keys are 2-letter ISO codes. "English" was an older alias that the
legacy resolvers tolerated; the canonical key is "en". Any new key
showing up is just another language — no schema change needed.

See `docs/COSH_2_SYNC_CONTRACT.md` §6 for the upstream guarantees.
"""
from __future__ import annotations

from typing import Iterable, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.sync.models import CoshCoreItem


DEFAULT_LANG = "en"


def pick_translation(
    translations: Optional[dict],
    lang: str,
    fallback: str = "",
) -> str:
    """Resolve a Cosh translations dict to a display string.

    Precedence: `lang` → English → `fallback`. `fallback` is typically
    the row's cosh_id (so a bad row surfaces as a UUID rather than
    silently vanishing).

    Tolerates None / empty translations and the historical "English"
    alias key (which a couple of pre-2026-05 resolvers wrote alongside
    "en"). Empty-string values are treated as missing — falls through
    to the next level.
    """
    t = translations or {}
    return (
        t.get(lang)
        or t.get(DEFAULT_LANG)
        or t.get("English")
        or fallback
    )


async def resolve_names_by_cosh_id(
    db: AsyncSession,
    cosh_ids: Iterable[str],
    lang: str,
    *,
    core_type: Optional[str] = None,
    use_fallback_id: bool = False,
) -> dict[str, str]:
    """Bulk resolve a set of cosh_ids to localised display names.

    Returns `{cosh_id: name}` for every row that produced a non-empty
    name. Cosh_ids that don't exist as an ACTIVE row, or whose
    translations are empty, are absent — the caller decides what to
    show. Pass `use_fallback_id=True` to instead seed missing entries
    with the cosh_id itself; convenient when the caller would otherwise
    do `names.get(cid, cid)` at every render site.

    Pass `core_type` to narrow the lookup to one Core (e.g.
    "biological_names", "specific_problem"). Omit it to look across
    every Core — useful for "resolve whatever this cosh_id is" callers.
    """
    cosh_ids = {c for c in cosh_ids if c}
    if not cosh_ids:
        return {}
    q = select(CoshCoreItem).where(
        CoshCoreItem.cosh_id.in_(cosh_ids),
        CoshCoreItem.status == "active",
    )
    if core_type is not None:
        q = q.where(CoshCoreItem.core_type == core_type)
    rows = (await db.execute(q)).scalars().all()
    out: dict[str, str] = {}
    for r in rows:
        name = pick_translation(r.translations, lang, "")
        if name:
            out[r.cosh_id] = name
    if use_fallback_id:
        for cid in cosh_ids:
            out.setdefault(cid, cid)
    return out


async def resolve_name_for_cosh_id(
    db: AsyncSession,
    cosh_id: Optional[str],
    lang: str,
    *,
    core_type: Optional[str] = None,
) -> Optional[str]:
    """Single-row convenience over `resolve_names_by_cosh_id`. Returns
    None when the cosh_id is empty or unresolvable; callers feeding
    Claude prompts / farmer-facing copy must NOT pass a raw cosh_id
    forward in that case."""
    if not cosh_id:
        return None
    names = await resolve_names_by_cosh_id(
        db, {cosh_id}, lang, core_type=core_type,
    )
    return names.get(cosh_id)


def get_locale(
    current_user: User = Depends(get_current_user),
) -> str:
    """FastAPI dependency: returns the caller's language code, falling
    back to "en".

    Use in an endpoint signature instead of computing
    `current_user.language_code or "en"` inline. Lets routes that only
    need the locale skip importing User entirely.

        @router.get("/example")
        async def example(
            db: AsyncSession = Depends(get_db),
            lang: str = Depends(get_locale),
        ):
            names = await resolve_names_by_cosh_id(db, ids, lang)
            ...
    """
    return current_user.language_code or DEFAULT_LANG
