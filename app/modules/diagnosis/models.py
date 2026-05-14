"""Diagnosis-side ORM models.

The farmer's diagnosis session — created when `POST /diagnosis/start`
returns the first BL-08 question, updated on each `POST
/diagnosis/{id}/answer`, terminated by DIAGNOSED / ABORTED. The
session is keyed to a `Subscription` (so each subscribed crop's
diagnoses stay isolated) and the `farmer_user_id` (so a malicious
caller can't bind to another farmer's subscription).

The companion `diagnosis_sessions` table was created by alembic
migration `b9e7d4a3c1f8_diagnosis_sessions.py`. This file just
re-homes the model out of the router file it was originally defined
in (under `farmpundit/diagnosis_router.py`); no schema change.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class DiagnosisSession(Base):
    __tablename__ = "diagnosis_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), nullable=False)
    farmer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    crop_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    crop_stage_cosh_id: Mapped[str] = mapped_column(String(100), nullable=True)
    initial_plant_part_cosh_id: Mapped[str] = mapped_column(String(100), nullable=False)
    remaining_problem_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    answers: Mapped[list] = mapped_column(JSON, nullable=False)
    has_yes_answer: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    diagnosed_problem_cosh_id: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
