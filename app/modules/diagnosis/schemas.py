"""Pydantic request schemas for the diagnosis pipe.

Re-homed from `app/modules/farmpundit/diagnosis_router.py` (Batch 23,
2026-05-14). The router file imports + re-exports them so existing
test imports (`from app.modules.diagnosis.router import AnswerRequest`,
etc.) continue to resolve.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class StartDiagnosisRequest(BaseModel):
    subscription_id: str
    crop_cosh_id: str
    crop_stage_cosh_id: Optional[str] = None
    plant_part_cosh_id: str


class AnswerRequest(BaseModel):
    plant_part_cosh_id: str
    symptom_cosh_id: str
    sub_part_cosh_id: Optional[str] = None
    sub_symptom_cosh_id: Optional[str] = None
    answer: str  # "YES" | "NO"


class ImageAnalysisRequest(BaseModel):
    image_base64: str            # base64-encoded image (JPEG/PNG/WebP)
    media_type: str = "image/jpeg"
    crop_cosh_id: str
    crop_name: str
    plant_part_cosh_id: str
    plant_part_name: str
    crop_stage_cosh_id: Optional[str] = None
    language_code: str = "en"


class ExplainSymptomRequest(BaseModel):
    crop_cosh_id: str
    plant_part_cosh_id: str
    symptom_cosh_id: str
    sub_part_cosh_id: Optional[str] = None
    sub_symptom_cosh_id: Optional[str] = None
    language_code: str = "en"
    language_name: str = "English"


class AIDirectImage(BaseModel):
    base64: str
    media_type: str = "image/jpeg"


class AIDirectDiagnoseRequest(BaseModel):
    subscription_id: str
    crop_cosh_id: str
    crop_stage_cosh_id: Optional[str] = None
    images: list[AIDirectImage]
    language_code: str = "en"


class ReferenceImagesRequest(BaseModel):
    crop_cosh_id: str
    crop_stage_cosh_id: Optional[str] = None
    plant_part_cosh_id: str
    symptom_cosh_id: str
    sub_part_cosh_id: Optional[str] = None
    sub_symptom_cosh_id: Optional[str] = None
    language_code: str = "en"
