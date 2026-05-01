"""
Diagnosis API — BL-08 Crop Health Diagnosis Path.
Routes farmers through a dynamic symptom Q&A to identify the crop health problem.
Image analysis via Claude (claude-sonnet-4-6).
Problem descriptions via Claude — plain language, farmer-accessible.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Text, Boolean, JSON, DateTime, ForeignKey
from app.database import Base, get_db
from app.dependencies import get_current_user
from app.services.claude_service import analyze_crop_image, enrich_problem_with_description
from app.modules.platform.models import User
from app.modules.sync.models import CoshReferenceCache
from app.services.bl08_diagnosis_path import (
    run_diagnosis_step, get_available_plant_parts, get_problem_list,
    ProblemSymptomRow, DiagnosisAnswer,
)

router = APIRouter(tags=["Diagnosis"])

QUERY_EXPIRE_DAYS = 7


def utcnow():
    return datetime.now(timezone.utc)


def new_uuid():
    return str(uuid.uuid4())


# ── Diagnosis Session model (SQLAlchemy) ──────────────────────────────────────

class DiagnosisSession(Base):
    __tablename__ = "diagnosis_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ── Schema ────────────────────────────────────────────────────────────────────

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


# ── Helper: load symptom rows from Cosh cache ─────────────────────────────────

async def _load_problem_symptom_rows(
    db: AsyncSession,
    crop_stage_cosh_id: Optional[str],
) -> list[ProblemSymptomRow]:
    """Query cosh_reference_cache for problem_to_symptom entries."""
    q = select(CoshReferenceCache).where(
        CoshReferenceCache.entity_type == "problem_to_symptom",
        CoshReferenceCache.status == "active",
    )
    if crop_stage_cosh_id:
        # Filter by crop stage via metadata JSON field
        from sqlalchemy import cast, String as SAStr
        # PostgreSQL JSON path filter
        q = q.where(
            CoshReferenceCache.metadata_.op("->>")(  "crop_stage_cosh_id") == crop_stage_cosh_id
        )

    result = await db.execute(q)
    rows_raw = result.scalars().all()

    rows: list[ProblemSymptomRow] = []
    for r in rows_raw:
        m = r.metadata_ or {}
        if not m.get("problem_cosh_id") or not m.get("plant_part_cosh_id") or not m.get("symptom_cosh_id"):
            continue
        rows.append(ProblemSymptomRow(
            problem_cosh_id=m["problem_cosh_id"],
            plant_part_cosh_id=m["plant_part_cosh_id"],
            symptom_cosh_id=m["symptom_cosh_id"],
            sub_part_cosh_id=m.get("sub_part_cosh_id"),
            sub_symptom_cosh_id=m.get("sub_symptom_cosh_id"),
        ))
    return rows


def _get_display_name(entity_cosh_id: str, lang: str = "en") -> str:
    """Placeholder — in production, looked up from cosh_reference_cache translations."""
    return entity_cosh_id.replace("_", " ").title()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/diagnosis/plant-parts")
async def get_plant_parts_for_crop(
    crop_cosh_id: str,
    crop_stage_cosh_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Step 1: Get available plant parts for this crop+stage. Farmer selects one."""
    rows = await _load_problem_symptom_rows(db, crop_stage_cosh_id)
    parts = get_available_plant_parts(rows)
    return [
        {
            "cosh_id": p,
            "display_name": _get_display_name(p),
        }
        for p in parts
    ]


@router.post("/diagnosis/start", status_code=201)
async def start_diagnosis(
    request: StartDiagnosisRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Begin a new diagnosis session. Returns the first question."""
    rows = await _load_problem_symptom_rows(db, request.crop_stage_cosh_id)

    if not rows:
        return {
            "status": "NO_DATA",
            "message": "No diagnostic data available for this crop and stage yet. Please contact your company or ask an expert.",
        }

    # Run BL-08 to get first question
    step = run_diagnosis_step(rows, request.plant_part_cosh_id, answers=[])

    session = DiagnosisSession(
        subscription_id=request.subscription_id,
        farmer_user_id=current_user.id,
        crop_cosh_id=request.crop_cosh_id,
        crop_stage_cosh_id=request.crop_stage_cosh_id,
        initial_plant_part_cosh_id=request.plant_part_cosh_id,
        remaining_problem_ids=step.remaining_problem_ids,
        answers=[],
        has_yes_answer=False,
        status="ACTIVE",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    return {
        "session_id": session.id,
        "status": step.status,
        "remaining_count": step.remaining_count,
        "question": _format_question(step.question) if step.question else None,
        "diagnosed_problem_cosh_id": step.diagnosed_problem_cosh_id,
    }


@router.post("/diagnosis/{session_id}/answer")
async def answer_question(
    session_id: str,
    request: AnswerRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer answers YES or NO. Returns next question or diagnosis."""
    session = (await db.execute(
        select(DiagnosisSession).where(
            DiagnosisSession.id == session_id,
            DiagnosisSession.farmer_user_id == current_user.id,
            DiagnosisSession.status == "ACTIVE",
        )
    )).scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="Diagnosis session not found or already complete")

    if request.answer not in ("YES", "NO"):
        raise HTTPException(status_code=422, detail="answer must be 'YES' or 'NO'")

    # Reload all rows
    rows = await _load_problem_symptom_rows(db, session.crop_stage_cosh_id)

    # Append new answer
    new_answer = DiagnosisAnswer(
        plant_part_cosh_id=request.plant_part_cosh_id,
        symptom_cosh_id=request.symptom_cosh_id,
        sub_part_cosh_id=request.sub_part_cosh_id,
        sub_symptom_cosh_id=request.sub_symptom_cosh_id,
        answer=request.answer,
    )
    all_answers = [
        DiagnosisAnswer(**a) for a in session.answers
    ] + [new_answer]

    step = run_diagnosis_step(rows, session.initial_plant_part_cosh_id, all_answers)

    # Update session
    session.answers = [
        {
            "plant_part_cosh_id": a.plant_part_cosh_id,
            "symptom_cosh_id": a.symptom_cosh_id,
            "sub_part_cosh_id": a.sub_part_cosh_id,
            "sub_symptom_cosh_id": a.sub_symptom_cosh_id,
            "answer": a.answer,
        }
        for a in all_answers
    ]
    session.remaining_problem_ids = step.remaining_problem_ids
    session.has_yes_answer = step.has_yes_answer

    if step.status == "DIAGNOSED":
        session.status = "DIAGNOSED"
        session.diagnosed_problem_cosh_id = step.diagnosed_problem_cosh_id
        problem_info = await _get_problem_info(db, step.diagnosed_problem_cosh_id)
        # Enrich with Claude description (farmer-friendly 2 sentences)
        crop_name = _get_display_name(session.crop_cosh_id)
        problem_info = await enrich_problem_with_description(problem_info, crop_name)
    elif step.status == "INCONCLUSIVE":
        session.status = "ABORTED"
        problem_info = None
    else:
        problem_info = None

    await db.commit()

    return {
        "session_id": session_id,
        "status": step.status,
        "remaining_count": step.remaining_count,
        "question": _format_question(step.question) if step.question else None,
        "diagnosed_problem_cosh_id": step.diagnosed_problem_cosh_id,
        "problem_info": problem_info,
    }


@router.post("/diagnosis/{session_id}/abort")
async def abort_diagnosis(
    session_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Farmer aborts:
    - reason='DONT_KNOW' → redirects to FarmPundit query submission
    - reason='KNOW_PROBLEM' + problem_cosh_id → direct diagnosis
    """
    session = (await db.execute(
        select(DiagnosisSession).where(
            DiagnosisSession.id == session_id,
            DiagnosisSession.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    reason = data.get("reason")
    if reason == "KNOW_PROBLEM":
        problem_cosh_id = data.get("problem_cosh_id")
        if not problem_cosh_id:
            raise HTTPException(status_code=422, detail="problem_cosh_id required for KNOW_PROBLEM")
        session.status = "DIAGNOSED"
        session.diagnosed_problem_cosh_id = problem_cosh_id
        await db.commit()
        problem_info = await _get_problem_info(db, problem_cosh_id)
        crop_name = _get_display_name(session.crop_cosh_id)
        problem_info = await enrich_problem_with_description(problem_info, crop_name)
        return {"status": "DIAGNOSED", "diagnosed_problem_cosh_id": problem_cosh_id, "problem_info": problem_info}
    else:
        session.status = "ABORTED"
        await db.commit()
        return {
            "status": "ABORTED",
            "next_action": "QUERY",
            "subscription_id": session.subscription_id,
            "message": "Opening FarmPundit query form.",
        }


@router.get("/diagnosis/problems")
async def list_problems_for_crop(
    crop_cosh_id: str,
    crop_stage_cosh_id: Optional[str] = None,
    plant_part_cosh_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """'I Know the Problem' — returns problems filtered to crop+stage+part."""
    rows = await _load_problem_symptom_rows(db, crop_stage_cosh_id)
    problem_ids = get_problem_list(rows, plant_part=plant_part_cosh_id)

    result = []
    for pid in problem_ids:
        info = await _get_problem_info(db, pid)
        result.append(info)
    return result


# ── Claude Image Analysis ─────────────────────────────────────────────────────

class ImageAnalysisRequest(BaseModel):
    image_base64: str          # base64-encoded image (JPEG/PNG/WebP)
    media_type: str = "image/jpeg"
    crop_cosh_id: str
    crop_name: str
    plant_part_cosh_id: str
    plant_part_name: str
    crop_stage_cosh_id: Optional[str] = None
    language_code: str = "en"


@router.post("/diagnosis/image-analysis")
async def analyse_image_with_claude(
    request: ImageAnalysisRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Farmer uploads a photo of the affected crop part.
    Claude analyses the image and returns:
    - The most likely problem name
    - A matching Cosh problem_cosh_id (if available)
    - Confidence level (HIGH / MEDIUM / LOW)
    - 2-sentence farmer-friendly description of what it sees
    - List of observed symptoms

    The result can be used to pre-fill the diagnosis path or skip to direct diagnosis.
    """
    # Load known problems from Cosh cache for this crop+stage to help Claude match IDs
    rows = await _load_problem_symptom_rows(db, request.crop_stage_cosh_id)
    known_problem_ids = list(dict.fromkeys(r.problem_cosh_id for r in rows))

    known_problem_names: list[str] = []
    for pid in known_problem_ids[:20]:  # Limit to 20 to keep prompt manageable
        info = await _get_problem_info(db, pid)
        known_problem_names.append(info.get("name", pid))

    # Call Claude
    result = await analyze_crop_image(
        image_base64=request.image_base64,
        media_type=request.media_type,
        crop_name=request.crop_name,
        plant_part_name=request.plant_part_name,
        known_problem_ids=known_problem_ids[:20],
        known_problem_names=known_problem_names,
        language_code=request.language_code,
    )

    return {
        "analysis": result.to_dict(),
        "note": (
            "Claude identified a possible match — tap 'Confirm' to use this diagnosis, "
            "or 'Check with Questions' to verify via the guided path."
            if result.confidence in ("HIGH", "MEDIUM")
            else "Claude is not confident. Please use the guided YES/NO questions for a more accurate diagnosis."
        ),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_question(question):
    if not question:
        return None
    return {
        "plant_part_cosh_id": question.plant_part_cosh_id,
        "symptom_cosh_id": question.symptom_cosh_id,
        "sub_part_cosh_id": question.sub_part_cosh_id,
        "sub_symptom_cosh_id": question.sub_symptom_cosh_id,
        "question_type": question.question_type,
        "display_text": _build_question_text(question),
    }


def _build_question_text(question) -> str:
    """Build human-readable question text from the question structure."""
    part = _get_display_name(question.plant_part_cosh_id)
    symptom = _get_display_name(question.symptom_cosh_id)

    if question.sub_part_cosh_id and question.sub_symptom_cosh_id:
        sub_part = _get_display_name(question.sub_part_cosh_id)
        sub_symptom = _get_display_name(question.sub_symptom_cosh_id)
        return f"Is there {sub_symptom} on {sub_part} of the {part}?"
    elif question.sub_symptom_cosh_id:
        sub_symptom = _get_display_name(question.sub_symptom_cosh_id)
        return f"Do the {symptom} on the {part} look like: {sub_symptom}?"
    elif question.sub_part_cosh_id:
        sub_part = _get_display_name(question.sub_part_cosh_id)
        return f"Is the {symptom} on the {sub_part} of the {part}?"
    else:
        return f"Do you see {symptom} on the {part}?"


async def _get_problem_info(db: AsyncSession, problem_cosh_id: str) -> dict:
    """Get problem display info from Cosh cache."""
    sp = (await db.execute(
        select(CoshReferenceCache).where(
            CoshReferenceCache.cosh_id == problem_cosh_id,
            CoshReferenceCache.entity_type == "specific_problem",
        )
    )).scalar_one_or_none()

    if not sp:
        pg = (await db.execute(
            select(CoshReferenceCache).where(
                CoshReferenceCache.cosh_id == problem_cosh_id,
                CoshReferenceCache.entity_type == "problem_group",
            )
        )).scalar_one_or_none()
        if pg:
            return {
                "cosh_id": problem_cosh_id,
                "name": pg.translations.get("en", problem_cosh_id),
                "translations": pg.translations,
                "type": "problem_group",
                "parent_cosh_id": pg.parent_cosh_id,
            }

        return {"cosh_id": problem_cosh_id, "name": _get_display_name(problem_cosh_id), "type": "unknown"}

    return {
        "cosh_id": problem_cosh_id,
        "name": sp.translations.get("en", problem_cosh_id),
        "translations": sp.translations,
        "type": "specific_problem",
        "parent_cosh_id": sp.parent_cosh_id,  # problem_group_cosh_id
    }
