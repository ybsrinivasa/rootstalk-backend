"""
RootsTalk seed script — run once after first migration.
Creates: 13 languages, SA account.
Safe to re-run — idempotent.
"""
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select
from app.config import settings
from app.database import Base
from app.modules.platform.models import User, UserRole, EnabledLanguage, RoleType, StatusEnum
from app.modules.auth.service import hash_password

engine = create_async_engine(settings.database_url)
AsyncSession = async_sessionmaker(engine, expire_on_commit=False)

LANGUAGES = [
    ("en",  "English",    "English",         "LTR", StatusEnum.ACTIVE),
    ("hi",  "Hindi",      "हिन्दी",           "LTR", StatusEnum.INACTIVE),
    ("ta",  "Tamil",      "தமிழ்",            "LTR", StatusEnum.INACTIVE),
    ("te",  "Telugu",     "తెలుగు",           "LTR", StatusEnum.INACTIVE),
    ("kn",  "Kannada",    "ಕನ್ನಡ",           "LTR", StatusEnum.INACTIVE),
    ("ml",  "Malayalam",  "മലയാളം",          "LTR", StatusEnum.INACTIVE),
    ("mr",  "Marathi",    "मराठी",            "LTR", StatusEnum.INACTIVE),
    ("gu",  "Gujarati",   "ગુજરાતી",          "LTR", StatusEnum.INACTIVE),
    ("pa",  "Punjabi",    "ਪੰਜਾਬੀ",           "LTR", StatusEnum.INACTIVE),
    ("or",  "Odia",       "ଓଡ଼ିଆ",            "LTR", StatusEnum.INACTIVE),
    ("bn",  "Bengali",    "বাংলা",            "LTR", StatusEnum.INACTIVE),
    ("as",  "Assamese",   "অসমীয়া",          "LTR", StatusEnum.INACTIVE),
    ("ur",  "Urdu",       "اردو",             "RTL", StatusEnum.INACTIVE),
]


async def seed():
    async with AsyncSession() as db:
        # Languages
        for code, name_en, name_native, direction, initial_status in LANGUAGES:
            existing = (await db.execute(
                select(EnabledLanguage).where(EnabledLanguage.language_code == code)
            )).scalar_one_or_none()
            if not existing:
                db.add(EnabledLanguage(
                    language_code=code,
                    language_name_en=name_en,
                    language_name_native=name_native,
                    script_direction=direction,
                    status=initial_status,
                ))
                print(f"  Added language: {name_en}")

        await db.flush()

        # Super Admin user
        sa = (await db.execute(
            select(User).where(User.email == settings.sa_email)
        )).scalar_one_or_none()

        if not sa:
            sa = User(
                email=settings.sa_email,
                name="Super Admin",
                password_hash=hash_password(settings.sa_password),
                language_code="en",
            )
            db.add(sa)
            await db.flush()
            db.add(UserRole(
                user_id=sa.id,
                role_type=RoleType.CONTENT_MANAGER,
                status=StatusEnum.ACTIVE,
            ))
            print(f"  Created SA: {settings.sa_email}")

        await db.commit()
        print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
