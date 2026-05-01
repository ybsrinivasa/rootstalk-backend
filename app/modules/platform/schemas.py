from pydantic import BaseModel
from typing import Optional
from app.modules.platform.models import StatusEnum


class LanguageOut(BaseModel):
    id: str
    language_code: str
    language_name_en: str
    language_name_native: str
    script_direction: str
    status: StatusEnum

    class Config:
        from_attributes = True


class LanguageToggle(BaseModel):
    status: StatusEnum
