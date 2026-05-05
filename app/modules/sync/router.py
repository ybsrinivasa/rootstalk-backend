from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Header, status, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.config import settings
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.sync.models import CoshSyncLog, VolumeFormula, CropHealthCrop, CropMeasure
from app.modules.sync.service import process_payload, get_cosh_translation

router = APIRouter(tags=["Cosh Sync"])

MAX_PAYLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


# ── POST /sync/cosh — Cosh pushes data here ────────────────────────────────────

@router.post("/sync/cosh")
async def receive_cosh_sync(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_cosh_api_key: str = Header(None, alias="X-Cosh-Api-Key"),
):
    """
    Accepts Cosh sync payload. Secured by shared API key.
    Built to the RootsTalk_CoshSync_APIContract spec.

    Note: A Field Mapping document will verify exact entity_type names
    once Cosh 2.0's first production sync is tested against this endpoint.
    """
    if not x_cosh_api_key or x_cosh_api_key != settings.cosh_sync_api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Cosh-Api-Key")

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large. Split into smaller batches.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Malformed JSON payload")

    if "entity_batches" not in payload:
        raise HTTPException(status_code=422, detail="Validation failed: entity_batches missing")

    sync_id = payload.get("sync_id", "unknown")
    sync_log = CoshSyncLog(
        sync_id=sync_id,
        initiated_by=payload.get("initiated_by"),
        sync_mode=payload.get("sync_mode", "incremental"),
        status="IN_PROGRESS",
    )
    db.add(sync_log)
    await db.flush()

    try:
        result = await process_payload(db, payload, sync_log)
        await db.commit()
        return result
    except Exception as e:
        sync_log.status = "FAILED"
        sync_log.error_log = {"error": str(e)}
        sync_log.completed_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(status_code=500, detail="Sync processing failed")


# ── GET /sync/cosh/log — Cosh Admin queries sync history ──────────────────────

@router.get("/sync/cosh/log")
async def get_sync_log(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):

    result = await db.execute(
        select(CoshSyncLog).order_by(CoshSyncLog.started_at.desc()).limit(limit)
    )
    logs = result.scalars().all()
    return [
        {
            "sync_id": log.sync_id,
            "sync_mode": log.sync_mode,
            "status": log.status,
            "items_synced": log.items_synced,
            "items_failed": log.items_failed,
            "started_at": log.started_at,
            "completed_at": log.completed_at,
        }
        for log in logs
    ]


# ── Volume Formulas API (CM with VOLUME_CALCULATIONS privilege) ────────────────

@router.get("/admin/volume-formulas")
async def list_volume_formulas(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(VolumeFormula).order_by(VolumeFormula.l2_practice))
    return result.scalars().all()


@router.post("/admin/volume-formulas", status_code=201)
async def create_volume_formula(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    formula = VolumeFormula(**{k: v for k, v in data.items() if k != "id"})
    db.add(formula)
    await db.commit()
    await db.refresh(formula)
    return formula


@router.put("/admin/volume-formulas/{formula_id}")
async def update_volume_formula(
    formula_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(VolumeFormula).where(VolumeFormula.id == formula_id))
    formula = result.scalar_one_or_none()
    if not formula:
        raise HTTPException(status_code=404, detail="Formula not found")
    for k, v in data.items():
        if k != "id":
            setattr(formula, k, v)
    await db.commit()
    await db.refresh(formula)
    return formula


# ── Crop Health Crops API (CM with CROP_HEALTH_CROPS privilege) ────────────────

@router.get("/admin/crop-health-crops")
async def list_crop_health_crops(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CropHealthCrop))
    return result.scalars().all()


@router.put("/admin/crop-health-crops/{crop_cosh_id}/enable")
async def enable_crop_health(
    crop_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(CropHealthCrop).where(CropHealthCrop.crop_cosh_id == crop_cosh_id)
    )
    crop = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if not crop:
        crop = CropHealthCrop(
            crop_cosh_id=crop_cosh_id,
            enabled_by=current_user.id,
            enabled_at=now,
            status="ACTIVE",
        )
        db.add(crop)
    else:
        crop.status = "ACTIVE"
        crop.enabled_by = current_user.id
        crop.enabled_at = now
    await db.commit()
    await db.refresh(crop)
    return crop


@router.put("/admin/crop-health-crops/{crop_cosh_id}/disable")
async def disable_crop_health(
    crop_cosh_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(CropHealthCrop).where(CropHealthCrop.crop_cosh_id == crop_cosh_id)
    )
    crop = result.scalar_one_or_none()
    if not crop:
        raise HTTPException(status_code=404, detail="Not found")
    crop.status = "INACTIVE"
    await db.commit()
    await db.refresh(crop)
    return crop


# ── Crop Measure (Phase D.1) ──────────────────────────────────────────────────
# AREA_WISE / PLANT_WISE classification per crop. Drives BL-06 volume-formula
# lookup and the SE practice-creation form. Today seeded manually by SA;
# Cosh sync will populate `synced_from_cosh_at` when integration ships.

class CropMeasureSetRequest(BaseModel):
    measure: str   # 'AREA_WISE' | 'PLANT_WISE'


@router.get("/admin/crop-measures")
async def list_crop_measures(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.crop_measure import list_measures
    rows = await list_measures(db)
    return [
        {
            "crop_cosh_id": r.crop_cosh_id,
            "measure": r.measure,
            "updated_by_user_id": r.updated_by_user_id,
            "synced_from_cosh_at": r.synced_from_cosh_at,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


@router.put("/admin/crop-measures/{crop_cosh_id}")
async def set_crop_measure(
    crop_cosh_id: str,
    request: CropMeasureSetRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.crop_measure import set_measure
    try:
        row = await set_measure(
            db, crop_cosh_id=crop_cosh_id, measure=request.measure,
            user_id=current_user.id,
        )
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "crop_cosh_id": row.crop_cosh_id,
        "measure": row.measure,
        "updated_by_user_id": row.updated_by_user_id,
        "updated_at": row.updated_at,
    }


# ── Cosh Reference Lookup (used internally by all other modules) ───────────────

@router.get("/internal/cosh-entity/{entity_type}/{cosh_id}")
async def lookup_cosh_entity(
    entity_type: str,
    cosh_id: str,
    lang: str = "en",
    db: AsyncSession = Depends(get_db),
):
    """Internal lookup for a Cosh entity's display name in a given language."""
    name = await get_cosh_translation(db, cosh_id, entity_type, lang)
    if name is None:
        raise HTTPException(status_code=404, detail="Entity not found in cache")
    return {"cosh_id": cosh_id, "entity_type": entity_type, "language": lang, "name": name}
