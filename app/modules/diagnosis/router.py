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
    AIDirectDiagnoseRequest,
    AnswerRequest,
    CommitToAdvisoryRequest,
    ExplainSymptomRequest,
    ImageCheckSymptomRequest,
    ImageAnalysisRequest,
    ReferenceImagesRequest,
    StartDiagnosisRequest,
)
from app.modules.platform.models import User
from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.i18n_cosh import (
    pick_translation,
    resolve_name_for_cosh_id,
    resolve_names_by_cosh_id,
)
from app.services.bl08_diagnosis_path import (
    DiagnosisAnswer,
    ProblemSymptomRow,
    get_available_plant_parts,
    get_problem_list,
    run_diagnosis_step,
)
from app.services.claude_service import (
    analyze_crop_image,
    analyze_crop_images_constrained,
    check_symptom_in_image,
    enrich_problem_with_description,
    explain_symptom,
    language_name_for,
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
    "ImageCheckSymptomRequest",
    "ReferenceImagesRequest", "StartDiagnosisRequest",
    "DiagnosisSession",
    "_load_problem_symptom_rows",  # used by one test inline-import
    "answer_question", "start_diagnosis", "abort_diagnosis",
    "explain_symptom_route", "get_diagnosis_eligibility",
    "get_reference_images", "list_problems_for_crop",
    "analyse_image_with_claude",
    "image_check_symptom_route",
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
    # Real Cosh ships ranks on `priority_rank_pests`. The earlier
    # legacy slug was `priority_rank`; we also accept it so any old
    # seeded data (incl. tests) keeps resolving.
    rows = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id.in_(rank_cosh_ids),
            CoshCoreItem.core_type.in_(["priority_rank_pests", "priority_rank"]),
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
    """Load `pest_diagnosis` Connect rows from `cosh_connect_rows`,
    filter to this crop (mandatory) and crop_stage (optional), and
    pivot the 9-position shape into BL-08's role-keyed dataclass.

    Wire shape (locked 2026-05-14, see project memory
    `project_rootstalk_pest_diagnosis.md`):
      pos 1 = damage_symptoms   → symptom
      pos 2 = damage_subsymptoms→ sub_symptom (BLANK BOX = wildcard)
      pos 3 = biological_names  → pest
      pos 4 = pest_stages       → pest_stage
      pos 5 = plant_parts       → part
      pos 6 = plant_subparts    → sub_part   (BLANK BOX = wildcard)
      pos 7 = biological_names  → crop
      pos 8 = crop_stages       → crop_stage (BLANK BOX = wildcard)
      pos 9 = priority_rank_pests → priority_rank (English "0".."5")

    BLANK BOX semantics: where a dimension on a row carries the
    sentinel cosh_id, the constraint is vacuously satisfied — the
    row behaves as a wildcard along that axis, and the dataclass
    field is set to None so the downstream BL-08 algorithm reads it
    as "applies regardless".

    Rewired from `pest_diagnosis_chain` (slug Cosh never shipped) on
    2026-05-25 to unblock the farmer-PWA Diagnose flow on real data.
    """
    from app.services.cosh_constants import (
        PD_POS_SYMPTOM, PD_POS_SUBSYMPTOM, PD_POS_PEST, PD_POS_PEST_STAGE,
        PD_POS_PLANT_PART, PD_POS_PLANT_SUBPART, PD_POS_CROP,
        PD_POS_CROP_STAGE, PD_POS_PRIORITY_RANK,
        PD_BLANK_BOX_BY_CORE,
        COSH_DAMAGE_SUBSYMPTOMS_CORE, COSH_PLANT_SUBPARTS_CORE,
        COSH_CROP_STAGES_CORE,
    )

    subsymptom_blank = PD_BLANK_BOX_BY_CORE.get(COSH_DAMAGE_SUBSYMPTOMS_CORE)
    subpart_blank = PD_BLANK_BOX_BY_CORE.get(COSH_PLANT_SUBPARTS_CORE)
    crop_stage_blank = PD_BLANK_BOX_BY_CORE.get(COSH_CROP_STAGES_CORE)

    def at(row: CoshConnectRow, position: int) -> Optional[str]:
        for ep in (row.endpoints or []):
            if ep.get("position") == position:
                return ep.get("cosh_id")
        return None

    q = select(CoshConnectRow).where(
        CoshConnectRow.connect_type == "pest_diagnosis",
        CoshConnectRow.status == "active",
    )
    raw = (await db.execute(q)).scalars().all()

    accepted: list[dict] = []
    rank_cosh_ids: set[str] = set()
    for r in raw:
        row_crop = at(r, PD_POS_CROP)
        if row_crop != crop_cosh_id:
            continue

        row_stage = at(r, PD_POS_CROP_STAGE)
        if crop_stage_cosh_id is not None:
            # Match strictly OR honour BLANK BOX wildcard.
            if row_stage != crop_stage_cosh_id and row_stage != crop_stage_blank:
                continue

        pest = at(r, PD_POS_PEST)
        part = at(r, PD_POS_PLANT_PART)
        symptom = at(r, PD_POS_SYMPTOM)
        # BL-08 dataclass requires all three. A row missing any of
        # them is degenerate Cosh data; skip silently.
        if not (pest and part and symptom):
            continue

        sub_symptom = at(r, PD_POS_SUBSYMPTOM)
        if subsymptom_blank is not None and sub_symptom == subsymptom_blank:
            sub_symptom = None

        sub_part = at(r, PD_POS_PLANT_SUBPART)
        if subpart_blank is not None and sub_part == subpart_blank:
            sub_part = None

        pest_stage = at(r, PD_POS_PEST_STAGE)
        priority_rank_id = at(r, PD_POS_PRIORITY_RANK)

        # Normalise the row's crop_stage to None when it carries the
        # wildcard sentinel — the BL-08 algorithm reads this field as
        # an absolute filter, not a wildcard, so the sentinel must
        # not leak through.
        normalised_stage = row_stage if row_stage != crop_stage_blank else None

        accepted.append({
            "pest": pest, "part": part, "symptom": symptom,
            "sub_symptom": sub_symptom, "sub_part": sub_part,
            "pest_stage": pest_stage,
            "crop": row_crop,
            "crop_stage": normalised_stage,
            "priority_rank_id": priority_rank_id,
        })
        if priority_rank_id:
            rank_cosh_ids.add(priority_rank_id)

    rank_values = await _load_priority_rank_values(db, rank_cosh_ids)

    rows: list[ProblemSymptomRow] = []
    for a in accepted:
        rid = a["priority_rank_id"]
        rows.append(ProblemSymptomRow(
            pest_cosh_id=a["pest"],
            part_cosh_id=a["part"],
            symptom_cosh_id=a["symptom"],
            crop_cosh_id=a["crop"],
            crop_stage_cosh_id=a["crop_stage"],
            pest_stage_cosh_id=a["pest_stage"],
            sub_part_cosh_id=a["sub_part"],
            sub_symptom_cosh_id=a["sub_symptom"],
            priority_rank=rank_values.get(rid) if rid else None,
        ))
    return rows


def _safe_name(cosh_id: Optional[str], names: dict[str, str]) -> Optional[str]:
    """Return the resolved name for a cosh_id, or None when it can't
    be resolved (caller drops the term from the question text rather
    than leaking a UUID into the farmer-facing string)."""
    if not cosh_id:
        return None
    return names.get(cosh_id)


def _build_question_text(question, names: dict[str, str]) -> str:
    """Compose the farmer-facing question string from resolved Cosh
    names. If any required term can't be resolved we fall back to
    generic copy — never echo a raw cosh_id."""
    # CONFIRMATION questions don't carry a symptom — the PWA renders
    # its own "Looks like the problem is X. Does this match what you're
    # seeing?" card using `problem_info`. We emit a clean fallback
    # string here for debug/logging; the PWA doesn't read it.
    if getattr(question, "question_type", None) == "CONFIRMATION":
        return "Does this match what you're seeing?"
    part = _safe_name(question.plant_part_cosh_id, names) or "this plant part"
    symptom = _safe_name(question.symptom_cosh_id, names) or "this symptom"
    sub_part = _safe_name(question.sub_part_cosh_id, names)
    sub_symptom = _safe_name(question.sub_symptom_cosh_id, names)
    if sub_part and sub_symptom:
        return f"Is there {sub_symptom} on the {sub_part} of the {part}?"
    elif sub_symptom:
        return f"Do the {symptom} on the {part} look like: {sub_symptom}?"
    elif sub_part:
        return f"Is the {symptom} on the {sub_part} of the {part}?"
    else:
        return f"Do you see {symptom} on the {part}?"


async def _format_question(question, db: AsyncSession, lang: str = "en"):
    """Resolve the question's cosh_ids to friendly names and emit the
    farmer-facing payload. Always returns names alongside ids so the
    PWA can drop the cosh_id-fallback breadcrumb entirely."""
    if not question:
        return None
    cosh_ids = {
        question.plant_part_cosh_id, question.symptom_cosh_id,
        question.sub_part_cosh_id, question.sub_symptom_cosh_id,
    }
    cosh_ids.discard(None)
    names = await resolve_names_by_cosh_id(db, cosh_ids, lang)
    return {
        "plant_part_cosh_id": question.plant_part_cosh_id,
        "symptom_cosh_id": question.symptom_cosh_id,
        "sub_part_cosh_id": question.sub_part_cosh_id,
        "sub_symptom_cosh_id": question.sub_symptom_cosh_id,
        "plant_part_name": _safe_name(question.plant_part_cosh_id, names),
        "symptom_name": _safe_name(question.symptom_cosh_id, names),
        "sub_part_name": _safe_name(question.sub_part_cosh_id, names),
        "sub_symptom_name": _safe_name(question.sub_symptom_cosh_id, names),
        "question_type": question.question_type,
        "display_text": _build_question_text(question, names),
    }


async def _trigger_cha_from_diagnosis(
    db: AsyncSession, session: DiagnosisSession, problem_cosh_id: str,
    affected_plants_count: Optional[int] = None,
):
    """Create a TriggeredCHAEntry so the farmer's advisory/today
    includes CHA timelines. Uses full SP→PG hierarchy: SP client →
    PG client → PG global.

    `affected_plants_count` is persisted on the new entry for
    plant-wise crops (drives BL-06's `Count` variable downstream).
    Area-wise crops pass None.
    """
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

    farmer = (await db.execute(
        select(User).where(User.id == session.farmer_user_id)
    )).scalar_one_or_none()
    resolved = await resolve_cha_recommendation(
        db, sub.client_id, problem_cosh_id, crop_cosh_id=sub_crop,
        lang=(farmer.language_code if farmer else None) or "en",
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
        affected_plants_count=affected_plants_count,
    ))


async def _get_problem_info(
    db: AsyncSession, problem_cosh_id: str, lang: str = "en",
) -> dict:
    """Resolve problem cosh_id to {cosh_id, name, translations, type,
    parent_cosh_id}. Tries specific_problem first, then problem_group,
    then degrades to a humanised id fallback. `name` is localised to
    `lang` when a translation exists, otherwise English, otherwise the
    cosh_id."""
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
                "name": pick_translation(pg.translations, lang, problem_cosh_id),
                "translations": pg.translations,
                "type": "problem_group",
                "parent_cosh_id": pg.parent_cosh_id,
            }
        # Fall back to a Core lookup by cosh_id alone (the problem
        # may be a `biological_names` pest from the BL-08 path, which
        # isn't under "specific_problem" or "problem_group" cores).
        any_core = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.cosh_id == problem_cosh_id,
                CoshCoreItem.status == "active",
            )
        )).scalar_one_or_none()
        if any_core:
            return {
                "cosh_id": problem_cosh_id,
                "name": pick_translation(any_core.translations, lang, problem_cosh_id),
                "translations": any_core.translations,
                "type": any_core.core_type,
            }
        return {
            "cosh_id": problem_cosh_id,
            "name": problem_cosh_id,
            "type": "unknown",
        }
    return {
        "cosh_id": problem_cosh_id,
        "name": pick_translation(sp.translations, lang, problem_cosh_id),
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
    # 2026-06-30 — Surface measure + declared plant count so the PWA
    # diagnose flow can render the mandatory "How many plants are
    # affected?" prompt before commit-to-advisory on plant-wise crops.
    from app.services.cosh_crop_view import get_measure_for_biological_name
    measure = await get_measure_for_biological_name(db, package.crop_cosh_id)
    return {
        "eligible": True,
        "measure": measure,
        "number_of_plants": int(sub.number_of_plants) if sub.number_of_plants else None,
    }


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

    response_status = step.status
    session_status = "ACTIVE"
    problem_info = None
    if step.status == "CONFIRMATION":
        # The pool resolved to exactly 1 candidate without any narrowing
        # answers — resolve its name so the PWA can render the
        # confirmation prompt. Session remains ACTIVE until the farmer
        # answers the confirmation.
        problem_info = await _get_problem_info(
            db, step.diagnosed_problem_cosh_id, current_user.language_code or "en",
        )
        crop_name = await resolve_name_for_cosh_id(
            db, request.crop_cosh_id, current_user.language_code or "en",
        ) or request.crop_cosh_id
        problem_info = await enrich_problem_with_description(problem_info, crop_name, language_code=current_user.language_code or "en")
    elif step.status == "INCONCLUSIVE":
        # Pool empty at start: no problems mapped to this crop + stage
        # + plant_part. Surface as OUTSIDE_LIST so the PWA shows the
        # honest "not in our catalogue" screen and routes to expert.
        session_status = "OUTSIDE_LIST"
        response_status = "OUTSIDE_LIST"

    session = DiagnosisSession(
        subscription_id=request.subscription_id,
        farmer_user_id=current_user.id,
        crop_cosh_id=request.crop_cosh_id,
        crop_stage_cosh_id=request.crop_stage_cosh_id,
        initial_plant_part_cosh_id=request.plant_part_cosh_id,
        remaining_problem_ids=step.remaining_problem_ids,
        answers=[],
        has_yes_answer=False,
        status=session_status,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return {
        "session_id": session.id,
        "status": response_status,
        "remaining_count": step.remaining_count,
        "question": await _format_question(
            step.question, db, current_user.language_code or "en",
        ),
        "diagnosed_problem_cosh_id": step.diagnosed_problem_cosh_id,
        "problem_info": problem_info,
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

    # ── Confirmation branch (BL-08 §8, amended 2026-05-28) ──────────────
    # When the previous step narrowed the pool to a single candidate,
    # the PWA shows a confirmation card and sets `is_confirmation=True`
    # on the answer. YES flips the session to DIAGNOSED; NO flips it to
    # OUTSIDE_LIST (the system honestly admits the farmer's problem
    # isn't in the catalogue and points them to an expert).
    # Defence-in-depth: only honour the flag when the session actually
    # has 1 candidate stored — a stray flag on a regular answer can't
    # shortcut the algorithm.
    if request.is_confirmation:
        if len(session.remaining_problem_ids or []) != 1:
            raise HTTPException(
                status_code=409,
                detail="is_confirmation set but session has no single candidate to confirm",
            )
        candidate_cosh_id = session.remaining_problem_ids[0]
        if request.answer == "YES":
            session.status = "DIAGNOSED"
            session.diagnosed_problem_cosh_id = candidate_cosh_id
            problem_info = await _get_problem_info(
                db, candidate_cosh_id, current_user.language_code or "en",
            )
            crop_name = await resolve_name_for_cosh_id(
                db, session.crop_cosh_id, current_user.language_code or "en",
            ) or session.crop_cosh_id
            problem_info = await enrich_problem_with_description(problem_info, crop_name, language_code=current_user.language_code or "en")
            await db.commit()
            return {
                "session_id": session_id,
                "status": "DIAGNOSED",
                "remaining_count": 1,
                "question": None,
                "diagnosed_problem_cosh_id": candidate_cosh_id,
                "problem_info": problem_info,
                "committed_to_advisory": session.committed_at is not None,
            }
        # request.answer == "NO" — farmer rejects the candidate.
        session.status = "OUTSIDE_LIST"
        await db.commit()
        return {
            "session_id": session_id,
            "status": "OUTSIDE_LIST",
            "remaining_count": 0,
            "question": None,
            "diagnosed_problem_cosh_id": None,
            "problem_info": None,
            "committed_to_advisory": False,
        }

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

    response_status = step.status
    if step.status == "DIAGNOSED":
        # Reached only via §7(d) random tie-break — multiple candidates
        # could not be narrowed further. (Convergence to 1 now returns
        # CONFIRMATION instead, per the BL-08 §8 amendment.)
        session.status = "DIAGNOSED"
        session.diagnosed_problem_cosh_id = step.diagnosed_problem_cosh_id
        problem_info = await _get_problem_info(
            db, step.diagnosed_problem_cosh_id, current_user.language_code or "en",
        )
        crop_name = await resolve_name_for_cosh_id(
            db, session.crop_cosh_id, current_user.language_code or "en",
        ) or session.crop_cosh_id
        problem_info = await enrich_problem_with_description(problem_info, crop_name, language_code=current_user.language_code or "en")
        # NB: CHA trigger is no longer fired here. The farmer commits
        # the diagnosis to their advisory explicitly via the
        # /diagnosis/{session_id}/commit-to-advisory endpoint.
    elif step.status == "CONFIRMATION":
        # Pool reduced to exactly 1. Resolve problem_info so the PWA
        # can render the confirmation prompt ("Looks like the problem
        # is X. Does this match what you're seeing?"). Session stays
        # ACTIVE — terminal transition fires when the farmer answers
        # the confirmation via the is_confirmation branch above.
        problem_info = await _get_problem_info(
            db, step.diagnosed_problem_cosh_id, current_user.language_code or "en",
        )
        crop_name = await resolve_name_for_cosh_id(
            db, session.crop_cosh_id, current_user.language_code or "en",
        ) or session.crop_cosh_id
        problem_info = await enrich_problem_with_description(problem_info, crop_name, language_code=current_user.language_code or "en")
    elif step.status == "INCONCLUSIVE":
        # Rare: a NO that eliminated the last 2+ candidates at once.
        # Surface to the farmer as OUTSIDE_LIST — same UX as a NO on a
        # CONFIRMATION prompt. The session is terminal.
        session.status = "OUTSIDE_LIST"
        response_status = "OUTSIDE_LIST"
        problem_info = None
    else:
        problem_info = None

    await db.commit()
    return {
        "session_id": session_id,
        "status": response_status,
        "remaining_count": step.remaining_count,
        "question": await _format_question(
            step.question, db, current_user.language_code or "en",
        ),
        "diagnosed_problem_cosh_id": step.diagnosed_problem_cosh_id,
        "problem_info": problem_info,
        "committed_to_advisory": session.committed_at is not None,
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
        # CHA trigger deferred to /commit-to-advisory (opt-in).
        await db.commit()
        problem_info = await _get_problem_info(
            db, problem_cosh_id, current_user.language_code or "en",
        )
        crop_name = await resolve_name_for_cosh_id(
            db, session.crop_cosh_id, current_user.language_code or "en",
        ) or session.crop_cosh_id
        problem_info = await enrich_problem_with_description(problem_info, crop_name, language_code=current_user.language_code or "en")
        return {
            "status": "DIAGNOSED",
            "diagnosed_problem_cosh_id": problem_cosh_id,
            "problem_info": problem_info,
            "committed_to_advisory": session.committed_at is not None,
        }
    session.status = "ABORTED"
    await db.commit()
    return {
        "status": "ABORTED",
        "next_action": "QUERY",
        "subscription_id": session.subscription_id,
        "message": "Opening FarmPundit query form.",
    }


@router.post("/diagnosis/{session_id}/commit-to-advisory")
async def commit_diagnosis_to_advisory(
    session_id: str,
    body: Optional[CommitToAdvisoryRequest] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Commit a diagnosed session to the farmer's advisory.

    Per user direction 2026-05-25: the CHA trigger no longer fires
    automatically on DIAGNOSED. The farmer reaches the diagnosed
    screen, reviews the problem + Claude description + reference
    images, and only then taps "Add Treatment Recommendations to
    the Advisory" — that lands here.

    Idempotent — a session can be committed at most once. Re-tap is
    a no-op that returns the same success payload.

    Refuses (422 `not_diagnosed`) if the session hasn't reached the
    DIAGNOSED state; the caller shouldn't have surfaced the CTA in
    that case but we guard server-side anyway.

    `affected_plants_count` (2026-06-30): mandatory for plant-wise
    crops. Drives BL-06's `Count` variable downstream so volume sizes
    to the count of affected plants rather than the farmer's declared
    total — the principle being that treating 200 palms when 10 are
    infested wastes money and resources. Ignored for area-wise crops.
    """
    session = (await db.execute(
        select(DiagnosisSession).where(
            DiagnosisSession.id == session_id,
            DiagnosisSession.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status != "DIAGNOSED" or not session.diagnosed_problem_cosh_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "not_diagnosed",
                "message": (
                    "This session hasn't reached a diagnosis yet. "
                    "Answer the questions to identify the problem first."
                ),
                "current_status": session.status,
            },
        )

    if session.committed_at is not None:
        # Idempotent — already committed; surface the same success
        # shape so the client doesn't need a separate "already done"
        # branch.
        return {
            "committed_to_advisory": True,
            "already_committed": True,
            "subscription_id": session.subscription_id,
        }

    # Validate `affected_plants_count` for plant-wise crops.
    from app.modules.subscriptions.models import Subscription
    from app.services.cosh_crop_view import get_measure_for_biological_name
    from app.modules.advisory.models import Package as _Pkg

    sub = (await db.execute(
        select(Subscription).where(Subscription.id == session.subscription_id)
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    package = (await db.execute(
        select(_Pkg).where(_Pkg.id == sub.package_id)
    )).scalar_one_or_none()
    crop_cosh_id = package.crop_cosh_id if package else None
    measure = (
        await get_measure_for_biological_name(db, crop_cosh_id)
        if crop_cosh_id else None
    )

    affected_count: Optional[int] = body.affected_plants_count if body else None
    if measure == "PLANT_WISE":
        if affected_count is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "affected_plants_count_required",
                    "message": (
                        "How many plants are affected? Required for "
                        "plant-wise crops."
                    ),
                },
            )
        if affected_count < 1:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "affected_plants_count_min",
                    "message": "Affected-plants count must be at least 1.",
                },
            )
        if sub.number_of_plants and affected_count > sub.number_of_plants:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "affected_plants_count_max",
                    "message": (
                        f"Affected-plants count cannot exceed your "
                        f"declared {sub.number_of_plants} plants."
                    ),
                    "max": sub.number_of_plants,
                },
            )
    else:
        # Area-wise: silently ignore even if the client sent a value.
        affected_count = None

    from datetime import datetime, timezone
    await _trigger_cha_from_diagnosis(
        db, session, session.diagnosed_problem_cosh_id,
        affected_plants_count=affected_count,
    )
    session.committed_at = datetime.now(timezone.utc)
    await db.commit()
    return {
        "committed_to_advisory": True,
        "already_committed": False,
        "subscription_id": session.subscription_id,
    }


@router.post("/diagnosis/ai-direct-diagnose")
async def ai_direct_diagnose(
    request: AIDirectDiagnoseRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Direct AI diagnosis — alternative to BL-08 dichotomous Q&A.

    The farmer skips part-picking + YES/NO narrowing entirely; uploads
    one or more photos; Claude vision is constrained to pick from the
    crop's known problem catalogue (filtered by stage if supplied) or
    return `needs_expert=true`.

    On a confident match we seed a DIAGNOSED `DiagnosisSession` so the
    existing `/commit-to-advisory` endpoint can route the resulting
    PG/SP recommendation into the farmer's advisory — same downstream
    path as a BL-08-narrowed diagnosis. On `needs_expert` we return
    without creating a session; the PWA routes the farmer to the
    FarmPundit query flow.

    The constraint to a known list is deliberate: Claude is told
    explicitly NOT to invent a problem name outside the catalogue
    (defence-in-depth check coerces a stray cosh_id back to
    `needs_expert`). This keeps the advisory bridge sound — a phantom
    pest with no curated recommendation would land on a dead end.
    """
    from app.modules.subscriptions.models import Subscription
    from app.services.pest_diagnosis_view import list_candidates

    # Subscription ownership — same gate the BL-08 start endpoint
    # uses. Closes the cross-farmer trigger gap.
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == request.subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # Build the candidate catalogue. `list_candidates` already dedupes
    # by (pest, pest_stage) and orders by priority rank — best signal
    # for the AI.
    candidates = await list_candidates(
        db,
        crop_cosh_id=request.crop_cosh_id,
        crop_stage=request.crop_stage_cosh_id,
        lang=current_user.language_code or "en",
    )
    # Collapse to one entry per pest (Claude shouldn't see the same
    # pest 3× because Cosh has separate rows per life-stage); keep
    # the first occurrence which is already the highest-priority row.
    seen: set[str] = set()
    catalogue: list[dict] = []
    for c in candidates:
        pid = c.get("pest_cosh_id") or c.get("cosh_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        catalogue.append(c)

    known_ids = [c["pest_cosh_id"] for c in catalogue if c.get("pest_cosh_id")]
    known_names = [
        c.get("pest_name") or c.get("pest_cosh_id", "")
        for c in catalogue if c.get("pest_cosh_id")
    ]

    crop_name = await resolve_name_for_cosh_id(
        db, request.crop_cosh_id, request.language_code,
    ) or request.crop_cosh_id
    stage_name = await resolve_name_for_cosh_id(
        db, request.crop_stage_cosh_id, request.language_code,
    ) if request.crop_stage_cosh_id else None

    result = await analyze_crop_images_constrained(
        images=[{"base64": i.base64, "media_type": i.media_type} for i in request.images],
        crop_name=crop_name,
        crop_stage_name=stage_name,
        known_problem_ids=known_ids,
        known_problem_names=known_names,
        language_code=request.language_code,
    )

    if result.needs_expert or not result.problem_cosh_id:
        return {
            "needs_expert": True,
            "analysis": result.to_dict(),
            "subscription_id": request.subscription_id,
        }

    # Confident match — seed a DIAGNOSED session so the existing
    # commit-to-advisory endpoint owns the trigger. No answers
    # recorded; remaining_problem_ids carries only the picked pest
    # so audit-trail queries can still see what the AI surfaced.
    session = DiagnosisSession(
        subscription_id=request.subscription_id,
        farmer_user_id=current_user.id,
        crop_cosh_id=request.crop_cosh_id,
        crop_stage_cosh_id=request.crop_stage_cosh_id,
        initial_plant_part_cosh_id="",  # not chosen on AI path
        remaining_problem_ids=[result.problem_cosh_id],
        answers=[],
        has_yes_answer=False,
        status="DIAGNOSED",
        diagnosed_problem_cosh_id=result.problem_cosh_id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    # Enrich the problem info with a Claude-generated farmer-friendly
    # description in the farmer's language — same enrichment path the
    # BL-08 DIAGNOSED branch uses, so the diagnosed-screen render is
    # uniform regardless of which path landed us here.
    problem_info = await _get_problem_info(
        db, result.problem_cosh_id, current_user.language_code or "en",
    )
    problem_info = await enrich_problem_with_description(problem_info, crop_name, language_code=current_user.language_code or "en")

    return {
        "needs_expert": False,
        "session_id": session.id,
        "analysis": result.to_dict(),
        "problem_info": problem_info,
        "subscription_id": request.subscription_id,
        "committed_to_advisory": False,
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
    crop+stage+part. Problem names are returned in the user's
    language when available, else English."""
    lang = current_user.language_code or "en"
    rows = await _load_problem_symptom_rows(db, crop_cosh_id, crop_stage_cosh_id)
    problem_ids = get_problem_list(rows, plant_part=plant_part_cosh_id)
    result = []
    for pid in problem_ids:
        result.append(await _get_problem_info(db, pid, lang))
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
        info = await _get_problem_info(db, pid, current_user.language_code or "en")
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Two plain-language sentences telling the farmer how to verify
    the current question's symptom. Names get resolved through Cosh
    (cosh_core_items.translations) before they reach Claude — a raw
    UUID in the prompt would confuse the model.

    Locale resolution prefers `current_user.language_code` over the
    request body — the PWA doesn't pass language fields here and the
    Pydantic default of "en" was silently overriding the farmer's
    actual language. Falls back to the request body / English when
    the user has no language set.
    """
    lang = current_user.language_code or request.language_code or "en"
    lang_name = (
        language_name_for(current_user.language_code)
        if current_user.language_code
        else request.language_name or "English"
    )
    names = await resolve_names_by_cosh_id(
        db,
        {
            request.crop_cosh_id, request.plant_part_cosh_id,
            request.symptom_cosh_id, request.sub_part_cosh_id,
            request.sub_symptom_cosh_id,
        } - {None, ""},
        lang,
    )
    text = await explain_symptom(
        crop_name=names.get(request.crop_cosh_id) or request.crop_cosh_id,
        plant_part_name=names.get(request.plant_part_cosh_id) or request.plant_part_cosh_id,
        symptom_name=names.get(request.symptom_cosh_id) or request.symptom_cosh_id,
        sub_part_name=names.get(request.sub_part_cosh_id) if request.sub_part_cosh_id else None,
        sub_symptom_name=names.get(request.sub_symptom_cosh_id) if request.sub_symptom_cosh_id else None,
        language_code=lang,
        language_name=lang_name,
    )
    return {"explanation": text, "language_code": lang}


@router.post("/diagnosis/image-check-symptom")
async def image_check_symptom_route(
    request: ImageCheckSymptomRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """In-loop AI decision support for the dichotomous Yes/No flow.

    Farmer is on a question like "Do you see {symptom} on {part}?" and
    is unsure. They tap `Ask AI`, take a photo, and we ask Claude
    specifically whether the symptom is present — NOT what the
    underlying problem might be. Claude returns
    {verdict: YES|NO|UNCERTAIN, confidence, reasoning}; the PWA shows
    this as a hint and the farmer picks Yes / No themselves. The
    diagnose loop never auto-advances based on this reply — that's
    what `/diagnosis/image-analysis` is for (a different entry point
    above the question loop, where the farmer is explicitly delegating
    the diagnosis to AI).

    Locale derives from `current_user.language_code` so the reasoning
    text lands in the farmer's language without the PWA having to
    thread the field through.
    """
    lang = current_user.language_code or "en"
    lang_name = language_name_for(current_user.language_code)
    names = await resolve_names_by_cosh_id(
        db,
        {
            request.crop_cosh_id, request.plant_part_cosh_id,
            request.symptom_cosh_id, request.sub_part_cosh_id,
            request.sub_symptom_cosh_id,
        } - {None, ""},
        lang,
    )
    result = await check_symptom_in_image(
        image_base64=request.image_base64,
        media_type=request.media_type,
        crop_name=names.get(request.crop_cosh_id) or request.crop_cosh_id,
        plant_part_name=names.get(request.plant_part_cosh_id) or request.plant_part_cosh_id,
        symptom_name=names.get(request.symptom_cosh_id) or request.symptom_cosh_id,
        sub_part_name=names.get(request.sub_part_cosh_id) if request.sub_part_cosh_id else None,
        sub_symptom_name=names.get(request.sub_symptom_cosh_id) if request.sub_symptom_cosh_id else None,
        language_code=lang,
        language_name=lang_name,
    )
    return {"check": result.to_dict(), "language_code": lang}


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
    return await list_crop_stages(
        db, crop_cosh_id=crop_cosh_id,
        lang=current_user.language_code or "en",
    )


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
        lang=current_user.language_code or "en",
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
        lang=current_user.language_code or "en",
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
        lang=current_user.language_code or "en",
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
        lang=current_user.language_code or "en",
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
        lang=current_user.language_code or "en",
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
