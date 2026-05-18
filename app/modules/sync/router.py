from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Header, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.config import settings
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.sync.models import CoshSyncLog, VolumeFormula, CropHealthCrop
from app.modules.sync.service import process_payload, get_cosh_translation
from app.modules.advisory.router import _assert_sa_or_cm, _assert_sa_or_privileged_cm

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


# ── SA-portal CM crop browse (post-Cosh-sync 2026-05-09) ───────────────────

@router.get("/admin/cosh/crops")
async def list_cosh_crops(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA-portal CM browse — full crop universe Cosh has classified.
    Returns `[{cosh_id, name_en, status}]` sorted by English name.

    The list grows as Cosh curators classify more biological_names as
    Crop via the `biological_names_and_roles` Connect; sync runs are
    incremental, so no RootsTalk action is needed when Cosh adds new
    crops — just appears here on next page refresh."""
    from app.services.cosh_crop_view import list_crops
    return await list_crops(db)


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
    await _assert_sa_or_privileged_cm(db, current_user, "VOLUME_CALCULATIONS")
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
    await _assert_sa_or_privileged_cm(db, current_user, "VOLUME_CALCULATIONS")
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
    await _assert_sa_or_privileged_cm(db, current_user, "CROP_HEALTH_CROPS")
    # 2026-05-18 — refuse free-text IDs. The CM was typing names
    # like "Tomato" / "tomato" into the page's old text box; they
    # got accepted and showed up as bogus disabled rows. Reject
    # anything that isn't a real Cosh crop ID.
    from app.services.cosh_crop_view import is_crop_in_cosh
    if not await is_crop_in_cosh(db, crop_cosh_id):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_cosh_crop",
                "message": (
                    f"'{crop_cosh_id}' is not a Cosh crop. Pick a crop "
                    f"from the Cosh crops list — that's the source of "
                    f"truth for what crops exist."
                ),
                "crop_cosh_id": crop_cosh_id,
            },
        )
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
    await _assert_sa_or_privileged_cm(db, current_user, "CROP_HEALTH_CROPS")
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


# ── Crop Measure — Cosh-sourced (Round 3, 2026-05-09) ───────────────────────
# AREA_WISE / PLANT_WISE classification comes from the `crop_area_plant_wise`
# Cosh Connect now. The previous SA-edited admin endpoint was removed (Cosh
# is the source); this read-through endpoint surfaces the joined view so the
# SA team can see which crops still need Cosh-side classification.

@router.get("/admin/crop-measures")
async def list_crop_measures(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read-through view of the Cosh-derived Crop → Measure mapping.
    Returns one row per Crop biological_name; `measure` is None when
    Cosh hasn't classified that crop in `crop_area_plant_wise` yet."""
    from app.services.cosh_crop_view import list_crops_with_measure
    return await list_crops_with_measure(db)


# ── Cosh Locations (state + district picker source) ────────────────────────────

@router.get("/cosh/locations/india")
async def list_cosh_india_locations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full Cosh India locations list, shaped for state-grouped pickers.

    Pulls distinct `(state, district)` pairs from `cosh_connect_rows`
    where `connect_type='india_locations'` (each row is a country/
    state/district/subdistrict tuple — we dedupe on the first three
    and ignore the sub-district since packages don't target below
    district level). Joins `cosh_core_items` for English names.

    Used by:
      - Setup → Locations tab (CA picks the company's footprint here).
      - The package detail Edit Locations modal indirectly, via
        /client/{cid}/location-options-for-package which filters this
        list to the company's footprint.

    Defensive on partial Cosh data: pairs whose state/district isn't
    in `cosh_core_items` (still-pending sync) surface as `null` name
    so the picker can render them as "(unnamed)" without breaking.
    """
    from app.modules.sync.models import CoshConnectRow, CoshCoreItem

    rows = (await db.execute(
        select(CoshConnectRow.endpoints).where(
            CoshConnectRow.connect_type == "india_locations",
            CoshConnectRow.status == "active",
        )
    )).scalars().all()

    pairs: set[tuple[str, str]] = set()
    for endpoints in rows:
        state_id, district_id = None, None
        for ep in endpoints or []:
            role = ep.get("role")
            if role == "state_list":
                state_id = ep.get("cosh_id")
            elif role == "district_list":
                district_id = ep.get("cosh_id")
        if state_id and district_id:
            pairs.add((state_id, district_id))

    needed_ids = {sid for sid, _ in pairs} | {did for _, did in pairs}
    if not needed_ids:
        return {"states": []}

    cores = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.core_type, CoshCoreItem.translations)
        .where(
            CoshCoreItem.cosh_id.in_(needed_ids),
            CoshCoreItem.core_type.in_(["state_list", "district_list"]),
        )
    )).all()
    state_names: dict[str, str] = {}
    district_names: dict[str, str] = {}
    for cosh_id, core_type, translations in cores:
        name = (translations or {}).get("en") if isinstance(translations, dict) else None
        if core_type == "state_list":
            state_names[cosh_id] = name
        elif core_type == "district_list":
            district_names[cosh_id] = name

    by_state: dict[str, list[dict]] = {}
    for sid, did in pairs:
        by_state.setdefault(sid, []).append({
            "cosh_id": did,
            "name": district_names.get(did),
        })

    states = []
    for sid, districts in by_state.items():
        districts.sort(key=lambda d: (d["name"] or "").lower())
        states.append({
            "cosh_id": sid,
            "name": state_names.get(sid),
            "districts": districts,
        })
    states.sort(key=lambda s: (s["name"] or "").lower())
    return {"states": states}


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
