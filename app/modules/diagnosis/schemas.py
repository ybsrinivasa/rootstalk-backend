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
    # Set to True by the PWA when answering a CONFIRMATION question
    # (BL-08 §8, amended 2026-05-28). The router branches early on
    # this flag: YES flips the session to DIAGNOSED on the candidate;
    # NO flips it to OUTSIDE_LIST. Server-side check: True is only
    # honoured when the session's remaining pool actually has 1
    # candidate, so a stray flag can't shortcut the algorithm.
    is_confirmation: bool = False


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


class ImageCheckSymptomRequest(BaseModel):
    """In-loop AI decision support for the current Yes/No question.
    Locale derives from current_user (request-body language fields are
    ignored)."""
    image_base64: str
    media_type: str = "image/jpeg"
    crop_cosh_id: str
    plant_part_cosh_id: str
    symptom_cosh_id: str
    sub_part_cosh_id: Optional[str] = None
    sub_symptom_cosh_id: Optional[str] = None


class AIDirectImage(BaseModel):
    base64: str
    media_type: str = "image/jpeg"


class AIDirectDiagnoseRequest(BaseModel):
    subscription_id: str
    crop_cosh_id: str
    crop_stage_cosh_id: Optional[str] = None
    images: list[AIDirectImage]
    language_code: str = "en"


class CommitToAdvisoryRequest(BaseModel):
    """Body of POST /diagnosis/{session_id}/commit-to-advisory.

    `affected_plants_count` is mandatory for plant-wise crops and
    ignored (allowed but unused) for area-wise crops. The PWA's
    plant-wise diagnose flow gates the commit CTA on a valid integer
    entry; this server-side validation is the second wall.
    """
    affected_plants_count: Optional[int] = None


class ReferenceImagesRequest(BaseModel):
    crop_cosh_id: str
    crop_stage_cosh_id: Optional[str] = None
    plant_part_cosh_id: str
    symptom_cosh_id: str
    sub_part_cosh_id: Optional[str] = None
    sub_symptom_cosh_id: Optional[str] = None
    language_code: str = "en"
