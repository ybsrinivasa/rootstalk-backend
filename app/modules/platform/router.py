from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.modules.platform.models import EnabledLanguage, StatusEnum
from app.modules.platform.schemas import LanguageOut, LanguageToggle

router = APIRouter(tags=["Platform"])


@router.get("/platform/languages", response_model=list[LanguageOut])
async def list_languages(db: AsyncSession = Depends(get_db)):
    """Return all enabled languages. Used by PWA to populate language selector."""
    result = await db.execute(select(EnabledLanguage).order_by(EnabledLanguage.language_name_en))
    return result.scalars().all()


@router.put("/platform/languages/{code}/status", response_model=LanguageOut)
async def toggle_language(
    code: str,
    request: LanguageToggle,
    db: AsyncSession = Depends(get_db),
    # SA only — auth enforced at main.py level via middleware (to be added)
):
    """SA: enable or disable a language. English cannot be disabled."""
    result = await db.execute(
        select(EnabledLanguage).where(EnabledLanguage.language_code == code)
    )
    lang = result.scalar_one_or_none()
    if not lang:
        raise HTTPException(status_code=404, detail="Language not found")
    if code == "en":
        raise HTTPException(status_code=400, detail="English cannot be disabled")

    lang.status = request.status
    await db.commit()
    await db.refresh(lang)
    return lang
