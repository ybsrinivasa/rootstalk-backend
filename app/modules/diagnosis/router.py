"""Diagnosis API — canonical home for all `/diagnosis/*` endpoints.

Two layers of functionality co-located here as of Batch 23 (2026-05-14):

  • Legacy BL-08 session-based path (originally in
    `farmpundit/diagnosis_router.py` — wrong module, moved here):
    `/start`, `/{id}/answer`, `/{id}/abort`, `/problems`,
    `/image-analysis`, `/explain-symptom`, `/reference-images`,
    `/eligibility/{subscription_id}`.
    These query `cosh_connect_rows` with the stale slug
    `pest_diagnosis_chain` — a shape Cosh never shipped. Today they
    return empty in prod but have 30+ passing tests against synthetic
    data; semantic rewire to the real `pest_diagnosis` Connect is a
    future batch.

  • Cascading lookup path (Batches 21-22, also moved here from
    `advisory/router.py` for thematic coherence):
    `/crop-stages`, `/plant-parts`, `/plant-subparts`, `/symptoms`,
    `/subsymptoms`, `/candidates`, `/google-search-query`.
    These query the real `pest_diagnosis` Connect (synced 2026-05-14,
    6266 rows) and serve real Cosh data end-to-end.

The duplicate `/diagnosis/plant-parts` from the legacy file (broken
in prod) was dropped during the move; the lookup-path version takes
the slot. Tests for the legacy plant-parts didn't exist (the legacy
file's tests covered start/answer/abort/explain/reference-images, not
plant-parts).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.diagnosis.models import DiagnosisSession
from app.modules.diagnosis.schemas import (
    AnswerRequest,
    ExplainSymptomRequest,
    ImageAnalysisRequest,
    ReferenceImagesRequest,
    StartDiagnosisRequest,
)
from app.modules.platform.models import User
from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.bl08_diagnosis_path import (
    DiagnosisAnswer,
    ProblemSymptomRow,
    get_available_plant_parts,
    get_problem_list,
    run_diagnosis_step,
)
from app.services.claude_service import (
    analyze_crop_image,
    enrich_problem_with_description,
    explain_symptom,
)
from app.services.diagnosis_images import (
    build_google_images_query,
    find_reference_images,
    google_images_url,
)

router = APIRouter(tags=["Diagnosis"])


# Re-exports so tests / older callers can still
# `from app.modules.diagnosis.router import DiagnosisSession, AnswerRequest, ...`.
__all__ = [
    "router",
    "AnswerRequest", "ExplainSymptomRequest", "ImageAnalysisRequest",
    "ReferenceImagesRequest", "StartDiagnosisRequest",
    "DiagnosisSession",
    "_load_problem_symptom_rows",  # used by one test inline-import
    "answer_question", "start_diagnosis", "abort_diagnosis",
    "explain_symptom_route", "get_diagnosis_eligibility",
    "get_reference_images", "list_problems_for_crop",
    "analyse_image_with_claude",
]


# ── Legacy BL-08 helpers ──────────────────────────────────────────────────

async def _load_priority_rank_values(
    db: AsyncSession, rank_cosh_ids: set[str],
) -> dict[str, int]:
    """Resolve `priority_rank` Core cosh_ids to integer ranks.
    Reads `metadata.rank` first, falls back to `translations.en` as
    a digit string (per docs/COSH_2_SYNC_CONTRACT.md §8.1, Cosh
    designer can put the value in either place)."""
    if not rank_cosh_ids:
        return {}
    rows = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id.in_(rank_cosh_ids),
            CoshCoreItem.core_type == "priority_rank",
            CoshCoreItem.status == "active",
        )
    )).scalars().all()
    out: dict[str, int] = {}
    for row in rows:
        meta_rank = (row.metadata_ or {}).get("rank")
        if isinstance(meta_rank, int):
            out[row.cosh_id] = meta_rank
            continue
        en = (row.translations or {}).get("en")
        if isinstance(en, str) and en.strip().lstrip("-").isdigit():
            try:
                out[row.cosh_id] = int(en.strip())
            except ValueError:
                pass
    return out


async def _load_problem_symptom_rows(
    db: AsyncSession,
    crop_cosh_id: str,
    crop_stage_cosh_id: Optional[str],
) -> list[ProblemSymptomRow]:
    """Load `pest_diagnosis_chain` rows from `cosh_connect_rows`,
    filter by crop (mandatory) and crop_stage (optional), and pivot
    typed endpoints into the BL-08 dataclass.

    KNOWN GAP: the Cosh slug `pest_diagnosis_chain` never landed in
    production sync (Cosh ships `pest_diagnosis` instead, on the
    9-endpoint position-based shape). This function returns empty in
    prod today. A future batch will rewire to the real shape; the
    test suite seeds synthetic `_chain` rows so the BL-08 algorithm
    is still exercised."""
    q = select(CoshConnectRow).where(
        CoshConnectRow.connect_type == "pest_diagnosis_chain",
        CoshConnectRow.status == "active",
    )
    raw = (await db.execute(q)).scalars().all()

    accepted: list[tuple[CoshConnectRow, dict]] = []
    rank_cosh_ids: set[str] = set()
    for r in raw:
        endpoints = {ep["role"]: ep["cosh_id"] for ep in (r.endpoints or [])
                     if ep.get("role") and ep.get("cosh_id")}
        if endpoints.get("crop") != crop_cosh_id:
            continue
        if crop_stage_cosh_id and endpoints.get("crop_stage") != crop_stage_cosh_id:
            continue
        if not endpoints.get("pest") or not endpoints.get("part") \
                or not endpoints.get("symptom"):
            continue
        accepted.append((r, endpoints))
        rk = endpoints.get("priority_rank")
        if rk:
            rank_cosh_ids.add(rk)

    rank_values = await _load_priority_rank_values(db, rank_cosh_ids)

    rows: list[ProblemSymptomRow] = []
    for _r, endpoints in accepted:
        rank_id = endpoints.get("priority_rank")
        rows.append(ProblemSymptomRow(
            pest_cosh_id=endpoints["pest"],
            part_cosh_id=endpoints["part"],
            symptom_cosh_id=endpoints["symptom"],
            crop_cosh_id=endpoints.get("crop"),
            crop_stage_cosh_id=endpoints.get("crop_stage"),
            pest_stage_cosh_id=endpoints.get("pest_stage"),
            sub_part_cosh_id=endpoints.get("sub_part"),
            sub_symptom_cosh_id=endpoints.get("sub_symptom"),
            priority_rank=rank_values.get(rank_id) if rank_id else None,
        ))
    return rows


def _get_display_name(entity_cosh_id: str, lang: str = "en") -> str:
    """Placeholder — production lookup goes through cosh_core_items
    translations. Left in place so the BL-08 path still produces
    farmer-readable question text."""
    return entity_cosh_id.replace("_", " ").title()


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


async def _trigger_cha_from_diagnosis(
    db: AsyncSession, session: DiagnosisSession, problem_cosh_id: str,
):
    """Create a TriggeredCHAEntry so the farmer's advisory/today
    includes CHA timelines. Uses full SP→PG hierarchy: SP client →
    PG client → PG global."""
    from app.modules.advisory.models import Package as _Pkg
    from app.modules.subscriptions.models import (
        Subscription, TriggeredCHAEntry,
    )
    from app.services.cha_hierarchy import resolve_cha_recommendation

    sub = (await db.execute(
        select(Subscription).where(Subscription.id == session.subscription_id)
    )).scalar_one_or_none()
    if not sub:
        return

    package = (await db.execute(
        select(_Pkg).where(_Pkg.id == sub.package_id)
    )).scalar_one_or_none()
    sub_crop = package.crop_cosh_id if package else None

    resolved = await resolve_cha_recommendation(
        db, sub.client_id, problem_cosh_id, crop_cosh_id=sub_crop,
    )
    if not resolved:
        return

    db.add(TriggeredCHAEntry(
        subscription_id=session.subscription_id,
        farmer_user_id=session.farmer_user_id,
        client_id=sub.client_id,
        problem_cosh_id=problem_cosh_id,
        recommendation_type=resolved.recommendation_type,
        recommendation_id=resolved.recommendation_id,
        triggered_by="DIAGNOSIS",
        problem_name=resolved.problem_name,
        parent_pg_cosh_id=resolved.parent_pg_cosh_id,
    ))


async def _get_problem_info(db: AsyncSession, problem_cosh_id: str) -> dict:
    """Resolve problem cosh_id to {cosh_id, name, translations, type,
    parent_cosh_id}. Tries specific_problem first, then problem_group,
    then degrades to a humanised id fallback."""
    sp = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id == problem_cosh_id,
            CoshCoreItem.core_type == "specific_problem",
        )
    )).scalar_one_or_none()
    if not sp:
        pg = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.cosh_id == problem_cosh_id,
                CoshCoreItem.core_type == "problem_group",
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
        return {
            "cosh_id": problem_cosh_id,
            "name": _get_display_name(problem_cosh_id),
            "type": "unknown",
        }
    return {
        "cosh_id": problem_cosh_id,
        "name": sp.translations.get("en", problem_cosh_id),
        "translations": sp.translations,
        "type": "specific_problem",
        "parent_cosh_id": sp.parent_cosh_id,
    }


# ── Legacy BL-08 routes ───────────────────────────────────────────────────

@router.get("/diagnosis/eligibility/{subscription_id}")
async def get_diagnosis_eligibility(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read-only check: can this farmer hit Diagnose for this
    subscription? Returns `{eligible, reason_code?, message?}`.

    Two server-side gates (the farmer-PWA Diagnose button greys
    when either fails):
      - `crop_not_on_belt` — the CA soft-removed the crop from
        their company shortlist.
      - `cha_not_enabled` — RootsTalk hasn't enabled CHA on this
        crop at the platform.

    The "no start date" gate stays client-side — farmer-fillable, not
    a CA / platform decision."""
    from app.modules.advisory.models import Package
    from app.modules.clients.models import ClientCrop
    from app.modules.subscriptions.models import Subscription
    from app.modules.sync.models import CropHealthCrop

    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    package = (await db.execute(
        select(Package).where(Package.id == sub.package_id)
    )).scalar_one_or_none()
    if not package:
        raise HTTPException(status_code=404, detail="Subscription's package not found")

    on_belt = (await db.execute(
        select(ClientCrop).where(
            ClientCrop.client_id == sub.client_id,
            ClientCrop.crop_cosh_id == package.crop_cosh_id,
            ClientCrop.removed_at.is_(None),
        )
    )).scalar_one_or_none()
    if not on_belt:
        return {
            "eligible": False,
            "reason_code": "crop_not_on_belt",
            "message": (
                "Your company stopped offering this crop. Diagnosis is "
                "available only for currently-supported crops."
            ),
        }

    cha_enabled = (await db.execute(
        select(CropHealthCrop).where(
            CropHealthCrop.crop_cosh_id == package.crop_cosh_id,
            CropHealthCrop.status == "ACTIVE",
        )
    )).scalar_one_or_none()
    if not cha_enabled:
        return {
            "eligible": False,
            "reason_code": "cha_not_enabled",
            "message": (
                "RootsTalk hasn't enabled crop-health diagnosis on this "
                "crop yet. Check back later or ask your company."
            ),
        }
    return {"eligible": True}


@router.post("/diagnosis/start", status_code=201)
async def start_diagnosis(
    request: StartDiagnosisRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Begin a new diagnosis session. Returns the first question.

    Ownership gate: the subscription_id must belong to the caller.
    Without this, a malicious farmer could start a session bound to
    another farmer's subscription_id and ultimately trigger a CHA
    recommendation onto that farmer's advisory."""
    from app.modules.subscriptions.models import Subscription
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == request.subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    rows = await _load_problem_symptom_rows(
        db, request.crop_cosh_id, request.crop_stage_cosh_id,
    )
    if not rows:
        return {
            "status": "NO_DATA",
            "message": (
                "No diagnostic data available for this crop and stage "
                "yet. Please contact your company or ask an expert."
            ),
        }

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
        raise HTTPException(
            status_code=404,
            detail="Diagnosis session not found or already complete",
        )
    if request.answer not in ("YES", "NO"):
        raise HTTPException(status_code=422, detail="answer must be 'YES' or 'NO'")

    rows = await _load_problem_symptom_rows(
        db, session.crop_cosh_id, session.crop_stage_cosh_id,
    )
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
        crop_name = _get_display_name(session.crop_cosh_id)
        problem_info = await enrich_problem_with_description(problem_info, crop_name)
        await _trigger_cha_from_diagnosis(db, session, step.diagnosed_problem_cosh_id)
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
    """Farmer aborts:
      - reason='DONT_KNOW'    → redirects to FarmPundit query submission
      - reason='KNOW_PROBLEM' → direct diagnosis with problem_cosh_id
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
            raise HTTPException(
                status_code=422, detail="problem_cosh_id required for KNOW_PROBLEM",
            )
        session.status = "DIAGNOSED"
        session.diagnosed_problem_cosh_id = problem_cosh_id
        await _trigger_cha_from_diagnosis(db, session, problem_cosh_id)
        await db.commit()
        problem_info = await _get_problem_info(db, problem_cosh_id)
        crop_name = _get_display_name(session.crop_cosh_id)
        problem_info = await enrich_problem_with_description(problem_info, crop_name)
        return {
            "status": "DIAGNOSED",
            "diagnosed_problem_cosh_id": problem_cosh_id,
            "problem_info": problem_info,
        }
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
    """'I Know the Problem' — returns problems filtered to
    crop+stage+part."""
    rows = await _load_problem_symptom_rows(db, crop_cosh_id, crop_stage_cosh_id)
    problem_ids = get_problem_list(rows, plant_part=plant_part_cosh_id)
    result = []
    for pid in problem_ids:
        result.append(await _get_problem_info(db, pid))
    return result


@router.post("/diagnosis/image-analysis")
async def analyse_image_with_claude(
    request: ImageAnalysisRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer uploads a photo of the affected crop part. Claude
    analyses the image and returns the most likely problem name +
    matching Cosh problem_cosh_id (if available) + confidence
    (HIGH/MEDIUM/LOW) + 2-sentence farmer-friendly description +
    list of observed symptoms."""
    rows = await _load_problem_symptom_rows(
        db, request.crop_cosh_id, request.crop_stage_cosh_id,
    )
    known_problem_ids = list(dict.fromkeys(r.problem_cosh_id for r in rows))
    known_problem_names: list[str] = []
    for pid in known_problem_ids[:20]:
        info = await _get_problem_info(db, pid)
        known_problem_names.append(info.get("name", pid))

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
            "Claude identified a possible match — tap 'Confirm' to use "
            "this diagnosis, or 'Check with Questions' to verify via the "
            "guided path."
            if result.confidence in ("HIGH", "MEDIUM")
            else "Claude is not confident. Please use the guided YES/NO "
                 "questions for a more accurate diagnosis."
        ),
    }


@router.post("/diagnosis/explain-symptom")
async def explain_symptom_route(
    request: ExplainSymptomRequest,
    current_user: User = Depends(get_current_user),
):
    """Two plain-language sentences telling the farmer how to verify
    the current question's symptom. Auth-only (no DB access, no
    per-subscription state) — the explanation is the same for every
    farmer asking about the same (crop, part, symptom)."""
    text = await explain_symptom(
        crop_name=_get_display_name(request.crop_cosh_id),
        plant_part_name=_get_display_name(request.plant_part_cosh_id),
        symptom_name=_get_display_name(request.symptom_cosh_id),
        sub_part_name=_get_display_name(request.sub_part_cosh_id) if request.sub_part_cosh_id else None,
        sub_symptom_name=_get_display_name(request.sub_symptom_cosh_id) if request.sub_symptom_cosh_id else None,
        language_code=request.language_code,
        language_name=request.language_name,
    )
    return {"explanation": text, "language_code": request.language_code}


@router.post("/diagnosis/reference-images")
async def get_reference_images(
    request: ReferenceImagesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reference images Cosh has curated for the farmer's current
    question, plus a Google Images fallback URL the PWA always has
    on hand.

    Returns:
      {
        "images": [{"cosh_id", "url", "media_type", "caption"}, ...],
        "google_images_url":   "https://...",
        "google_images_query": "Powdery mildew on leaves of Tomato",
        "language_code":       "en"
      }

    The API field `plant_part_cosh_id` is kept for backward-compat
    with the diagnosis flow's existing terminology; internally it
    maps to Cosh role `part`. `images: []` is the no-image fallback
    signal."""
    images = await find_reference_images(
        db,
        crop_cosh_id=request.crop_cosh_id,
        crop_stage_cosh_id=request.crop_stage_cosh_id,
        part_cosh_id=request.plant_part_cosh_id,
        symptom_cosh_id=request.symptom_cosh_id,
        sub_part_cosh_id=request.sub_part_cosh_id,
        sub_symptom_cosh_id=request.sub_symptom_cosh_id,
        language_code=request.language_code,
    )
    query = await build_google_images_query(
        db,
        crop_cosh_id=request.crop_cosh_id,
        part_cosh_id=request.plant_part_cosh_id,
        symptom_cosh_id=request.symptom_cosh_id,
        sub_part_cosh_id=request.sub_part_cosh_id,
        sub_symptom_cosh_id=request.sub_symptom_cosh_id,
        language_code=request.language_code,
    )
    return {
        "images": [
            {
                "cosh_id": img.cosh_id,
                "url": img.url,
                "media_type": img.media_type,
                "caption": img.caption,
            }
            for img in images
        ],
        "google_images_url": google_images_url(query) if query else None,
        "google_images_query": query or None,
        "language_code": request.language_code,
    }


# ── Cascading lookup path (Batches 21-22) ─────────────────────────────────
#
# Cascading filter over the real 9-endpoint `pest_diagnosis` Connect
# (synced 2026-05-14). Each step narrows by the prior selections;
# BLANK BOX-aware matching. See app/services/pest_diagnosis_view.py.

@router.get("/diagnosis/crop-stages")
async def diagnosis_crop_stages(
    crop_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.pest_diagnosis_view import list_crop_stages
    return await list_crop_stages(db, crop_cosh_id=crop_cosh_id)


@router.get("/diagnosis/plant-parts")
async def diagnosis_plant_parts(
    crop_cosh_id: str,
    crop_stage: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.pest_diagnosis_view import list_plant_parts
    return await list_plant_parts(
        db, crop_cosh_id=crop_cosh_id, crop_stage=crop_stage,
    )


@router.get("/diagnosis/plant-subparts")
async def diagnosis_plant_subparts(
    crop_cosh_id: str,
    crop_stage: Optional[str] = None,
    plant_part: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.pest_diagnosis_view import list_plant_subparts
    return await list_plant_subparts(
        db, crop_cosh_id=crop_cosh_id, crop_stage=crop_stage,
        plant_part=plant_part,
    )


@router.get("/diagnosis/symptoms")
async def diagnosis_symptoms(
    crop_cosh_id: str,
    crop_stage: Optional[str] = None,
    plant_part: Optional[str] = None,
    plant_subpart: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.pest_diagnosis_view import list_symptoms
    return await list_symptoms(
        db, crop_cosh_id=crop_cosh_id, crop_stage=crop_stage,
        plant_part=plant_part, plant_subpart=plant_subpart,
    )


@router.get("/diagnosis/subsymptoms")
async def diagnosis_subsymptoms(
    crop_cosh_id: str,
    crop_stage: Optional[str] = None,
    plant_part: Optional[str] = None,
    plant_subpart: Optional[str] = None,
    symptom: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.pest_diagnosis_view import list_subsymptoms
    return await list_subsymptoms(
        db, crop_cosh_id=crop_cosh_id, crop_stage=crop_stage,
        plant_part=plant_part, plant_subpart=plant_subpart,
        symptom=symptom,
    )


@router.get("/diagnosis/candidates")
async def diagnosis_candidates(
    crop_cosh_id: str,
    crop_stage: Optional[str] = None,
    plant_part: Optional[str] = None,
    plant_subpart: Optional[str] = None,
    symptom: Optional[str] = None,
    subsymptom: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Final diagnosis lookup — returns ranked candidate pests with
    curated images. Each candidate carries `image_urls` scoped to
    the filter context (post-cascade, post-dedup). Empty when Cosh
    hasn't curated images for that pest at that context. Farmer-PWA
    falls back to `/diagnosis/google-search-query` for visual
    research in that case."""
    from app.services.pest_diagnosis_view import list_candidates
    return await list_candidates(
        db, crop_cosh_id=crop_cosh_id, crop_stage=crop_stage,
        plant_part=plant_part, plant_subpart=plant_subpart,
        symptom=symptom, subsymptom=subsymptom,
    )


@router.get("/diagnosis/google-search-query")
async def diagnosis_google_search_query(
    crop_cosh_id: str,
    crop_stage: Optional[str] = None,
    plant_part: Optional[str] = None,
    plant_subpart: Optional[str] = None,
    symptom: Optional[str] = None,
    subsymptom: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Context-aware Google Images search terms used at any Symptom
    Node where the farmer wants visual research.

    Format: `<Crop> <Part> <Symptom>` — pest name intentionally
    omitted (one Symptom Node fans out to many pests; biasing the
    search toward one would skew the farmer's visual matching).
    Subsymptom isn't used today; we may promote to subsymptom-level
    specificity once farmer usage tells us the broader symptom misses
    too much.

    Returns `{"query": "..."}`. Empty string when nothing resolves
    (frontend hides the button in that case)."""
    from app.services.cosh_constants import (
        COSH_BIOLOGICAL_NAMES_CORE,
        COSH_DAMAGE_SYMPTOMS_CORE,
        COSH_PLANT_PARTS_CORE,
    )
    from app.services.pest_diagnosis_images_view import build_google_search_query

    async def _en(core_type: str, cosh_id: Optional[str]) -> Optional[str]:
        if not cosh_id:
            return None
        core = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.core_type == core_type,
                CoshCoreItem.cosh_id == cosh_id,
                CoshCoreItem.status == "active",
            )
        )).scalar_one_or_none()
        if core is None:
            return None
        t = core.translations or {}
        return t.get("en") or t.get("English")

    crop_name = await _en(COSH_BIOLOGICAL_NAMES_CORE, crop_cosh_id)
    part_name = await _en(COSH_PLANT_PARTS_CORE, plant_part)
    sym_name = await _en(COSH_DAMAGE_SYMPTOMS_CORE, symptom)
    query = build_google_search_query(
        crop_name=crop_name,
        plant_part_name=part_name,
        symptom_name=sym_name,
    )
    return {"query": query}


# ── SP × PG × Crop applicability (Batch 39Q, 2026-05-16) ──────────────────
#
# Reads through Cosh's `sp_pg_crops` Connect (1,633 rows). Three lookup
# directions: crops-for-PG, PGs-for-crop, SPs-for-(PG,crop). Used by the
# CHA-Global authoring UI (crop chips on each PG card) and the SE's
# Add Specific-Problem picker (crop-scoped SP list).


@router.get("/diagnosis/pg-crops")
async def diagnosis_pg_crops(
    pg: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crops applicable to a Problem Group.

    Args (querystring):
      pg — `problem_groups` Core cosh_id.

    Returns:
      `{"items": [{"cosh_id", "name_en"}, ...]}` sorted by name.
      Empty `items` when the PG has no rows or is unknown."""
    from app.services.sp_pg_crops_view import list_crops_for_pg
    items = await list_crops_for_pg(db, pg_cosh_id=pg)
    return {"items": items}


@router.get("/diagnosis/pg-by-crop")
async def diagnosis_pg_by_crop(
    crop: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Problem Groups applicable to a crop. Reverse of /pg-crops."""
    from app.services.sp_pg_crops_view import list_pgs_for_crop
    items = await list_pgs_for_crop(db, crop_cosh_id=crop)
    return {"items": items}


@router.get("/diagnosis/sps-by-pg-crop")
async def diagnosis_sps_by_pg_crop(
    pg: str,
    crop: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Specific Problems at the (PG, crop) intersection. Drives the
    SE's Add Specific-Problem picker when authoring SP-level
    recommendations under a PG that's scoped to a crop bundle."""
    from app.services.sp_pg_crops_view import list_sps_for_pg_crop
    items = await list_sps_for_pg_crop(
        db, pg_cosh_id=pg, crop_cosh_id=crop,
    )
    return {"items": items}
